import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURE_TRIAL = Path(__file__).parent / "fixtures" / "hello-world-trial"
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
