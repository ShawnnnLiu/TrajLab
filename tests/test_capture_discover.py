import json
from pathlib import Path
from uuid import UUID

import pytest

from trajlab.capture.discover import (
    TrialNotFinishedError,
    claude_session_id,
    compose_project_name,
    iter_trial_dirs,
    trial_record,
)

TRIAL_ID = UUID("e1eee115-a864-4c21-896b-5b60c8741d97")
JOB_ID = UUID("46eeee6e-8ffa-42b8-81da-b47efcf1407f")
CLAUDE_SESSION_ID = "78b481c6-4620-425b-b2c0-ec60bce7422f"


def test_iter_trial_dirs_yields_only_dirs_with_trial_config(job_dir: Path) -> None:
    (job_dir / "not-a-trial").mkdir()
    (job_dir / "job.log").write_text("")
    assert [p.name for p in iter_trial_dirs(job_dir)] == ["hello-world__K3GBok3"]


def test_compose_project_name_uses_environment_session_id(fixture_trial: Path) -> None:
    # Harbor 0.23.0 names the environment `<trial_name>__env`, then lowercases it.
    assert compose_project_name(fixture_trial) == "hello-world__k3gbok3__env"


def test_trial_record_joins_harbor_and_claude_ids(job_dir: Path) -> None:
    record = trial_record(job_dir / "hello-world__K3GBok3")
    assert record.trial_id == TRIAL_ID
    assert record.trial_name == "hello-world__K3GBok3"
    assert record.task_name == "hello-world/hello-world"
    assert record.job_id == JOB_ID
    assert record.job_dir == job_dir.name
    assert record.claude_session_id == CLAUDE_SESSION_ID


def test_trial_record_refuses_unfinished_trial(trial_copy: Path) -> None:
    (trial_copy / "result.json").unlink()
    with pytest.raises(TrialNotFinishedError, match="no result.json"):
        trial_record(trial_copy)


def test_trial_record_refuses_mismatched_trial_name(trial_copy: Path) -> None:
    path = trial_copy / "config.json"
    config = json.loads(path.read_text())
    config["trial_name"] = "hello-world__other"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="trial_name"):
        trial_record(trial_copy)


def test_claude_session_id_absent_without_trajectory(trial_copy: Path) -> None:
    (trial_copy / "agent" / "trajectory.json").unlink()
    assert claude_session_id(trial_copy) is None
    assert trial_record(trial_copy).claude_session_id is None
