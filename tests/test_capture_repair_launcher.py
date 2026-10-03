import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from harbor.models.job.config import JobConfig
from harbor.models.trial.result import TrialResult

from tests.conftest import FIXTURE_TRIAL_NAME, assemble_job_dir, make_checkpoint, write_capture
from trajlab.capture import repair_launcher
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.repair import REPAIR_PROTOCOL
from trajlab.capture.repair_launcher import (
    INPUTS_DIRNAME,
    Launcher,
    RepairJob,
    checkpoint_tag,
    classify_failure,
    final_checkpoint,
    repair_job_config,
    repair_job_name,
    source_reserved_slots,
)
from trajlab.contracts import (
    CHECKPOINT_ARMS,
    REPAIR_ARMS,
    REPAIR_SOURCE_FILENAME,
    SESSION_ARMS,
    FailureKind,
    RepairArm,
    RepairSource,
)

TASK = "hello-world/hello-world"
TAG = f"trajlab-checkpoint:{FIXTURE_TRIAL_NAME}.0002"


def set_result(trial_dir: Path, reward: float | None, exception: str | None = None) -> None:
    path = trial_dir / "result.json"
    data = json.loads(path.read_text())
    data["verifier_result"] = None if reward is None else {"rewards": {"reward": reward}}
    data["exception_info"] = (
        None
        if exception is None
        else {
            "exception_type": exception,
            "exception_message": "",
            "exception_traceback": "",
            "occurred_at": "2026-09-22T00:49:18Z",
        }
    )
    path.write_text(json.dumps(data))


def result_of(trial_dir: Path) -> TrialResult:
    return TrialResult.model_validate_json((trial_dir / "result.json").read_text())


@pytest.fixture
def failed_job(tmp_path: Path) -> Path:
    """The hello-world job with its one trial failed (reward 0) and two checkpoints."""
    job_dir = assemble_job_dir(tmp_path / "jobs")
    trial = job_dir / FIXTURE_TRIAL_NAME
    set_result(trial, 0.0)
    write_capture(trial, [make_checkpoint(1), make_checkpoint(2)])
    return job_dir


def launcher(job_dir: Path, **overrides: Any) -> Launcher:
    fields: dict[str, Any] = {
        "source_jobs": [job_dir],
        "prefix": "rep-v1",
        "attempts": 3,
        "max_running": 6,
        "jobs_dir": job_dir.parent,
        "manifests_dir": job_dir.parent.parent / "manifests",
        "env_file": None,
        "tag_lookup": lambda image_id: TAG,
        "clock": lambda: 1000.0,
    }
    return Launcher(**(fields | overrides))


@pytest.mark.parametrize(
    ("reward", "exception", "kind"),
    [
        (1.0, None, None),
        (1.0, "AgentTimeoutError", None),
        (0.0, None, "ended_turn"),
        (None, None, "infra_error"),
        (0.0, "AgentTimeoutError", "timeout"),
        (None, "AgentTimeoutError", "timeout"),
        (0.0, "OutputTokenExceededError", "agent_error"),
        (None, "ApiUsageLimitError", "infra_error"),
        (None, "CancelledError", "infra_error"),
        (0.0, "NonZeroAgentExitCodeError", "unclassified"),
        (0.0, "SomethingNewError", "unclassified"),
    ],
)
def test_classify_failure(
    trial_copy: Path, reward: float | None, exception: str | None, kind: FailureKind | None
) -> None:
    set_result(trial_copy, reward, exception)
    assert classify_failure(result_of(trial_copy)) == kind


def test_final_checkpoint_is_the_highest_seq(trial_copy: Path) -> None:
    assert final_checkpoint(trial_copy) is None
    write_capture(trial_copy, [make_checkpoint(2), make_checkpoint(1)])
    record = final_checkpoint(trial_copy)
    assert record is not None and record.seq == 2


def source_config() -> dict[str, Any]:
    return {
        "job_name": "tb40-x",
        "jobs_dir": "corpus/jobs",
        "n_attempts": 3,
        "n_concurrent_trials": 4,
        "agent_timeout_multiplier": 0.5,
        "agents": [
            {
                "name": "claude-code",
                "model_name": "anthropic/claude-sonnet-5-5",
                "kwargs": {"reasoning_effort": "medium", "version": CLAUDE_CODE_VERSION},
            }
        ],
        "datasets": [
            {
                "name": "terminal-bench/terminal-bench",
                "ref": "4.0.0",
                "task_names": ["terminal-bench/a", "terminal-bench/b"],
            }
        ],
    }


