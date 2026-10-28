"""The retry loop (technical spec §5.1; ADR-0016 items 2 and 3; ADR-0022): for each root failure
and arm, attempt, let the verifier score it, and stop at reward 1 or at N; otherwise build the
attempt into the chain's store (record arms) and start the next one.

`chain_state` reads what a chain needs next from its two logs alone, so the loop resumes from
disk: a chain is done (passed, or N attempts counted), stopped (an ingest failed, the harness
failed one attempt `max_reruns` times, usage limits aside, an unclassified exception waits for
a person, or the prompt would leak), or needs its last attempt ingested, or its next attempt
launched. An attempt the harness failed (`infra`) or that left no result (`lost`) is not an
attempt: it is run again under a new job name, `-r<n>`, and its job dir is kept.

`Exp2Runner` runs the chains `max_running` attempts at a time. Each attempt is one `trajlab run`
subprocess, so it gets the same pins, watcher check, and manifest as a corpus trial; the loop
holds launches while the watcher or the write recorder is not running, while the disk is below
`hold_below_gb`, while the tree has changes `trajlab run` would refuse, and for
`usage_backoff_s` after a usage limit. A record-engine attempt has its query server for exactly
the attempt's duration; the server is stopped before the attempt is ingested (ADR-0019 item 8).
Ingests run in worker threads, so a slow `docker image save` delays only its own chain.
"""

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import duckdb
from harbor.models.trajectories import Trajectory
from harbor.models.trial.result import TrialResult
from pydantic import ValidationError

from trajlab.capture.repair_launcher import (
    REPAIRABLE_KINDS,
    USAGE_LIMIT_EXCEPTION,
    classify_failure,
    commit_new_manifests,
    finished_trials,
    pid_alive,
    trial_reward,
)
from trajlab.checkpoint.watcher import watcher_running
from trajlab.checkpoint.writes import recorder_running
from trajlab.contracts import (
    QUERIES_DIRNAME,
    QUERY_LOG_FILENAME,
    QUERY_TOOL_RELPATH,
    ArmConfig,
    AttemptLaunch,
    AttemptOutcome,
    AttemptResult,
    Exp2Settings,
    IngestResult,
    RootFailure,
)
from trajlab.exp2 import chain as chains
from trajlab.exp2 import leaks, prompt
from trajlab.exp2.arms import INPUTS_DIRNAME, QueryServer, arm_config, job_config, job_name
from trajlab.exp2.prereg import instructions
from trajlab.store.describe import describe
from trajlab.store.schema import connect_agent

logger = logging.getLogger(__name__)

# Linux caps one environment string at 128 KiB (MAX_ARG_STRLEN), and Harbor passes the prompt
# as one (docs/harbor-facts.md); leave room for the variable's name.
MAX_PROMPT_BYTES = 131_072 - 1_024
LAUNCH_FILENAME = "launch.json"
PROMPT_FILENAME = "prompt.txt"
CONFIG_FILENAME = "config.json"
RUN_LOG_FILENAME = "run.log"
STATUS_FILENAME = "status.json"

StateKind = Literal["launch", "ingest", "done", "stopped"]


@dataclass(frozen=True)
class ChainState:
    kind: StateKind
    attempt_idx: int = 0
    rerun: int = 0
    parent_trial_id: UUID | None = None
    reason: str = ""


