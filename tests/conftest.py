import json
import shutil
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trajlab.contracts import (
    CALLS_FILENAME,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    POLICY_FILENAME,
    CallRecord,
    CheckpointPolicy,
    CheckpointRecord,
)

FIXTURE_TRIAL = Path(__file__).parent / "fixtures" / "hello-world-trial"
# The job-level files (config.json, lock.json, result.json) of the job FIXTURE_TRIAL came from.
FIXTURE_JOB_FILES = Path(__file__).parent / "fixtures" / "hello-world-job"
FIXTURE_JOB_NAME = "hello-world-smoke"
FIXTURE_TOOL_CALL_ID = "toolu_01TKn2WsYzAzVaTXotNFqp2Z"


@pytest.fixture
def fixture_trial() -> Path:
    """The committed hello-world trial dir. Read-only: copy it before writing."""
    return FIXTURE_TRIAL


@pytest.fixture
def trial_copy(tmp_path: Path) -> Path:
    """A writable copy of the hello-world trial dir."""
    return Path(shutil.copytree(FIXTURE_TRIAL, tmp_path / FIXTURE_TRIAL.name))


def edit_trajectory(trial_dir: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    """Apply `mutate` to a trial dir's agent/trajectory.json in place; return its path."""
    path = trial_dir / "agent" / "trajectory.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    return path


def assemble_job_dir(parent: Path, name: str = FIXTURE_JOB_NAME) -> Path:
    """Build a writable job dir `<parent>/<name>` holding the fixture job files and trial."""
    job_dir = Path(shutil.copytree(FIXTURE_JOB_FILES, parent / name))
    shutil.copytree(FIXTURE_TRIAL, job_dir / "hello-world__K3GBok3")
    return job_dir


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    """A writable copy of the hello-world job dir, with its one trial."""
    return assemble_job_dir(tmp_path / "jobs")


FIXTURE_TRIAL_NAME = "hello-world__K3GBok3"
CAPTURE_POLICY = CheckpointPolicy(every=1, gate="change")
# Just after the fixture's one tool call.
T0 = datetime(2026, 9, 22, 0, 49, 17, 300000, tzinfo=UTC)


def make_checkpoint(
    seq: int, tool_call_id: str = FIXTURE_TOOL_CALL_ID, **overrides: Any
) -> CheckpointRecord:
    """A CheckpointRecord for the fixture trial."""
    fields: dict[str, Any] = {
        "checkpoint_id": f"sha256:{seq:064x}",
        "trial_name": FIXTURE_TRIAL_NAME,
        "tool_call_id": tool_call_id,
        "seq": seq,
        "covered_tool_call_ids": (tool_call_id,),
        "tool_name": "Write",
        "capture_ms": 4000,
        "bytes": 1024,
        "requested_at": T0 + timedelta(seconds=seq),
        "captured_at": T0 + timedelta(seconds=seq, milliseconds=500),
    }
    return CheckpointRecord(**fields | overrides)


def make_call(
    call_seq: int, tool_call_id: str = FIXTURE_TOOL_CALL_ID, **overrides: Any
) -> CallRecord:
    """A CallRecord for the fixture trial; outcome checkpoint by default."""
    fields: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "trial_name": FIXTURE_TRIAL_NAME,
        "tool_name": "Write",
        "call_seq": call_seq,
        "outcome": "checkpoint",
        "change": "baseline",
        "checkpoint_seq": call_seq,
        "requested_at": T0 + timedelta(seconds=call_seq),
        "answered_at": T0 + timedelta(seconds=call_seq, milliseconds=600),
    }
    return CallRecord(**fields | overrides)


def write_capture(
    trial_dir: Path,
    checkpoints: Sequence[CheckpointRecord] = (),
    calls: Sequence[CallRecord] = (),
    timeouts: Sequence[str] = (),
    policy: CheckpointPolicy | None = CAPTURE_POLICY,
) -> Path:
    """Write the files the hook and watcher leave under agent/checkpoints/."""
    directory = trial_dir / "agent" / CHECKPOINTS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    if policy is not None:
        (directory / POLICY_FILENAME).write_text(policy.model_dump_json() + "\n")
    (directory / CHECKPOINT_RECORDS_FILENAME).write_text(
        "".join(r.model_dump_json() + "\n" for r in checkpoints)
    )
    (directory / CALLS_FILENAME).write_text("".join(c.model_dump_json() + "\n" for c in calls))
    for call_id in timeouts:
        (directory / f"{call_id}.req").write_text("{}")
        (directory / f"{call_id}.timeout").write_text("")
    return directory
