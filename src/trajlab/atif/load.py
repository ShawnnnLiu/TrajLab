"""Load ATIF trajectories from a trial dir into Harbor's `Trajectory` model.

Loading is strict: pydantic's strict mode refuses type coercion (e.g. `"step_id": "1"`), which
Harbor's own validator would accept. Loading only reads; a failure never touches the file.
"""

import logging
from pathlib import Path

from harbor.models.trajectories import Trajectory
from harbor.models.trial.paths import TrialPaths
from pydantic import ValidationError

logger = logging.getLogger(__name__)

# Harbor hardcodes this name in trial.py and each agent; TrialPaths has no property for it.
TRAJECTORY_FILENAME = "trajectory.json"


class TrajectoryLoadError(ValueError):
    """A trajectory file is not strictly valid ATIF. `errors` holds one line per error path."""

    def __init__(self, path: Path, errors: list[str]) -> None:
        self.path = path
        self.errors = errors
        super().__init__(f"{path}: {len(errors)} error(s)\n" + "\n".join(errors))


def trajectory_path(trial_dir: Path) -> Path:
    """Return the path of Harbor's ATIF trajectory inside a trial dir."""
    return TrialPaths(trial_dir).agent_dir / TRAJECTORY_FILENAME


def format_validation_errors(error: ValidationError) -> list[str]:
    """Render each pydantic error as `trajectory.<loc>: <msg>`, matching Harbor's validator."""
    return [
        f"trajectory.{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
        if e["loc"]
        else f"trajectory: {e['msg']}"
        for e in error.errors()
    ]


def load_trajectory(path: Path) -> Trajectory:
    """Strictly parse an ATIF trajectory file.

    Raises `FileNotFoundError` if the file is missing and `TrajectoryLoadError` (chained to the
    pydantic `ValidationError`) if it is not strictly valid ATIF.
    """
    logger.debug("loading trajectory %s", path)
    try:
        return Trajectory.model_validate_json(path.read_bytes(), strict=True)
    except ValidationError as e:
        raise TrajectoryLoadError(path, format_validation_errors(e)) from e


def load_trial_trajectory(trial_dir: Path) -> Trajectory:
    """Strictly parse Harbor's `agent/trajectory.json` for a trial dir."""
    return load_trajectory(trajectory_path(trial_dir))