def chain_state(
    attempts: Sequence[AttemptResult],
    ingests: Sequence[IngestResult],
    *,
    root_trial_id: UUID,
    budget: int,
    ingests_rows: bool,
    max_reruns: int,
    retry_ingest: bool = False,
    rerun_unclassified: bool = False,
) -> ChainState:
    """What the chain needs next, from its logs. `retry_ingest` lets a failed ingest be tried
    again; `rerun_unclassified` runs again an attempt with an unclassified exception."""
    counted = [a for a in attempts if a.counted]
    for position, attempt in enumerate(counted, start=1):
        if attempt.attempt_idx != position:
            raise chains.ChainError(f"counted attempts are not 1, 2, ...: {position} is missing")
    if counted and counted[-1].outcome == "passed":
        return ChainState("done", counted[-1].attempt_idx, reason="passed")
    done = len(counted)
    if done and ingests_rows and done < budget:
        mine = [i for i in ingests if i.attempt_idx == done]
        if not any(i.ok for i in mine):
            last = counted[-1]
            if mine and not retry_ingest:
                return ChainState("stopped", done, reason=f"ingest failed: {mine[-1].error}")
            return ChainState("ingest", done, parent_trial_id=last.parent_trial_id)
    if done >= budget:
        return ChainState("done", done, reason=f"failed all {budget}")
    upcoming = done + 1
    tries = [a for a in attempts if a.attempt_idx == upcoming and not a.counted]
    if tries and tries[-1].failure_kind == "unclassified" and not rerun_unclassified:
        return ChainState(
            "stopped",
            upcoming,
            reason=f"{tries[-1].exception_type} is unclassified; a person decides "
            "(ADR-0012 item 4)",
        )
    # A usage limit is the subscription's, not the harness's: it reruns without limit, since the
    # same chains are first in line each time launches resume.
    harness = [a for a in tries if a.exception_type != USAGE_LIMIT_EXCEPTION]
    if len(harness) >= max_reruns:
        reason = f"the harness failed attempt {upcoming} {len(harness)} times"
        return ChainState("stopped", upcoming, reason=reason)
    parent = counted[-1].trial_id if counted else root_trial_id
    return ChainState("launch", upcoming, rerun=len(tries), parent_trial_id=parent)


@dataclass
class Chain:
    root: RootFailure
    arm: ArmConfig
    dir: Path
    root_dir: Path
    instruction: str

    @property
    def name(self) -> str:
        return f"{self.root.trial_name}/{self.arm.arm}"

    @property
    def store(self) -> Path:
        return chains.store_dir(self.dir)


@dataclass
class Running:
    chain: Chain
    launch: AttemptLaunch
    process: subprocess.Popen[bytes] | None
    server: QueryServer | None


def outcome_of(result: TrialResult | None) -> tuple[AttemptOutcome, Any]:
    """How an attempt counts, from its result.json: passed, failed (the agent's own failure), or
    infra (the harness's, or unclassified); lost without a result."""
    if result is None:
        return "lost", None
    kind = classify_failure(result)
    if kind is None:
        return "passed", None
    return ("failed" if kind in REPAIRABLE_KINDS else "infra"), kind


def _first_user_step(trial_dir: Path) -> tuple[str | None, datetime | None]:
    path = trial_dir / "agent" / "trajectory.json"
    if not path.is_file():
        return None, None
    try:
        trajectory = Trajectory.model_validate_json(path.read_text())
    except ValidationError:
        return None, None
    for step in trajectory.steps:
        if step.source == "user":
            message = step.message if isinstance(step.message, str) else None
            stamp = datetime.fromisoformat(step.timestamp) if step.timestamp else None
            return message, stamp
    return None, None


def attempt_result(
    launch: AttemptLaunch,
    *,
    root: RootFailure,
    prompt_text: str,
    job_dir: Path,
    finished_at: datetime,
) -> AttemptResult:
    """The `AttemptResult` of a finished attempt's job dir."""
    trials = finished_trials(job_dir)
    trial_dir, result = trials[0] if trials else (None, None)
    if trial_dir is None:
        made = sorted(p for p in job_dir.iterdir() if p.is_dir()) if job_dir.is_dir() else []
        trial_dir = made[0] if len(made) == 1 else None
    outcome, kind = outcome_of(result)
    exception = result.exception_info.exception_type if result and result.exception_info else None
    delivered: bool | None = None
    queries: int | None = None
    ready: bool | None = None
    if trial_dir is not None:
        message, first_at = _first_user_step(trial_dir)
        delivered = None if message is None else message == prompt_text
        if launch.arm == "record-engine":
            agent = trial_dir / "agent"
            log = agent / QUERY_LOG_FILENAME
            queries = len(log.read_text().splitlines()) if log.is_file() else 0
            tool = agent / QUERY_TOOL_RELPATH
            ready = (
                tool.is_file()
                and log.is_file()
                and (agent / QUERIES_DIRNAME).is_dir()
                and (first_at is None or tool.stat().st_mtime <= first_at.timestamp())
            )
    elif launch.arm == "record-engine":
        queries, ready = 0, False
    note = None
    if outcome == "lost":
        note = "no result.json: `trajlab run` refused or Harbor stopped; see the run log"
    elif outcome == "infra":
        note = f"not an attempt: {exception or 'no reward'}"
    return AttemptResult(
        run=launch.run,
        arm=launch.arm,
        root_trial=root.trial_name,
        root_trial_id=root.trial_id,
        task=root.task,
        attempt_idx=launch.attempt_idx,
        parent_trial_id=launch.parent_trial_id,
        job_name=launch.job_name,
        outcome=outcome,
        trial_name=trial_dir.name if trial_dir is not None else None,
        trial_id=result.id if result is not None else None,
        reward=trial_reward(result) if result is not None else None,
        failure_kind=kind,
        exception_type=exception,
        prompt_sha256=launch.prompt_sha256,
        prompt_bytes=launch.prompt_bytes,
        prompt_delivered=delivered,
        store_trials=launch.store_trials,
        bundle=launch.bundle,
        queries=queries,
        query_tool_ready=ready,
        preregistration=launch.preregistration,
        started_at=launch.started_at,
        finished_at=max(finished_at, launch.started_at),
        note=note,
    )


