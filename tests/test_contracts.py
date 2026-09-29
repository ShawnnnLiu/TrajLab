import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from harbor.models.trajectories import Trajectory
from harbor.utils.trajectory_validator import TrajectoryValidator
from pydantic import BaseModel, TypeAdapter, ValidationError

import trajlab.contracts
from trajlab.contracts import (
    CHECKPOINT_STEP_MESSAGE,
    CONTEXT_MANAGEMENT_EXTRA_KEY,
    TRAJLAB_EXTRA_KEY,
    CheckpointRecord,
    CheckpointStepExtra,
    CompactionStepExtra,
    ContextManagementExtra,
    OriginalStepExtra,
    TrajlabStepExtra,
    TrialRecord,
)

FIXTURE_TRAJECTORY = Path(__file__).parent / "fixtures/hello-world-trial/agent/trajectory.json"
FIXTURE_TOOL_CALL_ID = "toolu_01TKn2WsYzAzVaTXotNFqp2Z"
TRIAL_ID = UUID("e1eee115-a864-4c21-896b-5b60c8741d97")


def _checkpoint_record(**overrides: Any) -> CheckpointRecord:
    fields: dict[str, Any] = {
        "checkpoint_id": "sha256:3f1c9e",
        "trial_id": TRIAL_ID,
        "tool_call_id": FIXTURE_TOOL_CALL_ID,
        "seq": 1,
        "tool_name": "Bash",
        "capture_ms": 1840,
        "bytes": 52_428_800,
        "requested_at": datetime(2026, 9, 29, 12, 0, 0, 120000, tzinfo=UTC),
        "captured_at": datetime(2026, 9, 29, 12, 0, 1, 960000, tzinfo=UTC),
    }
    return CheckpointRecord(**fields | overrides)


INSTANCES: list[BaseModel] = [
    _checkpoint_record(),
    _checkpoint_record(bytes=None, path="/var/lib/trajlab/ckpt"),
    TrialRecord(
        trial_id=TRIAL_ID,
        trial_name="hello-world__K3GBok3",
        task_name="hello-world/hello-world",
        job_id=UUID("46eeee6e-8ffa-42b8-81da-b47efcf1407f"),
        job_dir="hello-world-smoke",
        claude_session_id="78b481c6-4620-425b-b2c0-ec60bce7422f",
    ),
    OriginalStepExtra(original_step_id=2),
    CheckpointStepExtra(tool_call_id=FIXTURE_TOOL_CALL_ID),
    CompactionStepExtra(),
    ContextManagementExtra(type="compaction", boundary="replace"),
]


@pytest.mark.parametrize("instance", INSTANCES, ids=lambda m: type(m).__name__)
def test_json_round_trip(instance: BaseModel) -> None:
    dumped = instance.model_dump_json()

    assert type(instance).model_validate_json(dumped) == instance
    assert type(instance).model_validate_json(dumped, strict=True) == instance


