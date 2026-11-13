"""The three arms, one attempt's Harbor job config, and the record-engine arm's query server
(ADR-0016, ADR-0019, ADR-0022).

Every attempt is Exp 1's `fresh` repair job (`trajlab.capture.repair_launcher.repair_job_config`):
the corpus job's model, agent settings, hooks, and timeouts, the pre-installed task image, a new
Claude Code session, and `RepairClaudeCode` with the retry prompt's prefix as its note. The arms
differ only in that note's evidence sentence and in what the agent can reach:

    control        nothing
    record-files   the chain's bundle, a read-only bind mount at /logs/agent/record
    record-engine  /logs/agent/bin/query, answered from the chain's store by a server this
                   module runs for the attempt's duration
"""

import logging
import threading
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig

from trajlab.capture.repair_launcher import repair_job_config
from trajlab.contracts import RECORD_MOUNT, ArmConfig, Exp2Arm
from trajlab.store.query import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_ROW_CAP,
    DEFAULT_THREADS,
    DEFAULT_TIMEOUT_S,
)
from trajlab.store.serve import ServeError, Server

logger = logging.getLogger(__name__)

INPUTS_DIRNAME = "_exp2-inputs"


def arm_config(
    arm: Exp2Arm,
    *,
    cap: int = DEFAULT_ROW_CAP,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    threads: int = DEFAULT_THREADS,
) -> ArmConfig:
    """The arm's settings; the query caps apply to record-engine only."""
    engine = arm == "record-engine"
    return ArmConfig(
        arm=arm,
        ingests=arm != "control",
        mounts_bundle=arm == "record-files",
        serves_queries=engine,
        query_cap=cap if engine else None,
        query_timeout_s=timeout_s if engine else None,
        query_memory_limit=memory_limit if engine else None,
        query_threads=threads if engine else None,
    )


def job_name(run: str, root_trial: str, arm: Exp2Arm, attempt_idx: int, rerun: int = 0) -> str:
    """`<run>-<root trial>-<arm>-a<t>`, and `-r<n>` for the n-th rerun of an attempt the harness
    failed. It is also the attempt's corpus id."""
    name = f"{run}-{root_trial}-{arm}-a{attempt_idx}"
    return f"{name}-r{rerun}" if rerun else name


def job_config(
    corpus_config: dict[str, Any],
    *,
    task: str,
    arm: ArmConfig,
    name: str,
    jobs_dir: Path,
    repair_note: str,
    bundle: Path | None,
) -> dict[str, Any]:
    """One attempt's Harbor job config: one trial of `task`, as the corpus job ran it, with the
    retry prompt's prefix as the agent's note and, for record-files, the bundle mounted."""
    if arm.mounts_bundle != (bundle is not None):
        raise ValueError(f"arm {arm.arm}: a bundle exactly when it mounts one")
    config = repair_job_config(
        corpus_config,
        task_name=task,
        arm="fresh",
        job_name=name,
        jobs_dir=jobs_dir,
        attempts=1,
        checkpoint_image=None,
        session_file=None,
    )
    config["agents"][0]["kwargs"]["repair_note"] = repair_note
    if bundle is not None:
        config["environment"]["mounts"] = [
            {
                "type": "bind",
                "source": str(bundle.resolve()),
                "target": RECORD_MOUNT,
                "read_only": True,
            }
        ]
    JobConfig.model_validate(config)
    return config


class QueryServer:
    """`trajlab.store.serve.Server` for one record-engine attempt, in a thread of this process.

    `trajlab run` refuses a job dir that exists, so the server cannot be started on it before
    Harbor makes it; the thread waits for the dir and serves from then on. Harbor makes the
    trial dir and its `agent/` in the trial's constructor, before the container starts
    (docs/harbor-facts.md), so the server's first pass (0.1 s) puts `bin/query` in place long
    before the agent can run; the loop checks that it did (`query_tool_ready`)."""

    def __init__(self, store: Path, job_dir: Path, arm: ArmConfig, poll_s: float = 0.1) -> None:
        if not arm.serves_queries:
            raise ValueError(f"arm {arm.arm} serves no queries")
        self.store = store
        self.job_dir = job_dir
        self.poll_s = poll_s
        self.stop_event = threading.Event()
        self.error: Exception | None = None
        self.server = Server(
            store,
            job_dir,
            cap=arm.query_cap or DEFAULT_ROW_CAP,
            timeout_s=arm.query_timeout_s or DEFAULT_TIMEOUT_S,
            memory_limit=arm.query_memory_limit,
            threads=arm.query_threads,
            poll_s=poll_s,
            workers=1,
        )
        self.thread = threading.Thread(
            target=self._serve, name=f"query-server-{job_dir.name}", daemon=True
        )

    def _serve(self) -> None:
        while not self.job_dir.is_dir():
            if self.stop_event.wait(self.poll_s):
                return
        try:
            self.server.run(self.stop_event)
        except ServeError as error:
            self.error = error
            logger.error("query server for %s: %s", self.job_dir.name, error)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        """Stop serving and wait for the query under way; the store is then free to build."""
        self.stop_event.set()
        self.thread.join()
