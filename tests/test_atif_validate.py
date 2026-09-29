import json
from pathlib import Path

from typer.testing import CliRunner

from trajlab.atif.load import trajectory_path
from trajlab.atif.validate import validate_trajectory
from trajlab.cli import app


def _corrupt(trial_dir: Path) -> None:
    path = trajectory_path(trial_dir)
    data = json.loads(path.read_text())
    data["steps"][1]["source"] = "robot"
    del data["agent"]["name"]
    path.write_text(json.dumps(data))


def test_fixture_is_valid(fixture_trial: Path) -> None:
    assert validate_trajectory(trajectory_path(fixture_trial)) == []


def test_collects_every_error(trial_copy: Path) -> None:
    _corrupt(trial_copy)

    errors = validate_trajectory(trajectory_path(trial_copy))

    assert len(errors) == 2
    assert any("agent.name" in error for error in errors)
    assert any("steps.1.source" in error for error in errors)


def test_rejects_non_sequential_step_ids(trial_copy: Path) -> None:
    # Postprocess must renumber after inserting system steps; this is the check it relies on.
    path = trajectory_path(trial_copy)
    data = json.loads(path.read_text())
    data["steps"][1]["step_id"] = 7
    path.write_text(json.dumps(data))

    errors = validate_trajectory(path)

    assert len(errors) == 1
    assert "step_id" in errors[0]


def test_invalid_json(trial_copy: Path) -> None:
    trajectory_path(trial_copy).write_text("{not json")

    errors = validate_trajectory(trajectory_path(trial_copy))

    assert len(errors) == 1
    assert errors[0].startswith("Invalid JSON")


def test_missing_file(tmp_path: Path) -> None:
    assert validate_trajectory(trajectory_path(tmp_path)) == [
        f"File not found: {trajectory_path(tmp_path)}"
    ]


def test_cli_valid_exits_zero(fixture_trial: Path) -> None:
    result = CliRunner().invoke(app, ["validate", str(fixture_trial)])

    assert result.exit_code == 0
    assert "valid:" in result.output


def test_cli_invalid_exits_one(trial_copy: Path) -> None:
    _corrupt(trial_copy)

    result = CliRunner().invoke(app, ["validate", str(trial_copy)])

    assert result.exit_code == 1
    assert "agent.name" in result.output


def test_cli_missing_trajectory_exits_one(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["validate", str(tmp_path)])

    assert result.exit_code == 1
    assert "File not found" in result.output
