"""Load ATIF trajectories from a trial dir into Harbor's `Trajectory` model."""

import logging
from pathlib import Path

from harbor.models.trajectories import Trajectory
from harbor.models.trial.paths import TrialPaths

logger = logging.getLogger(__name__)

# Harbor hardcodes this name in trial.py and each agent; TrialPaths has no property for it.
TRAJECTORY_FILENAME = "trajectory.json"


def trajectory_path(trial_dir: Path) -> Path:
    """Return the path of Harbor's ATIF trajectory inside a trial dir."""
    return TrialPaths(trial_dir).agent_dir / TRAJECTORY_FILENAME


def load_trajectory(path: Path) -> Trajectory:
    """Parse an ATIF trajectory file.

    Raises `FileNotFoundError` if the file is missing and `pydantic.ValidationError` if it is
    not valid ATIF.
    """
    logger.debug("loading trajectory %s", path)
    return Trajectory.model_validate_json(path.read_bytes())


def load_trial_trajectory(trial_dir: Path) -> Trajectory:
    """Parse Harbor's `agent/trajectory.json` for a trial dir."""
    return load_trajectory(trajectory_path(trial_dir))