def test_checkpoint_record_json_shape() -> None:
    data = json.loads(_checkpoint_record().model_dump_json())

    assert data["trial_id"] == str(TRIAL_ID)
    assert data["backend"] == "docker_commit"
    assert data["physical"] is True
    assert datetime.fromisoformat(data["captured_at"]).tzinfo is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"backend": "statefork"},
        {"physical": False},
        {"seq": 0},
        {"capture_ms": -1},
        {"tool_call_id": ""},
        {"requested_at": datetime(2026, 9, 29, 12, 0)},  # naive: ambiguous on the host
        {"image_tag": "x"},
    ],
    ids=lambda o: next(iter(o)),
)
def test_checkpoint_record_rejects(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _checkpoint_record(**overrides)


def test_records_are_frozen() -> None:
    with pytest.raises(ValidationError):
        _checkpoint_record().seq = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"kind": "original", "original_step_id": 3}, OriginalStepExtra),
        (
            {"kind": "checkpoint", "tool_call_id": "t", "original_step_id": None},
            CheckpointStepExtra,
        ),
        ({"kind": "compaction", "original_step_id": None}, CompactionStepExtra),
    ],
)
def test_trajlab_extra_dispatches_on_kind(data: dict[str, Any], expected: type) -> None:
    assert isinstance(TypeAdapter(TrajlabStepExtra).validate_python(data), expected)


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "restore"},
        {"kind": "checkpoint"},  # no tool_call_id
        {"kind": "checkpoint", "tool_call_id": "t", "original_step_id": 4},
        {"kind": "original"},  # no original_step_id
    ],
)
def test_trajlab_extra_rejects(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(TrajlabStepExtra).validate_python(data)


def test_context_management_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        ContextManagementExtra(type="compaction", boundary="rewind")  # type: ignore[arg-type]


def test_enriched_shape_is_valid_atif(tmp_path: Path) -> None:
    # The ADR-0003 shape, built from these contracts, must pass Harbor's validator and our
    # strict load. Step 7 will build the same thing from checkpoints.jsonl.
    data = json.loads(FIXTURE_TRAJECTORY.read_text())
    record = _checkpoint_record()
    compaction_step = {
        "source": "system",
        "message": "Context compaction performed",
        "observation": {"results": [{"content": "Summary of the conversation so far."}]},
        "extra": {
            TRAJLAB_EXTRA_KEY: CompactionStepExtra().model_dump(mode="json"),
            CONTEXT_MANAGEMENT_EXTRA_KEY: ContextManagementExtra(
                type="compaction", boundary="replace"
            ).model_dump(mode="json"),
        },
    }
    checkpoint_step = {
        "timestamp": record.captured_at.isoformat(),
        "source": "system",
        "message": CHECKPOINT_STEP_MESSAGE,
        "observation": {
            "results": [{"content": record.checkpoint_id, "extra": record.model_dump(mode="json")}]
        },
        "extra": {
            TRAJLAB_EXTRA_KEY: CheckpointStepExtra(tool_call_id=record.tool_call_id).model_dump(
                mode="json"
            )
        },
    }
    original = data["steps"]
    for step in original:
        step["extra"] = (step.get("extra") or {}) | {
            TRAJLAB_EXTRA_KEY: OriginalStepExtra(original_step_id=step["step_id"]).model_dump(
                mode="json"
            )
        }
    # Fixture: step 2 owns the tool call. Compaction goes first to exercise both kinds.
    steps = [original[0], compaction_step, original[1], checkpoint_step, original[2]]
    for new_id, step in enumerate(steps, start=1):
        step["step_id"] = new_id
    data["steps"] = steps
    path = tmp_path / "trajectory.enriched.json"
    path.write_text(json.dumps(data))

    validator = TrajectoryValidator()
    assert validator.validate(path), validator.get_errors()
    trajectory = Trajectory.model_validate_json(path.read_bytes(), strict=True)
    extras = [
        TypeAdapter(TrajlabStepExtra).validate_python(step.extra[TRAJLAB_EXTRA_KEY])
        for step in trajectory.steps
    ]
    assert [e.kind for e in extras] == [
        "original",
        "compaction",
        "original",
        "checkpoint",
        "original",
    ]
    assert [e.original_step_id for e in extras] == [1, None, 2, None, 3]
    checkpoint = trajectory.steps[3].observation.results[0].extra  # type: ignore[union-attr]
    assert CheckpointRecord.model_validate(checkpoint) == record


def test_contracts_import_only_pydantic_and_stdlib() -> None:
    # CLAUDE.md constraint 2: no I/O, no imports from other trajlab modules.
    allowed = {"typing", "uuid", "datetime", "pydantic", "trajlab.contracts"}
    package = Path(trajlab.contracts.__file__).parent
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                permitted = any(name == a or name.startswith(f"{a}.") for a in allowed)
                assert permitted, f"{source.name} imports {name}"
