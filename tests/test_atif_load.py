import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from trajlab.atif.load import load_trajectory, load_trial_trajectory, trajectory_path


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
    assert tool_call_ids == ["toolu_01TKn2WsYzAzVaTXotNFqp2Z"]


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_trial_trajectory(tmp_path)


def test_load_invalid_atif_raises(trial_copy: Path) -> None:
    path = trajectory_path(trial_copy)
    data = json.loads(path.read_text())
    data["steps"][0]["source"] = "robot"
    path.write_text(json.dumps(data))

    with pytest.raises(ValidationError):
        load_trajectory(path)