@pytest.mark.parametrize("arm", REPAIR_ARMS)
def test_repair_job_config_differs_only_by_arm(arm: RepairArm, tmp_path: Path) -> None:
    session = tmp_path / "s.jsonl"
    config = repair_job_config(
        source_config(),
        task_name="terminal-bench/b",
        arm=arm,
        job_name="rep-b-x",
        jobs_dir=Path("corpus/jobs"),
        attempts=3,
        checkpoint_image=TAG if arm in CHECKPOINT_ARMS else None,
        session_file=session if arm in SESSION_ARMS else None,
    )
    JobConfig.model_validate(config)
    assert config["n_attempts"] == 3 and config["n_concurrent_trials"] == 3
    assert config["agent_timeout_multiplier"] == 0.5
    assert config["datasets"] == [
        {
            "name": "terminal-bench/terminal-bench",
            "ref": "4.0.0",
            "task_names": ["terminal-bench/b"],
        }
    ]
    (agent,) = config["agents"]
    assert agent["import_path"] == "trajlab.capture.repair:RepairClaudeCode"
    assert agent["model_name"] == "anthropic/claude-sonnet-5-5"
    assert agent["kwargs"]["reasoning_effort"] == "medium"
    assert agent["kwargs"]["version"] == CLAUDE_CODE_VERSION
    assert ("load_trajectory" in agent) == (arm in SESSION_ARMS)
    environment = config["environment"]
    if arm in CHECKPOINT_ARMS:
        assert environment["kwargs"] == {"checkpoint_image": TAG}
    else:
        assert environment == {
            "import_path": "trajlab.capture.preinstall:PreinstalledDockerEnvironment"
        }


def test_repair_job_config_refuses_missing_inputs() -> None:
    with pytest.raises(ValueError, match="checkpoint image"):
        repair_job_config(
            source_config(),
            task_name="terminal-bench/a",
            arm="state",
            job_name="j",
            jobs_dir=Path("corpus/jobs"),
            attempts=1,
            checkpoint_image=None,
            session_file=None,
        )


def test_plan_writes_four_paired_jobs(failed_job: Path) -> None:
    trial = failed_job / FIXTURE_TRIAL_NAME
    planned = launcher(failed_job).plan_failure(trial, result_of(trial))
    assert isinstance(planned, list)
    assert [job.source.arm for job in planned] == list(REPAIR_ARMS)
    inputs = failed_job.parent / INPUTS_DIRNAME
    for job in planned:
        assert job.name == repair_job_name("rep-v1", FIXTURE_TRIAL_NAME, job.source.arm)
        saved = RepairSource.model_validate_json(
            (inputs / job.name / REPAIR_SOURCE_FILENAME).read_text()
        )
        assert saved == job.source
        assert saved.failure_kind == "ended_turn"
        assert saved.source_reward == 0.0
        assert saved.task_name == TASK
        assert saved.checkpoint_seq == (2 if job.source.arm in CHECKPOINT_ARMS else None)
        copies = list((inputs / job.name).glob("*.jsonl"))
        assert len(copies) == (1 if job.source.arm in SESSION_ARMS else 0)
        config = json.loads(job.config_path.read_text())
        assert config["tasks"] == [{"name": TASK, "ref": "latest"}]


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("passed", "passed"),
        ("usage", "infra_error (ApiUsageLimitError)"),
        ("no-checkpoint", "no checkpoint"),
        ("image-gone", "is gone"),
    ],
)
def test_plan_refuses_unrepairable(failed_job: Path, setup: str, reason: str) -> None:
    trial = failed_job / FIXTURE_TRIAL_NAME
    lookup = (lambda image_id: None) if setup == "image-gone" else (lambda image_id: TAG)
    if setup == "passed":
        set_result(trial, 1.0)
    elif setup == "usage":
        set_result(trial, None, "ApiUsageLimitError")
    elif setup == "no-checkpoint":
        write_capture(trial, [])
    planned = launcher(failed_job, tag_lookup=lookup).plan_failure(trial, result_of(trial))
    assert isinstance(planned, str) and reason in planned
    assert not (failed_job.parent / INPUTS_DIRNAME).exists()


def test_discover_queues_each_failure_once(failed_job: Path) -> None:
    runner = launcher(failed_job)
    runner.discover()
    runner.discover()
    assert len(runner.pending) == 4
    assert runner.queued_trials == {FIXTURE_TRIAL_NAME}


def test_discover_pauses_on_a_usage_limit(failed_job: Path) -> None:
    set_result(failed_job / FIXTURE_TRIAL_NAME, None, "ApiUsageLimitError")
    runner = launcher(failed_job)
    runner.discover()
    assert runner.pending == []
    assert runner.paused_until == 1000.0 + runner.usage_backoff_s


