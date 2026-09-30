"""Find trial dirs in a job dir and derive the ids that name each trial.

Harbor writes a trial's `config.json` when the trial starts and its `result.json` (which holds
`trial_id`) only when it ends, so a running trial has a name and a compose project but no
trial id yet.
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult

from trajlab.contracts import TrialRecord

logger = logging.getLogger(__name__)

# Harbor's Trial passes `session_id=f"{trial_name}__env"` to the agent environment
# (harbor/trial/trial.py, _init_agent_environment); DockerEnvironment sanitizes it into the
# compose project name. Not `__agent`, which is the agent's session id.
ENVIRONMENT_SESSION_SUFFIX = "__env"


class TrialNotFinishedError(ValueError):
    """A trial dir has no result.json: the trial is still running or was killed."""


def iter_trial_dirs(job_dir: Path) -> Iterator[Path]:
    """Yield the trial dirs of a job dir, sorted by name.

    A trial dir is a direct subdirectory with Harbor's trial `config.json`.
    """
    for child in sorted(job_dir.iterdir()):
        if child.is_dir() and TrialPaths(child).config_path.is_file():
            yield child


def load_trial_config(trial_dir: Path) -> TrialConfig:
    return TrialConfig.model_validate_json(TrialPaths(trial_dir).config_path.read_text())


def load_trial_result(trial_dir: Path) -> TrialResult:
    path = TrialPaths(trial_dir).result_path
    if not path.is_file():
        raise TrialNotFinishedError(f"{trial_dir}: no result.json; trial unfinished or killed")
    return TrialResult.model_validate_json(path.read_text())


def environment_session_id(trial_name: str) -> str:
    """Harbor's environment session id for a trial (glossary: `session_id`)."""
    return f"{trial_name}{ENVIRONMENT_SESSION_SUFFIX}"


def compose_project_name(trial_dir: Path) -> str:
    """The Docker Compose project Harbor names for this trial's environment.

    The main container carries the labels `com.docker.compose.project=<this>` and
    `com.docker.compose.service=main`; look it up by label, never by building its name.
    """
    trial_name = load_trial_config(trial_dir).trial_name
    return _sanitize_docker_compose_project_name(environment_session_id(trial_name))


def claude_session_id(trial_dir: Path) -> str | None:
    """The Claude Code session id: the root `session_id` of Harbor's trajectory.json.

    Read as plain JSON: loading the trajectory as ATIF is the `atif` module's job, and capture
    does not import it (CLAUDE.md constraint 3).
    """
    path = TrialPaths(trial_dir).agent_dir / "trajectory.json"
    if not path.is_file():
        return None
    session_id = json.loads(path.read_text()).get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def trial_record(trial_dir: Path) -> TrialRecord:
    """Identify a finished trial. Raises `TrialNotFinishedError` without result.json."""
    config = load_trial_config(trial_dir)
    result = load_trial_result(trial_dir)
    if result.trial_name != config.trial_name:
        raise ValueError(
            f"{trial_dir}: trial_name {result.trial_name!r} in result.json "
            f"!= {config.trial_name!r} in config.json"
        )
    return TrialRecord(
        trial_id=result.id,
        trial_name=config.trial_name,
        task_name=result.task_name,
        job_id=config.job_id,
        job_dir=trial_dir.parent.name,
        claude_session_id=claude_session_id(trial_dir),
    )
