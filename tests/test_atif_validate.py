from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.conftest import FIXTURE_TOOL_CALL_ID, edit_trajectory
from trajlab.atif.load import trajectory_path
from trajlab.atif.validate import validate_trajectory
from trajlab.cli import app


def _break_two_fields(data: dict[str, Any]) -> None:
    data["steps"][1]["source"] = "robot"
    del data["agent"]["name"]


def _append_system_step(data: dict[str, Any], **fields: Any) -> None:
    """Append a system step shaped like a postprocess checkpoint step."""
    data["steps"].append(
        {
            "step_id": len(data["steps"]) + 1,
            "timestamp": "2026-09-29T12:00:00.123456+00:00",
            "source": "system",
            "message": "checkpoint",
            "observation": {"results": [{"content": "checkpoint sha256:abc"}]},
            "extra": {"checkpoint": {"tool_call_id": FIXTURE_TOOL_CALL_ID}},
            **fields,
        }
    )


def test_fixture_is_valid(fixture_trial: Path) -> None:
    assert validate_trajectory(trajectory_path(fixture_trial)) == []


def test_checkpoint_shaped_system_step_is_valid(trial_copy: Path) -> None:
    # The shape step 7 inserts: system step, observation without source_call_id, link in extra.
    path = edit_trajectory(trial_copy, _append_system_step)

    assert validate_trajectory(path) == []


def test_collects_every_field_error(trial_copy: Path) -> None:
    path = edit_trajectory(trial_copy, _break_two_fields)

    errors = validate_trajectory(path)

    assert len(errors) == 2
    assert any("agent.name" in error for error in errors)
    assert any("steps.1.source" in error for error in errors)


# The ATIF rules step 7 (postprocess) must respect when inserting system steps.
STEP_7_RULES: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "step ids have a gap": (
        lambda data: data["steps"][1].update(step_id=7),
        "expected 2 (sequential from 1), got 7",
    ),
    "source_call_id points at another step": (
        lambda data: _append_system_step(
            data,
            observation={"results": [{"source_call_id": FIXTURE_TOOL_CALL_ID, "content": "x"}]},
        ),
        f"source_call_id '{FIXTURE_TOOL_CALL_ID}' which is not found in step 4",
    ),
    "system step has tool_calls": (
        lambda data: _append_system_step(
            data,
            tool_calls=[{"tool_call_id": "t", "function_name": "f", "arguments": {}}],
        ),
        "'tool_calls' is only applicable when source is 'agent'",
    ),
    "system step has metrics": (
        lambda data: _append_system_step(data, metrics={"prompt_tokens": 1}),
        "'metrics' is only applicable when source is 'agent'",
    ),
    "system step has model_name": (
        lambda data: _append_system_step(data, model_name="m"),
        "'model_name' is only applicable when source is 'agent'",
    ),
    "system step has reasoning_content": (
        lambda data: _append_system_step(data, reasoning_content="r"),
        "'reasoning_content' is only applicable when source is 'agent'",
    ),
    "unknown key on a step": (
        lambda data: _append_system_step(data, checkpoint_id="sha256:abc"),
        "steps.3.checkpoint_id: unexpected field",
    ),
    "timestamp is not ISO 8601": (
        lambda data: _append_system_step(data, timestamp="1759147200"),
        "Invalid ISO 8601 timestamp",
    ),
    "type coercion (strict)": (
        lambda data: data["steps"][0].update(step_id="1"),
        "strict: trajectory.steps.0.step_id",
    ),
}


@pytest.mark.parametrize("rule", STEP_7_RULES)
def test_rejects(trial_copy: Path, rule: str) -> None:
    mutate, expected = STEP_7_RULES[rule]
    path = edit_trajectory(trial_copy, mutate)
    before = path.read_bytes()

    errors = validate_trajectory(path)

    assert len(errors) == 1
    assert expected in errors[0]
    assert path.read_bytes() == before


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


def test_cli_invalid_exits_one_and_keeps_file(trial_copy: Path) -> None:
    path = edit_trajectory(trial_copy, _break_two_fields)
    before = path.read_bytes()

    result = CliRunner().invoke(app, ["validate", str(trial_copy)])

    assert result.exit_code == 1
    assert "agent.name" in result.output
    assert "steps.1.source" in result.output
    assert path.read_bytes() == before


def test_cli_missing_trajectory_exits_one(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["validate", str(tmp_path)])

    assert result.exit_code == 1
    assert "File not found" in result.output
