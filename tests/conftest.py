import shutil
from pathlib import Path

import pytest

FIXTURE_TRIAL = Path(__file__).parent / "fixtures" / "hello-world-trial"


@pytest.fixture
def fixture_trial() -> Path:
    """The committed hello-world trial dir. Read-only: copy it before writing."""
    return FIXTURE_TRIAL


@pytest.fixture
def trial_copy(tmp_path: Path) -> Path:
    """A writable copy of the hello-world trial dir."""
    return Path(shutil.copytree(FIXTURE_TRIAL, tmp_path / FIXTURE_TRIAL.name))
