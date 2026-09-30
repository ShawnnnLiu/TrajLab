import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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
