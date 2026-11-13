"""One attempt's Harbor job config per arm, and the record-engine arm's query server (ADR-0022)."""

import json
import time
from pathlib import Path
from typing import Any

import pytest
from harbor.models.job.config import JobConfig

from tests.test_exp2_chain import TRIAL_ID, as_attempt, copy_trial
from tests.test_store_build import all_images
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.repair_launcher import HOOKS, REPAIR_AGENT
from trajlab.contracts import RECORD_MOUNT
from trajlab.exp2.arms import QueryServer, arm_config, job_config, job_name
from trajlab.exp2.chain import seed_store
from trajlab.store.build import build_trial

CORPUS_CONFIG: dict[str, Any] = {
    "job_name": "tb40-sonnet-v4",
    "jobs_dir": "corpus/jobs",
    "n_attempts": 3,
    "n_concurrent_trials": 6,
    "agent_timeout_multiplier": 0.5,
    "agents": [
        {
            "name": "claude-code",
            "model_name": "anthropic/claude-sonnet-5-5",
            "kwargs": {"reasoning_effort": "medium", "version": "2.1.278", "config": HOOKS},
        }
    ],
    "datasets": [
        {
            "name": "terminal-bench/terminal-bench",
            "ref": "4.0.0",
            "task_names": ["terminal-bench/cad-model", "terminal-bench/mvcc-lsm-compaction"],
        }
    ],
    "environment": {"import_path": "trajlab.capture.preinstall:PreinstalledDockerEnvironment"},
}


def config_for(arm: str, bundle: Path | None = None) -> dict[str, Any]:
    return job_config(
        CORPUS_CONFIG,
        task="terminal-bench/mvcc-lsm-compaction",
        arm=arm_config(arm),  # type: ignore[arg-type]
        name=job_name("tb40-exp2-dry1", "mvcc-lsm-compaction__PFDAk9Y", arm, 1),  # type: ignore[arg-type]
        jobs_dir=Path("corpus/jobs"),
        repair_note="1 previous attempt(s) at this task FAILED ...",
        bundle=bundle,
    )


@pytest.mark.parametrize("arm", ["control", "record-files", "record-engine"])
def test_every_arm_is_one_fresh_trial_as_the_corpus_ran_it(arm: str, tmp_path: Path) -> None:
    config = config_for(arm, tmp_path if arm == "record-files" else None)
    (agent,) = config["agents"]
    assert agent["import_path"] == REPAIR_AGENT
    assert agent["model_name"] == "anthropic/claude-sonnet-5-5"
    assert agent["kwargs"] == {
        "reasoning_effort": "medium",
        "version": CLAUDE_CODE_VERSION,
        "config": HOOKS,
        "repair_note": "1 previous attempt(s) at this task FAILED ...",
    }
    assert "load_trajectory" not in agent  # a fresh Claude Code session
    assert (config["n_attempts"], config["agent_timeout_multiplier"]) == (1, 0.5)
    assert config["datasets"] == [
        {
            "name": "terminal-bench/terminal-bench",
            "ref": "4.0.0",
            "task_names": ["terminal-bench/mvcc-lsm-compaction"],
        }
    ]
    assert config["environment"]["import_path"] == CORPUS_CONFIG["environment"]["import_path"]
    assert config["job_name"] == f"tb40-exp2-dry1-mvcc-lsm-compaction__PFDAk9Y-{arm}-a1"
    JobConfig.model_validate(config)


def test_only_record_files_mounts_the_bundle_read_only(tmp_path: Path) -> None:
    files = config_for("record-files", tmp_path)
    assert files["environment"]["mounts"] == [
        {
            "type": "bind",
            "source": str(tmp_path.resolve()),
            "target": RECORD_MOUNT,
            "read_only": True,
        }
    ]
    assert JobConfig.model_validate(files).environment.mounts == files["environment"]["mounts"]
    for arm in ("control", "record-engine"):
        assert "mounts" not in config_for(arm)["environment"]
    with pytest.raises(ValueError, match="a bundle exactly when"):
        config_for("record-files")
    with pytest.raises(ValueError, match="a bundle exactly when"):
        config_for("control", tmp_path)


def test_the_arms_configs_differ_only_in_the_note_and_the_mount(tmp_path: Path) -> None:
    configs = {
        arm: config_for(arm, tmp_path if arm == "record-files" else None)
        for arm in ("control", "record-files", "record-engine")
    }
    for config in configs.values():
        config.pop("job_name")
        config["environment"].pop("mounts", None)
    assert configs["control"] == configs["record-files"] == configs["record-engine"]


def test_job_names_count_reruns() -> None:
    assert job_name("r", "x__1", "control", 2) == "r-x__1-control-a2"
    assert job_name("r", "x__1", "control", 2, rerun=1) == "r-x__1-control-a2-r1"


def test_the_query_server_waits_for_harbor_to_make_the_job_dir(tmp_path: Path) -> None:
    root = as_attempt(copy_trial(tmp_path / "corpus-job"))
    corpus = tmp_path / "corpus-store"
    build_trial(root, corpus, images=all_images(), work_dir=tmp_path / "work")
    store = tmp_path / "store"
    seed_store(corpus, store, TRIAL_ID)
    job_dir = tmp_path / "jobs" / "r-x__1-record-engine-a1"
    server = QueryServer(store, job_dir, arm_config("record-engine"), poll_s=0.02)
    server.start()
    try:
        time.sleep(0.1)
        assert not job_dir.exists()  # it never makes the dir `trajlab run` refuses to find
        agent = job_dir / "hello-world__Ab12Cd3" / "agent"
        agent.mkdir(parents=True)
        (agent.parent / "config.json").write_text("{}")
        deadline = time.monotonic() + 10
        while not (agent / "bin" / "query").is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (agent / "bin" / "query").is_file()
        assert (agent / "query_log.jsonl").read_text() == ""
        (agent / "queries" / "q_1_1.sql").write_text("SELECT count(*) AS n FROM trials")
        while not (agent / "queries" / "q_1_1.result").is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (agent / "queries" / "q_1_1.result").read_text() == '{"n":1}\n-- 1 row\n'
    finally:
        server.stop()
    assert not server.thread.is_alive() and server.error is None
    (entry,) = [json.loads(line) for line in (agent / "query_log.jsonl").read_text().splitlines()]
    assert entry["rows"] == 1


def test_the_query_server_stops_before_its_job_dir_exists(tmp_path: Path) -> None:
    server = QueryServer(tmp_path / "store", tmp_path / "never", arm_config("record-engine"), 0.02)
    server.start()
    server.stop()
    assert not server.thread.is_alive()


def test_only_record_engine_serves() -> None:
    with pytest.raises(ValueError, match="serves no queries"):
        QueryServer(Path("s"), Path("j"), arm_config("record-files"))