def default_spawn(command: list[str], log_path: Path) -> subprocess.Popen[bytes]:
    with log_path.open("ab") as log:
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)


def capture_problem(jobs_dir: Path) -> str | None:
    """Why an attempt would not be captured as the corpus was: no watcher or no write recorder
    holds the jobs dir (ADR-0021); None when both run."""
    if not watcher_running(jobs_dir):
        return f"no watcher holds {jobs_dir}: start `trajlab watch {jobs_dir}`"
    if not recorder_running(jobs_dir):
        return f"no write recorder holds {jobs_dir}: start `sudo trajlab record-writes {jobs_dir}`"
    return None


def _disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


@dataclass
class Exp2Runner:
    """One poll loop over a run's chains. `step()` is one pass; `run()` loops until done."""

    settings: Exp2Settings
    preregistration: str | None
    root: Path
    jobs_dir: Path
    manifests_dir: Path
    work_dir: Path
    env_file: Path | None
    storage: str | None = None
    max_running: int = 6
    hold_below_gb: float = 50.0
    usage_backoff_s: float = 1800.0
    max_reruns: int = 3
    ingest_workers: int = 1
    prune_ingested: bool = False
    retry_failed_ingests: bool = False
    rerun_unclassified: bool = False
    spawn: Callable[[list[str], Path], subprocess.Popen[bytes]] = default_spawn
    open_images: chains.OpenImages = chains.docker_images
    needles_of: Callable[[Path], leaks.Needles] = leaks.needles
    capture_problem: Callable[[Path], str | None] = capture_problem
    commit_manifests: Callable[[Path], list[str]] = commit_new_manifests
    disk_free_gb: Callable[[Path], float] = _disk_free_gb
    clock: Callable[[], float] = time.monotonic
    alive: Callable[[int, str], bool] = pid_alive

    chains: list[Chain] = field(default_factory=list, init=False)
    running: dict[str, Running] = field(default_factory=dict, init=False)
    ingesting: dict[str, Future[IngestResult]] = field(default_factory=dict, init=False)
    stopped: dict[str, str] = field(default_factory=dict, init=False)
    retried_ingest: set[str] = field(default_factory=set, init=False)
    paused_until: float = field(default=0.0, init=False)
    hold: str | None = field(default=None, init=False)
    executor: ThreadPoolExecutor | None = field(default=None, init=False)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if "record-engine" in self.settings.arms and self.preregistration is None:
            raise chains.ChainError(
                "record-engine runs only under a committed preregistration (technical spec §5.3)"
            )
        corpus_store = Path(self.settings.corpus_store)
        instruction = instructions(corpus_store, self.settings.sample)
        query = self.settings.query
        for root in self.settings.sample:
            for arm in self.settings.arms:
                config = query if arm == "record-engine" and query is not None else arm_config(arm)
                self.chains.append(
                    Chain(
                        root=root,
                        arm=config,
                        dir=chains.chain_dir(self.root, self.settings.run, root.trial_name, arm),
                        root_dir=Path(self.settings.corpus_job) / root.trial_name,
                        instruction=instruction[root.trial_name],
                    )
                )

    @property
    def inputs_root(self) -> Path:
        return self.jobs_dir / INPUTS_DIRNAME

    def state(self, chain: Chain) -> ChainState:
        if chain.name in self.stopped:
            return ChainState("stopped", reason=self.stopped[chain.name])
        return chain_state(
            chains.read_attempts(chain.dir),
            chains.read_ingests(chain.dir),
            root_trial_id=chain.root.trial_id,
            budget=self.settings.budget,
            ingests_rows=chain.arm.ingests,
            max_reruns=self.max_reruns,
            retry_ingest=self.retry_failed_ingests and chain.name not in self.retried_ingest,
            rerun_unclassified=self.rerun_unclassified,
        )

    # -- launching ---------------------------------------------------------------------------

    def _store_for(self, chain: Chain, state: ChainState) -> tuple[tuple[UUID, ...], Path | None]:
        """The chain's store holding exactly attempts 0 to the next one's minus 1, and for
        record-files the bundle of it; seeded with the root failure at attempt 1."""
        if not chain.arm.ingests:
            return (), None
        if state.attempt_idx == 1:
            chains.seed_store(Path(self.settings.corpus_store), chain.store, chain.root.trial_id)
        held = chains.store_trials(chain.store)
        if len(held) != state.attempt_idx or held[0] != chain.root.trial_id:
            raise chains.ChainError(
                f"{chain.store} holds {len(held)} attempts; attempt {state.attempt_idx} needs "
                f"exactly the {state.attempt_idx} before it"
            )
        if held[-1] != state.parent_trial_id:
            raise chains.ChainError(f"{chain.store} does not end with {state.parent_trial_id}")
        bundle = None
        if chain.arm.mounts_bundle:
            bundle = chains.export_bundle(
                chain.store, chains.bundle_dir(chain.dir, state.attempt_idx)
            )
        return held, bundle

    def _prompt(self, chain: Chain, k: int) -> tuple[str, str]:
        """The prompt's prefix (the agent's note) and the whole rendered prompt, refused unless
        the prefix is the preregistered one and the prompt names no check and quotes no
        verifier text."""
        text = None
        if chain.arm.arm == "record-engine":
            with connect_agent(chain.store) as con:
                text = describe(con)
        before = prompt.prefix(chain.arm.arm, k, text)
        key = f"{chain.arm.arm}/{k}"
        if prompt.sha256(before) != self.settings.prefix_sha256.get(key):
            raise chains.ChainError(f"the prompt prefix {key} is not the preregistered one")
        full = prompt.render(before, chain.instruction)
        if len(full.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise chains.ChainError(f"the prompt is over {MAX_PROMPT_BYTES} bytes")
        found = leaks.leaks(full, chain.instruction, self.needles_of(chain.root_dir))
        if found:
            raise chains.ChainError(f"the prompt leaks {'; '.join(found)}")
        return before, full

    def launch(self, chain: Chain, state: ChainState) -> None:
        assert state.parent_trial_id is not None
        name = job_name(
            self.settings.run, chain.root.trial_name, chain.arm.arm, state.attempt_idx, state.rerun
        )
        inputs = self.inputs_root / name
        if inputs.exists() or (self.jobs_dir / name).exists():
            # A launch of an earlier loop: wait for it, or record that it was lost.
            self.adopt(chain, name)
            return
        try:
            held, bundle = self._store_for(chain, state)
            note, text = self._prompt(chain, state.attempt_idx)
            config = job_config(
                json.loads(Path(self.settings.corpus_config).read_text()),
                task=chain.root.task,
                arm=chain.arm,
                name=name,
                jobs_dir=self.jobs_dir,
                repair_note=note,
                bundle=bundle,
            )
        except (ValueError, OSError, duckdb.Error) as error:
            logger.error("%s: not launched: %s", chain.name, error)
            self.stopped[chain.name] = str(error)
            return
        inputs.mkdir(parents=True)
        (inputs / CONFIG_FILENAME).write_text(json.dumps(config, indent=2) + "\n")
        (inputs / PROMPT_FILENAME).write_bytes(text.encode("utf-8"))
        launch = AttemptLaunch(
            run=self.settings.run,
            arm=chain.arm.arm,
            root_trial=chain.root.trial_name,
            attempt_idx=state.attempt_idx,
            rerun=state.rerun,
            job_name=name,
            parent_trial_id=state.parent_trial_id,
            prompt_sha256=prompt.sha256(text),
            prompt_bytes=len(text.encode("utf-8")),
            store_trials=held,
            bundle=None if bundle is None else str(bundle),
            preregistration=self.preregistration,
            started_at=datetime.now(UTC),
        )
        server = None
        if chain.arm.serves_queries:
            server = QueryServer(chain.store, self.jobs_dir / name, chain.arm)
            server.start()
        command = [
            str(Path(sys.executable).parent / "trajlab"),
            "run",
            str(inputs / CONFIG_FILENAME),
        ]
        command += ["--corpus-id", name, "--manifests-dir", str(self.manifests_dir)]
        if self.storage is not None:
            command += ["--storage", self.storage]
        if self.env_file is not None:
            command += ["--env-file", str(self.env_file)]
        try:
            process = self.spawn(command, inputs / RUN_LOG_FILENAME)
        except OSError as error:
            if server is not None:
                server.stop()
            logger.error("%s: `trajlab run` did not start: %s", name, error)
            self.stopped[chain.name] = f"`trajlab run` did not start: {error}"
            return
        launch = launch.model_copy(update={"pid": process.pid})
        (inputs / LAUNCH_FILENAME).write_text(launch.model_dump_json(indent=2) + "\n")
        self.running[name] = Running(chain, launch, process, server)
        logger.info(
            "launched %s (%s, attempt %d%s; %d running)",
            name,
            chain.name,
            state.attempt_idx,
            f", rerun {state.rerun}" if state.rerun else "",
            len(self.running),
        )

    def adopt(self, chain: Chain, name: str) -> None:
        """Take over an attempt an earlier loop launched: wait for it while its `trajlab run`
        lives; otherwise record it from its job dir (finished, or lost)."""
        inputs = self.inputs_root / name
        path = inputs / LAUNCH_FILENAME
        if not path.is_file():
            # Stopped between making the inputs dir and writing the launch: nothing ran.
            shutil.move(str(inputs), str(inputs.with_name(f"{name}.unlaunched-{int(time.time())}")))
            logger.warning("%s: moved aside an inputs dir with no launch record", name)
            return
        launch = AttemptLaunch.model_validate_json(path.read_text())
        server = None
        if launch.pid is not None and self.alive(launch.pid, name):
            if chain.arm.serves_queries:
                server = QueryServer(chain.store, self.jobs_dir / name, chain.arm)
                server.start()
            logger.info("adopted %s, still running as pid %d", name, launch.pid)
        self.running[name] = Running(chain, launch, None, server)

    # -- finishing ---------------------------------------------------------------------------

    def _exited(self, entry: Running) -> bool:
        if entry.process is not None:
            return entry.process.poll() is not None
        pid = entry.launch.pid
        return pid is None or not self.alive(pid, entry.launch.job_name)

    def reap(self) -> None:
        for name, entry in list(self.running.items()):
            if not self._exited(entry):
                continue
            del self.running[name]
            if entry.server is not None:
                entry.server.stop()
            text = (self.inputs_root / name / PROMPT_FILENAME).read_bytes().decode("utf-8")
            result = attempt_result(
                entry.launch,
                root=entry.chain.root,
                prompt_text=text,
                job_dir=self.jobs_dir / name,
                finished_at=datetime.now(UTC),
            )
            chains.append_attempt(entry.chain.dir, result)
            logger.info(
                "%s: %s (reward %s)%s",
                name,
                result.outcome,
                result.reward,
                f", {result.exception_type}" if result.exception_type else "",
            )
            if result.prompt_delivered is False:
                logger.error("%s: the agent's first message is not the rendered prompt", name)
            if result.exception_type == USAGE_LIMIT_EXCEPTION:
                self.paused_until = self.clock() + self.usage_backoff_s
                logger.warning("usage limit: launches paused for %.0f s", self.usage_backoff_s)

    # -- ingesting ---------------------------------------------------------------------------

    def _ingest(self, chain: Chain, state: ChainState) -> IngestResult:
        attempt = next(
            a
            for a in chains.read_attempts(chain.dir)
            if a.counted and a.attempt_idx == state.attempt_idx
        )
        assert attempt.trial_id is not None and attempt.trial_name is not None
        trial_dir = self.jobs_dir / attempt.job_name / attempt.trial_name
        bundle = (
            chains.bundle_dir(chain.dir, state.attempt_idx + 1) if chain.arm.mounts_bundle else None
        )
        result = chains.ingest(
            trial_dir,
            chain.store,
            trial_id=attempt.trial_id,
            attempt_idx=state.attempt_idx,
            parent_trial_id=attempt.parent_trial_id,
            work_dir=self.work_dir,
            bundle_out=bundle,
            open_images=self.open_images,
        )
        if result.ok and self.prune_ingested:
            result = result.model_copy(update={"images_pruned": chains.prune_ingested(trial_dir)})
        chains.append_ingest(chain.dir, result)
        logger.info(
            "%s: ingested attempt %d in %.1f s%s",
            chain.name,
            state.attempt_idx,
            result.seconds,
            "" if result.ok else f": {result.error}",
        )
        return result

    def start_ingests(self) -> None:
        for name, future in list(self.ingesting.items()):
            if future.done():
                del self.ingesting[name]
                if (error := future.exception()) is not None:
                    logger.error("%s: ingest raised: %s", name, error)
                    self.stopped[name] = f"ingest raised {error}"
        for chain in self.chains:
            if chain.name in self.ingesting or self._busy(chain):
                continue
            state = self.state(chain)
            if state.kind != "ingest":
                continue
            if any(i.attempt_idx == state.attempt_idx for i in chains.read_ingests(chain.dir)):
                self.retried_ingest.add(chain.name)
            if self.executor is None:
                self._ingest(chain, state)
            else:
                self.ingesting[chain.name] = self.executor.submit(self._ingest, chain, state)

    # -- the pass ----------------------------------------------------------------------------

    def _busy(self, chain: Chain) -> bool:
        return any(entry.chain is chain for entry in self.running.values())

    def hold_reason(self) -> str | None:
        if self.clock() < self.paused_until:
            return "usage-limit pause"
        free = self.disk_free_gb(self.jobs_dir)
        if free < self.hold_below_gb:
            return f"{free:.0f} GB free, below {self.hold_below_gb:.0f} GB"
        problem = self.capture_problem(self.jobs_dir)
        if problem is not None:
            return problem
        try:
            dirty = self.commit_manifests(self.manifests_dir)
        except subprocess.CalledProcessError as error:  # e.g. another commit holds index.lock
            return f"committing manifests failed: {error}"
        if dirty:
            return f"uncommitted changes `trajlab run` would refuse: {', '.join(dirty[:5])}"
        return None

    def launch_ready(self) -> None:
        ready = [
            (state, chain)
            for chain in self.chains
            if chain.name not in self.ingesting and not self._busy(chain)
            for state in [self.state(chain)]
            if state.kind == "launch"
        ]
        if not ready or len(self.running) >= self.max_running:
            return
        reason = self.hold_reason()
        if reason != self.hold:
            if reason is not None:
                logger.warning("holding launches: %s", reason)
            self.hold = reason
        if reason is not None:
            return
        # Earlier attempts first, then the sample's order with the arms interleaved, so the
        # arms of one failure run close together in time.
        order = {chain.name: i for i, chain in enumerate(self.chains)}
        ready.sort(key=lambda item: (item[0].attempt_idx, order[item[1].name]))
        for state, chain in ready:
            if len(self.running) >= self.max_running:
                break
            self.launch(chain, state)

    def done(self) -> bool:
        if self.running or self.ingesting:
            return False
        return all(self.state(chain).kind in ("done", "stopped") for chain in self.chains)

    def status(self) -> dict[str, Any]:
        states = {chain.name: self.state(chain) for chain in self.chains}
        return {
            "updated_at": datetime.now(UTC).isoformat(),
            "run": self.settings.run,
            "running": sorted(self.running),
            "ingesting": sorted(self.ingesting),
            "hold": self.hold,
            "chains": {
                name: {"state": s.kind, "attempt": s.attempt_idx, "reason": s.reason}
                for name, s in states.items()
            },
        }

    def step(self) -> None:
        self.reap()
        self.start_ingests()
        self.launch_ready()
        path = self.root / self.settings.run / STATUS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.status(), indent=2) + "\n")

    def run(self, poll_s: float = 30.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        with ThreadPoolExecutor(max_workers=self.ingest_workers) as executor:
            self.executor = executor
            try:
                while not stop.is_set():
                    self.step()
                    if self.done():
                        break
                    stop.wait(poll_s)
            finally:
                self.executor = None
        for name, state in ((c.name, self.state(c)) for c in self.chains):
            logger.info(
                "%s: %s at attempt %d %s", name, state.kind, state.attempt_idx, state.reason
            )
