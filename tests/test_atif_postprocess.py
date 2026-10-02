import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from tests.conftest import (
    CAPTURE_POLICY,
    FIXTURE_TOOL_CALL_ID,
    T0,
    edit_trajectory,
    make_call,
    make_checkpoint,
    write_capture,
)
from trajlab.atif.load import load_trajectory
from trajlab.atif.postprocess import (
    CaptureRecords,
    PostprocessError,
    enrich,
    enriched_trajectory_path,
    postprocess_trial,
)
from trajlab.atif.validate import validate_trajectory
from trajlab.contracts import (
    CHECKPOINT_RECORDS_FILENAME,
    TRAJLAB_EXTRA_KEY,
    CheckpointRecord,
    CheckpointStepExtra,
    EnrichedTrajectoryExtra,
    OriginalStepExtra,
    TrajlabStepExtra,
)

STOP_ID = "stop_1790907280_166"
SECOND_CALL_ID = "toolu_01SecondCallInTheSameStep"


def _trajlab(step: Any) -> Any:
    return TypeAdapter(TrajlabStepExtra).validate_python(step.extra[TRAJLAB_EXTRA_KEY])


def _kinds(trial_dir: Path) -> list[str]:
    enriched = load_trajectory(enriched_trajectory_path(trial_dir))
    return [_trajlab(step).kind for step in enriched.steps]


def _root(trial_dir: Path) -> EnrichedTrajectoryExtra:
    enriched = load_trajectory(enriched_trajectory_path(trial_dir))
    return EnrichedTrajectoryExtra.model_validate(enriched.extra[TRAJLAB_EXTRA_KEY])


def test_stock_trial_copies_every_step(trial_copy: Path) -> None:
    source = trial_copy / "agent" / "trajectory.json"
    before = source.read_bytes()

    result = postprocess_trial(trial_copy)

    assert source.read_bytes() == before
    assert result.path == trial_copy / "agent" / "trajectory.enriched.json"
    assert validate_trajectory(result.path) == []
    original = load_trajectory(source)
    enriched = load_trajectory(result.path)
    assert [_trajlab(s) for s in enriched.steps] == [
        OriginalStepExtra(original_step_id=n) for n in (1, 2, 3)
    ]
    # Harbor's own step content and extra keys are untouched.
    for old, new in zip(original.steps, enriched.steps, strict=True):
        new_extra = dict(new.extra or {})
        del new_extra[TRAJLAB_EXTRA_KEY]
        assert new.model_copy(update={"extra": new_extra or None}) == old
    root = _root(trial_copy)
    assert root.policy is None
    assert root.source_sha256 == hashlib.sha256(before).hexdigest()
    assert enriched.final_metrics.total_steps == 3  # type: ignore[union-attr]
    assert (result.checkpoints, result.compactions) == (0, 0)


def test_checkpoint_step_follows_owning_agent_step(trial_copy: Path) -> None:
    record = make_checkpoint(1)
    call = make_call(1)
    write_capture(trial_copy, [record], [call])

    result = postprocess_trial(trial_copy)

    enriched = load_trajectory(result.path)
    assert [s.source for s in enriched.steps] == ["user", "agent", "system", "agent"]
    assert [s.step_id for s in enriched.steps] == [1, 2, 3, 4]
    assert _kinds(trial_copy) == ["original", "original", "checkpoint", "original"]
    step = enriched.steps[2]
    assert _trajlab(step) == CheckpointStepExtra(tool_call_id=FIXTURE_TOOL_CALL_ID)
    assert step.message == "checkpoint"
    assert step.observation.results[0].content == record.checkpoint_id  # type: ignore[union-attr]
    assert (
        CheckpointRecord.model_validate(step.observation.results[0].extra)  # type: ignore[union-attr]
        == record
    )
    owner = _trajlab(enriched.steps[1])
    assert owner.original_step_id == 2
    assert owner.calls == (call,)
    assert _root(trial_copy).policy == CAPTURE_POLICY
    assert enriched.final_metrics.total_steps == 4  # type: ignore[union-attr]


def test_unchanged_call_has_record_but_no_checkpoint_step(trial_copy: Path) -> None:
    call = make_call(1, outcome="unchanged", change="unchanged", checkpoint_seq=None)
    write_capture(trial_copy, [], [call])

    postprocess_trial(trial_copy)

    assert _kinds(trial_copy) == ["original"] * 3
    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    assert _trajlab(enriched.steps[1]).calls == (call,)


def test_stop_checkpoint_goes_last_and_stop_call_on_root(trial_copy: Path) -> None:
    stop = make_checkpoint(
        2,
        STOP_ID,
        trigger="stop",
        tool_name="Stop",
        captured_at=T0 + timedelta(seconds=10),
    )
    stop_call = make_call(2, STOP_ID, trigger="stop", tool_name="Stop", change="changed")
    write_capture(trial_copy, [make_checkpoint(1), stop], [make_call(1), stop_call])

    postprocess_trial(trial_copy)

    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    assert _kinds(trial_copy) == ["original", "original", "checkpoint", "original", "checkpoint"]
    assert _trajlab(enriched.steps[-1]).tool_call_id == STOP_ID
    assert _root(trial_copy).stop_calls == (stop_call,)


