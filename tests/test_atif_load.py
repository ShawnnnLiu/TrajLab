from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.conftest import FIXTURE_TOOL_CALL_ID, edit_trajectory
from trajlab.atif.load import (
    TrajectoryLoadError,
    load_trajectory,
    load_trial_trajectory,
    trajectory_path,
)


def test_trajectory_path_points_at_agent_dir(fixture_trial: Path) -> None:
    assert trajectory_path(fixture_trial) == fixture_trial / "agent" / "trajectory.json"


def test_load_fixture(fixture_trial: Path) -> None:
    trajectory = load_trial_trajectory(fixture_trial)

    # Harbor's Claude Code converter stamps v1.7 (docs/harbor-facts.md).
    assert trajectory.schema_version == "ATIF-v1.7"
    assert trajectory.agent.name == "claude-code"
    assert trajectory.session_id == "78b481c6-4620-425b-b2c0-ec60bce7422f"
    assert [step.step_id for step in trajectory.steps] == [1, 2, 3]
    assert [step.source for step in trajectory.steps] == ["user", "agent", "agent"]


def test_load_exposes_tool_call_ids(fixture_trial: Path) -> None:
    trajectory = load_trial_trajectory(fixture_trial)

    tool_call_ids = [
        call.tool_call_id for step in trajectory.steps for call in step.tool_calls or []
    ]
    assert tool_call_ids == [FIXTURE_TOOL_CALL_ID]


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_trial_trajectory(tmp_path)


def test_load_error_names_file_and_every_path(trial_copy: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["steps"][0]["source"] = "robot"
        del data["agent"]["name"]

    path = edit_trajectory(trial_copy, mutate)

    with pytest.raises(TrajectoryLoadError) as excinfo:
        load_trajectory(path)

    assert excinfo.value.path == path
    assert str(path) in str(excinfo.value)
    assert sorted(line.split(":")[0] for line in excinfo.value.errors) == [
        "trajectory.agent.name",
        "trajectory.steps.0.source",
    ]
    assert isinstance(excinfo.value.__cause__, ValidationError)


def test_load_is_strict(trial_copy: Path) -> None:
    # Harbor's lax validator coerces "1" to 1; our load refuses.
    path = edit_trajectory(trial_copy, lambda data: data["steps"][0].update(step_id="1"))

    with pytest.raises(TrajectoryLoadError) as excinfo:
        load_trajectory(path)

    assert [line.split(":")[0] for line in excinfo.value.errors] == ["trajectory.steps.0.step_id"]


def test_load_invalid_json_raises(trial_copy: Path) -> None:
    path = trajectory_path(trial_copy)
    path.write_text("{not json")

    with pytest.raises(TrajectoryLoadError):
        load_trajectory(path)


def test_failed_load_leaves_file_untouched(trial_copy: Path) -> None:
    path = edit_trajectory(trial_copy, lambda data: data["steps"][0].update(source="robot"))
    before = path.read_bytes()

    with pytest.raises(TrajectoryLoadError):
        load_trajectory(path)

    assert path.read_bytes() == before