def test_finished_source_reserves_nothing(failed_job: Path) -> None:
    assert source_reserved_slots(failed_job) == 0
    data = json.loads((failed_job / "result.json").read_text())
    data["finished_at"] = None
    data["n_total_trials"] = 3
    (failed_job / "result.json").write_text(json.dumps(data))
    # One of three trials finished; the job's own concurrency (1) caps what it may still use.
    assert source_reserved_slots(failed_job) == 1


class FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.pid = 4242

    def poll(self) -> int | None:
        return self.code


def test_launch_respects_max_running(failed_job: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair_launcher, "commit_new_manifests", lambda _: [])
    launched: list[list[str]] = []

    def popen(command: list[str], **_: Any) -> FakeProcess:
        launched.append(command)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)
    runner = launcher(failed_job, hold_below_gb=0.0, storage="/srv/trajlab/jobs")
    runner.inputs_root.mkdir(parents=True)
    runner.discover()
    runner.launch_ready()
    # 6 slots, 3 attempts per job, no repair trial finished yet: two jobs at once.
    assert len(launched) == 2 and len(runner.pending) == 2
    assert [c[1] for c in launched] == ["run", "run"]
    assert launched[0][-2:] == ["--storage", "/srv/trajlab/jobs"]
    assert sorted(runner.running) == [
        repair_job_name("rep-v1", FIXTURE_TRIAL_NAME, arm) for arm in ("fresh", "state")
    ]
    assert "--corpus-id" in launched[0]


def test_launch_holds_without_watcher(failed_job: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair_launcher, "commit_new_manifests", lambda _: [])
    runner = launcher(failed_job, hold_below_gb=0.0, watcher_running=lambda _: False)
    runner.discover()
    runner.launch_ready()
    assert runner.running == {} and len(runner.pending) == 4


def test_refused_run_is_requeued(failed_job: Path) -> None:
    runner = launcher(failed_job)
    runner.discover()
    job = runner.pending.pop(0)
    runner.running[job.name] = FakeProcess(code=1)  # type: ignore[assignment]
    runner.reap()
    assert [j.name for j in runner.pending][-1] == job.name
    assert runner.refusals == {job.name: 1}


def finished_repair_job(failed_job: Path, job: RepairJob, exception: str | None) -> Path:
    job_dir = assemble_job_dir(failed_job.parent, job.name)
    set_result(job_dir / FIXTURE_TRIAL_NAME, None if exception else 0.0, exception)
    return job_dir


def test_infra_failed_job_is_resumed_after_backoff(failed_job: Path) -> None:
    runner = launcher(failed_job)
    runner.discover()
    job = runner.pending.pop(0)
    finished_repair_job(failed_job, job, "ApiUsageLimitError")
    runner.running[job.name] = FakeProcess(code=0)  # type: ignore[assignment]
    runner.reap()
    assert runner.resume_due == {job.name: 1000.0 + runner.usage_backoff_s}
    assert runner.paused_until == 1000.0 + runner.usage_backoff_s


def test_clean_finished_job_is_left_alone(failed_job: Path) -> None:
    runner = launcher(failed_job)
    runner.discover()
    job = runner.pending.pop(0)
    finished_repair_job(failed_job, job, None)
    runner.running[job.name] = FakeProcess(code=0)  # type: ignore[assignment]
    runner.reap()
    assert runner.resume_due == {}


def test_restart_adopts_existing_jobs_instead_of_rerunning(failed_job: Path) -> None:
    first = launcher(failed_job)
    first.discover()
    for job in first.pending:
        finished_repair_job(failed_job, job, None)
    second = launcher(failed_job)
    second.discover()
    assert second.pending == [] and second.resume_due == {}
    assert second.done()


def test_checkpoint_tag_picks_the_checkpoint_repository() -> None:
    def runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        tags = ["other:latest", TAG]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(tags))

    assert checkpoint_tag("sha256:abc", runner) == TAG

    def missing(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="")

    assert checkpoint_tag("sha256:abc", missing) is None


def test_repair_source_requires_arm_inputs() -> None:
    fields: dict[str, Any] = {
        "source_job": "j",
        "source_trial": "t__1",
        "task_name": TASK,
        "failure_kind": "timeout",
        "arm": "state",
        "attempts": 3,
        "recorded_at": datetime.now(UTC),
    }
    with pytest.raises(ValueError, match="checkpoint_image"):
        RepairSource(**fields)
    with pytest.raises(ValueError, match="not repaired"):
        RepairSource(**fields | {"failure_kind": "infra_error", "checkpoint_image": TAG})
    assert RepairSource(**fields | {"checkpoint_image": TAG}).arm in CHECKPOINT_ARMS


def add_attempt(job_dir: Path, name: str, reward: float) -> Path:
    trial = Path(shutil.copytree(job_dir / FIXTURE_TRIAL_NAME, job_dir / name))
    for path in (trial / "config.json", trial / "result.json"):
        path.write_text(path.read_text().replace(FIXTURE_TRIAL_NAME, name))
    set_result(trial, reward)
    return trial