def test_unchanged_stop_is_recorded_on_root_only(trial_copy: Path) -> None:
    stop_call = make_call(
        2,
        STOP_ID,
        trigger="stop",
        tool_name="Stop",
        outcome="unchanged",
        change="unchanged",
        checkpoint_seq=1,
    )
    write_capture(trial_copy, [make_checkpoint(1)], [make_call(1), stop_call])

    postprocess_trial(trial_copy)

    assert _kinds(trial_copy) == ["original", "original", "checkpoint", "original"]
    assert _root(trial_copy).stop_calls == (stop_call,)


def test_timeouts_are_recorded(trial_copy: Path) -> None:
    write_capture(trial_copy, timeouts=[FIXTURE_TOOL_CALL_ID, STOP_ID])

    postprocess_trial(trial_copy)

    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    assert _trajlab(enriched.steps[1]).timed_out_tool_call_ids == (FIXTURE_TOOL_CALL_ID,)
    assert _trajlab(enriched.steps[1]).calls == ()
    assert _root(trial_copy).timed_out_stop_ids == (STOP_ID,)


def test_timeout_with_a_record_keeps_the_record(trial_copy: Path) -> None:
    # The hook gave up just as the watcher answered (docs/checkpoint-protocol.md).
    write_capture(trial_copy, [make_checkpoint(1)], [make_call(1)], timeouts=[FIXTURE_TOOL_CALL_ID])

    postprocess_trial(trial_copy)

    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    assert _trajlab(enriched.steps[1]).timed_out_tool_call_ids == ()
    assert _trajlab(enriched.steps[1]).calls == (make_call(1),)


def _add_second_call(data: dict[str, Any]) -> None:
    step = data["steps"][1]
    step["tool_calls"].append(
        {"tool_call_id": SECOND_CALL_ID, "function_name": "Bash", "arguments": {"command": "ls"}}
    )
    step["observation"]["results"].append({"source_call_id": SECOND_CALL_ID, "content": "x"})


def test_parallel_calls_keep_tool_call_order_and_checkpoint_seq(trial_copy: Path) -> None:
    edit_trajectory(trial_copy, _add_second_call)
    # The second call was answered first; every-N covers both in one checkpoint, then the
    # first call got its own (seq order is capture order).
    first = make_checkpoint(1, SECOND_CALL_ID, tool_name="Bash")
    second = make_checkpoint(2)
    calls = [make_call(2), make_call(1, SECOND_CALL_ID, tool_name="Bash")]
    write_capture(trial_copy, [second, first], calls)

    postprocess_trial(trial_copy)

    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    kinds = _kinds(trial_copy)
    assert kinds == ["original", "original", "checkpoint", "checkpoint", "original"]
    seqs = [s.observation.results[0].extra["seq"] for s in enriched.steps[2:4]]  # type: ignore[union-attr,index]
    assert seqs == [1, 2]
    assert [c.tool_call_id for c in _trajlab(enriched.steps[1]).calls] == [
        FIXTURE_TOOL_CALL_ID,
        SECOND_CALL_ID,
    ]


@pytest.mark.parametrize(
    "capture",
    [
        {"checkpoints": [make_checkpoint(1, "toolu_nowhere")]},
        {"calls": [make_call(1, "toolu_nowhere")]},
        {"timeouts": ["toolu_nowhere"]},
        {"checkpoints": [make_checkpoint(1, trial_name="other__trial")]},
        {"calls": [make_call(1, trial_name="other__trial")]},
    ],
    ids=["checkpoint", "call", "timeout", "checkpoint-trial", "call-trial"],
)
def test_unjoinable_records_fail_without_writing(trial_copy: Path, capture: dict[str, Any]) -> None:
    postprocess_trial(trial_copy)
    earlier = enriched_trajectory_path(trial_copy).read_bytes()
    write_capture(trial_copy, **capture)

    with pytest.raises(PostprocessError):
        postprocess_trial(trial_copy)

    assert enriched_trajectory_path(trial_copy).read_bytes() == earlier
    assert not list((trial_copy / "agent").glob("*.tmp"))


def test_malformed_record_names_file_and_line(trial_copy: Path) -> None:
    directory = write_capture(trial_copy, [make_checkpoint(1)])
    with (directory / CHECKPOINT_RECORDS_FILENAME).open("a") as stream:
        stream.write('{"checkpoint_id": "x"}\n')

    with pytest.raises(PostprocessError, match=r"checkpoints\.jsonl:2: not a CheckpointRecord"):
        postprocess_trial(trial_copy)


def test_rerun_is_byte_identical(trial_copy: Path) -> None:
    write_capture(trial_copy, [make_checkpoint(1)], [make_call(1)])

    first = postprocess_trial(trial_copy).path.read_bytes()
    second = postprocess_trial(trial_copy).path.read_bytes()

    assert first == second


def test_enrich_refuses_an_already_enriched_trajectory(trial_copy: Path) -> None:
    postprocess_trial(trial_copy)
    enriched = load_trajectory(enriched_trajectory_path(trial_copy))

    with pytest.raises(PostprocessError, match="already has extra.trajlab"):
        enrich(enriched, CaptureRecords(), [], source_sha256="0" * 64)


def test_missing_trajectory_raises(trial_copy: Path) -> None:
    (trial_copy / "agent" / "trajectory.json").unlink()

    with pytest.raises(FileNotFoundError):
        postprocess_trial(trial_copy)


def test_enriched_file_is_indented_like_harbors(trial_copy: Path) -> None:
    path = postprocess_trial(trial_copy).path

    text = path.read_text()
    assert text.startswith('{\n  "schema_version"')
    assert json.loads(text)["schema_version"] == "ATIF-v1.7"