def three_attempts(failed_job: Path, rewards: tuple[float, float]) -> None:
    config = json.loads((failed_job / "config.json").read_text())
    (failed_job / "config.json").write_text(json.dumps(config | {"n_attempts": 3}))
    for index, reward in enumerate(rewards):
        add_attempt(failed_job, f"hello-world__Extra{index}", reward)


def test_per_task_waits_for_every_attempt(failed_job: Path) -> None:
    data = json.loads((failed_job / "result.json").read_text())
    (failed_job / "result.json").write_text(json.dumps(data | {"finished_at": None}))
    config = json.loads((failed_job / "config.json").read_text())
    (failed_job / "config.json").write_text(json.dumps(config | {"n_attempts": 3}))
    runner = launcher(failed_job, per_task=1)
    runner.inputs_root.mkdir(parents=True)
    runner.discover()
    assert runner.pending == [] and runner.selection == {}


def test_per_task_draws_one_failure_reproducibly(failed_job: Path) -> None:
    three_attempts(failed_job, (0.0, 1.0))
    runner = launcher(failed_job, per_task=1)
    runner.inputs_root.mkdir(parents=True)
    runner.discover()
    (selection,) = runner.selection.values()
    assert selection["candidates"] == ["hello-world__Extra0", FIXTURE_TRIAL_NAME]
    assert selection["not_candidates"] == {"hello-world__Extra1": "passed"}
    (chosen,) = selection["chosen"]
    assert {job.source.source_trial for job in runner.pending} == {chosen}
    assert len(runner.pending) == 4
    other = next(c for c in selection["candidates"] if c != chosen)
    assert runner.skipped[other] == "not drawn"
    # A restarted launcher reads the recorded draw and queues the same jobs.
    again = launcher(failed_job, per_task=1)
    again.discover()
    assert [job.name for job in again.pending] == [job.name for job in runner.pending]


def test_later_round_repairs_the_same_failures_with_its_note(failed_job: Path) -> None:
    three_attempts(failed_job, (0.0, 1.0))
    first = launcher(failed_job, per_task=1)
    first.inputs_root.mkdir(parents=True)
    first.discover()
    (drawn,) = first.selection.values()
    # A different prefix would draw with a different seed; reusing the draw pins the failures.
    second = launcher(
        failed_job,
        prefix="rep-v2",
        per_task=1,
        same_failures_as="rep-v1",
        repair_note=REPAIR_PROTOCOL,
    )
    second.discover()
    (reused,) = second.selection.values()
    assert reused == drawn | {"reused_from": "rep-v1"}
    assert (second.inputs_root / "rep-v2.selection.json").is_file()
    assert {job.source.source_trial for job in second.pending} == set(drawn["chosen"])
    assert all(job.name.startswith("rep-v2-") for job in second.pending)
    for job in second.pending:
        config = json.loads(job.config_path.read_text())
        assert config["agents"][0]["kwargs"]["repair_note"] == REPAIR_PROTOCOL
    for job in first.pending:
        config = json.loads(job.config_path.read_text())
        assert "repair_note" not in config["agents"][0]["kwargs"]


def test_later_round_draws_no_new_task(failed_job: Path) -> None:
    three_attempts(failed_job, (0.0, 1.0))
    runner = launcher(failed_job, prefix="rep-v2", per_task=1, same_failures_as="rep-v1")
    runner.inputs_root.mkdir(parents=True)
    (runner.inputs_root / "rep-v1.selection.json").write_text("{}")
    runner.discover()
    assert runner.selection == {} and runner.pending == []


def test_finished_repair_job_keeps_only_final_images(failed_job: Path) -> None:
    removed: list[str] = []

    def remove(tag: str) -> bool:
        removed.append(tag)
        return True

    runner = launcher(
        failed_job,
        tag_lookup=lambda image_id: f"trajlab-checkpoint:{image_id[-4:]}",
        remove_image=remove,
    )
    runner.discover()
    job = runner.pending.pop(0)
    repair = finished_repair_job(failed_job, job, None)
    write_capture(repair / FIXTURE_TRIAL_NAME, [make_checkpoint(1), make_checkpoint(2)])
    runner.check_finished(job.name)
    assert removed == ["trajlab-checkpoint:0001"]
    pruned = json.loads((repair / FIXTURE_TRIAL_NAME / "agent/checkpoints/pruned.json").read_text())
    assert pruned["kept_seq"] == 2 and [r["seq"] for r in pruned["removed"]] == [1]
    # Pruning happens once per trial.
    runner.check_finished(job.name)
    assert removed == ["trajlab-checkpoint:0001"]
