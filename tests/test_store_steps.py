"""Step rows from enriched trajectories (ADR-0014).

The fixture test goes through postprocess on disk. The synthetic tests build trajectories from
the fixture's prompt step plus hand-written steps, and enrich them with postprocess's own
`enrich`, so the rows are built from the shape postprocess really writes.
"""

import copy
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from harbor.models.trajectories import ObservationResult, Trajectory

from tests.conftest import (
    CAPTURE_POLICY,
    FIXTURE_TOOL_CALL_ID,
    FIXTURE_TRIAL,
    T0,
    make_call,
    make_checkpoint,
    write_capture,
)
from trajlab.atif.load import load_trajectory, trajectory_path
from trajlab.atif.postprocess import (
    CaptureRecords,
    enrich,
    enriched_trajectory_path,
    postprocess_trial,
)
from trajlab.capture import transcript
from trajlab.contracts import (
    CallRecord,
    CheckpointPolicy,
    CheckpointRecord,
    CompactionRecord,
    StepRow,
)
from trajlab.store.steps import StepsError, TrialSteps, build_steps, seen_output

TRIAL_ID = UUID("e1eee115-a864-4c21-896b-5b60c8741d97")
STOP_ID = "stop_1790000000_42"
PROMPT = 'Create a file called hello.txt with "Hello, world!" as the content.\n'
FIXTURE_DATA = json.loads((FIXTURE_TRIAL / "agent" / "trajectory.json").read_text())
_FIXTURE_RESULT = FIXTURE_DATA["steps"][1]["observation"]["results"][0]
# What the agent saw: Claude Code's tool_result. Harbor's `content` adds a `[metadata]` line.
FIXTURE_OUTPUT = _FIXTURE_RESULT["extra"]["tool_result_metadata"]["raw_tool_result"]["content"]
assert FIXTURE_OUTPUT.startswith("File created successfully at: /app/hello.txt (")
assert "[metadata]" in _FIXTURE_RESULT["content"] and "[metadata]" not in FIXTURE_OUTPUT


def image(seq: int) -> str:
    return f"sha256:{seq:064x}"  # make_checkpoint's checkpoint_id


# -- the fixture -------------------------------------------------------------------------------


def capture_fixture(trial_dir: Path) -> None:
    """The Write call checkpointed as seq 1, then a stop that changed files, as seq 2."""
    stop = make_checkpoint(2, STOP_ID, trigger="stop", tool_name="Stop")
    stop_call = make_call(2, STOP_ID, trigger="stop", tool_name="Stop", change="changed")
    write_capture(trial_dir, [make_checkpoint(1), stop], [make_call(1), stop_call])
    postprocess_trial(trial_dir)


def test_fixture_rows(trial_copy: Path) -> None:
    capture_fixture(trial_copy)

    steps = build_steps(load_trajectory(enriched_trajectory_path(trial_copy)), trial_id=TRIAL_ID)

    assert steps.prompt == PROMPT
    assert steps.rows == (
        StepRow(
            trial_id=TRIAL_ID,
            step_idx=1,
            atif_step_id=2,
            source="agent",
            tool_call_id=FIXTURE_TOOL_CALL_ID,
            tool="Write",
            args_json='{"content": "Hello, world!", "file_path": "/app/hello.txt"}',
            output=FIXTURE_OUTPUT,
            message="I'll create the file now.",
            t_start=datetime(2026, 9, 22, 0, 49, 17, 70000, tzinfo=UTC),
            t_end=make_call(1).requested_at,
            checkpoint_seq=1,
            checkpoint_id=image(1),
        ),
        StepRow(
            trial_id=TRIAL_ID,
            step_idx=2,
            atif_step_id=3,
            source="agent",
            message='Created `/app/hello.txt` with the content "Hello, world!".',
            t_start=datetime(2026, 9, 22, 0, 49, 18, 565000, tzinfo=UTC),
            checkpoint_seq=1,
            checkpoint_id=image(1),
        ),
        StepRow(
            trial_id=TRIAL_ID,
            step_idx=3,
            source="system",
            tool_call_id=STOP_ID,
            tool="Stop",
            t_start=make_call(2).requested_at,
            t_end=make_call(2).requested_at,
            checkpoint_seq=2,
            checkpoint_id=image(2),
        ),
    )
    assert steps.owner_step == {FIXTURE_TOOL_CALL_ID: 1, STOP_ID: 3}
    assert [record.seq for record in steps.checkpoints] == [1, 2]
    assert steps.checkpoints[1].tool_call_id == STOP_ID
    assert steps.hooked_calls == steps.answered_calls == (FIXTURE_TOOL_CALL_ID,)
    assert steps.timed_out_calls == steps.unanswered_calls == ()
    assert [call.tool_call_id for call in steps.stop_calls] == [STOP_ID]
    assert all(r.is_loop is r.is_revert is r.files_touched is None for r in steps.rows)


def test_stock_fixture_has_no_checkpoints(trial_copy: Path) -> None:
    postprocess_trial(trial_copy)

    steps = build_steps(load_trajectory(enriched_trajectory_path(trial_copy)), trial_id=TRIAL_ID)

    assert [(row.tool, row.checkpoint_seq, row.t_end) for row in steps.rows] == [
        ("Write", None, None),
        (None, None, None),
    ]
    assert steps.checkpoints == steps.stop_calls == ()
    assert steps.unanswered_calls == (FIXTURE_TOOL_CALL_ID,)
    assert steps.answered_calls == ()


def test_a_trajectory_that_is_not_enriched_is_refused(fixture_trial: Path) -> None:
    with pytest.raises(StepsError, match="not an enriched trajectory"):
        build_steps(load_trajectory(trajectory_path(fixture_trial)), trial_id=TRIAL_ID)


# -- synthetic trajectories --------------------------------------------------------------------


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"tool_call_id": call_id, "function_name": name, "arguments": arguments}


def result(call_id: str, seen: Any, **raw: Any) -> dict[str, Any]:
    """An observation result as Harbor writes it: `content` with Harbor's additions, and the
    tool_result the agent saw in `raw_tool_result`."""
    block = {"tool_use_id": call_id, "type": "tool_result", "content": seen, **raw}
    return {
        "source_call_id": call_id,
        "content": f"{seen}\n\n[metadata] {{}}",
        "extra": {"tool_result_metadata": {"raw_tool_result": block}},
    }


def at_time(seconds: float) -> datetime:
    """T0 plus `seconds`, as a datetime (a call record's times)."""
    return T0 + timedelta(seconds=seconds)


def agent(
    seconds: float,
    message: Any = "",
    calls: Sequence[dict[str, Any]] = (),
    results: Sequence[dict[str, Any]] = (),
    **fields: Any,
) -> dict[str, Any]:
    step: dict[str, Any] = {"timestamp": at(seconds), "source": "agent", "message": message}
    if calls:
        step["tool_calls"] = list(calls)
    if results:
        step["observation"] = {"results": list(results)}
    return step | fields


def trajectory(*steps: dict[str, Any], prompt: bool = True) -> Trajectory:
    """The fixture's trajectory with its steps after the prompt replaced by `steps`."""
    data = copy.deepcopy(FIXTURE_DATA)
    data["steps"] = [*data["steps"][:1]] if prompt else []
    data["steps"].extend(copy.deepcopy(steps))
    for step_id, step in enumerate(data["steps"], start=1):
        step["step_id"] = step_id
    data["final_metrics"]["total_steps"] = len(data["steps"])
    return Trajectory.model_validate(data)


def rows_of(
    trajectory_: Trajectory,
    *,
    checkpoints: Sequence[CheckpointRecord] = (),
    calls: Sequence[CallRecord] = (),
    timeouts: Sequence[str] = (),
    compactions: Sequence[CompactionRecord] = (),
    policy: CheckpointPolicy = CAPTURE_POLICY,
) -> TrialSteps:
    capture = CaptureRecords(
        policy=policy,
        checkpoints=tuple(checkpoints),
        calls=tuple(calls),
        timed_out_ids=tuple(timeouts),
    )
    enriched = enrich(trajectory_, capture, list(compactions), source_sha256="0" * 64)
    return build_steps(enriched, trial_id=TRIAL_ID)


def unchanged(call_seq: int, call_id: str, seq: int | None, **fields: Any) -> CallRecord:
    defaults = {"tool_name": "Bash", "outcome": "unchanged", "change": "unchanged"}
    return make_call(call_seq, call_id, checkpoint_seq=seq, **defaults | fields)


def test_a_step_with_two_calls_gives_a_row_per_call() -> None:
    traj = trajectory(
        agent(
            1,
            "Two things.",
            [call("toolu_a", "Bash", command="ls /app"), call("toolu_b", "Read", file_path="/x")],
            # Reversed: outputs are matched to calls by source_call_id, not by position.
            [result("toolu_b", "contents of x"), result("toolu_a", "hello.txt\n")],
            reasoning_content="List, then read.",
        )
    )

    steps = rows_of(traj)

    a, b = steps.rows
    assert (a.step_idx, a.atif_step_id, a.tool, a.tool_call_id) == (1, 2, "Bash", "toolu_a")
    assert (b.step_idx, b.atif_step_id, b.tool, b.tool_call_id) == (2, 2, "Read", "toolu_b")
    assert (a.output, b.output) == ("hello.txt\n", "contents of x")
    assert (a.message, a.reasoning) == ("Two things.", "List, then read.")
    assert (b.message, b.reasoning) == (None, None)
    assert a.t_start == b.t_start == T0 + timedelta(seconds=1)
    assert steps.owner_step == {"toolu_a": 1, "toolu_b": 2}
    # Read is not a hooked tool; the Bash call has no record in this stock capture.
    assert steps.hooked_calls == steps.unanswered_calls == ("toolu_a",)


def test_args_json_has_sorted_keys_and_keeps_non_ascii() -> None:
    arguments = {"file_path": "/app/é.txt", "content": "ü", "edits": [{"b": 1, "a": 2}]}
    traj = trajectory(agent(1, calls=[call("toolu_w", "Write", **arguments)]))

    (row,) = rows_of(traj).rows

    assert (
        row.args_json == '{"content": "ü", "edits": [{"a": 2, "b": 1}], "file_path": "/app/é.txt"}'
    )
    assert json.loads(row.args_json) == arguments
    # A call without a result has no output; a call step's empty message stays empty.
    assert (row.output, row.is_error, row.message) == (None, None, "")


def test_checkpoint_seq_follows_the_state_after_each_row() -> None:
    traj = trajectory(
        agent(1, calls=[call("toolu_r1", "Read", file_path="/app")]),  # before any checkpoint
        agent(2, calls=[call("toolu_u0", "Bash", command="ls")]),  # unchanged, no checkpoint yet
        agent(3, calls=[call("toolu_w1", "Write", file_path="/app/a", content="a")]),
        agent(4, calls=[call("toolu_r2", "Read", file_path="/app/a")]),  # read-only after it
        agent(5, calls=[call("toolu_u1", "Bash", command="cat /app/a")]),  # unchanged
        agent(6, calls=[call("toolu_t1", "Bash", command="make")]),  # timed out
        agent(7, "Checking."),
        agent(8, calls=[call("toolu_e2", "Edit", file_path="/app/a")]),
        agent(9, "Done."),
    )
    checkpoints = [
        make_checkpoint(1, "toolu_w1", tool_name="Write"),
        make_checkpoint(2, "toolu_e2", tool_name="Edit"),
    ]
    calls = [
        unchanged(1, "toolu_u0", None),
        make_call(2, "toolu_w1", checkpoint_seq=1),
        unchanged(3, "toolu_u1", 1),
        make_call(4, "toolu_e2", tool_name="Edit", checkpoint_seq=2, change="changed"),
        unchanged(5, STOP_ID, 2, trigger="stop", tool_name="Stop", requested_at=at_time(10)),
    ]

    steps = rows_of(traj, checkpoints=checkpoints, calls=calls, timeouts=["toolu_t1"])

    assert [(row.tool, row.checkpoint_seq, row.checkpoint_id) for row in steps.rows] == [
        ("Read", None, None),  # the task image's state
        ("Bash", None, None),  # unchanged before any checkpoint: still the task image
        ("Write", 1, image(1)),
        ("Read", 1, image(1)),
        ("Bash", 1, image(1)),  # its record names the previous checkpoint
        ("Bash", None, None),  # timed out: no state on record
        (None, None, None),  # its changes are in no checkpoint until the next one
        ("Edit", 2, image(2)),
        (None, 2, image(2)),
        ("Stop", 2, image(2)),  # an unchanged stop names the last checkpoint
    ]
    assert [row.atif_step_id for row in steps.rows] == [2, 3, 4, 5, 6, 7, 8, 9, 10, None]
    assert [row.step_idx for row in steps.rows] == list(range(1, 11))
    # t_end is the hook's requested_at for answered calls only.
    by_call = {row.tool_call_id: row for row in steps.rows}
    assert by_call["toolu_w1"].t_end == make_call(2).requested_at
    assert by_call["toolu_r2"].t_end is None
    assert by_call["toolu_t1"].t_end is None
    assert steps.hooked_calls == ("toolu_u0", "toolu_w1", "toolu_u1", "toolu_t1", "toolu_e2")
    assert steps.answered_calls == ("toolu_u0", "toolu_w1", "toolu_u1", "toolu_e2")
    assert steps.timed_out_calls == ("toolu_t1",)
    assert steps.unanswered_calls == ()


def test_parallel_calls_answered_out_of_order_leave_the_latest_checkpoint() -> None:
    # The second call was answered first (checkpoint 1), then the first (checkpoint 2); the
    # state after the step is checkpoint 2.
    traj = trajectory(
        agent(
            1,
            calls=[
                call("toolu_a", "Write", file_path="/a", content="a"),
                call("toolu_b", "Bash", command="touch /b"),
            ],
        ),
        agent(2, "Both done."),
    )
    checkpoints = [
        make_checkpoint(1, "toolu_b", tool_name="Bash"),
        make_checkpoint(2, "toolu_a"),
    ]
    calls = [make_call(2, "toolu_a"), make_call(1, "toolu_b", tool_name="Bash")]

    steps = rows_of(traj, checkpoints=checkpoints, calls=calls)

    assert [(row.tool, row.checkpoint_seq) for row in steps.rows] == [
        ("Write", 2),
        ("Bash", 1),
        (None, 2),
    ]


def test_deferred_calls_have_no_state_on_record() -> None:
    # Every-N with N = 2 (ADR-0007): the first of each pair is deferred, the second covers it.
    traj = trajectory(
        agent(1, calls=[call("toolu_d1", "Bash", command="a")]),
        agent(2, calls=[call("toolu_r1", "Read", file_path="/a")]),
        agent(3, calls=[call("toolu_c1", "Bash", command="b")]),
        agent(4, calls=[call("toolu_d2", "Bash", command="c")]),
        agent(5, "Waiting."),
    )
    checkpoints = [
        make_checkpoint(
            1, "toolu_c1", tool_name="Bash", covered_tool_call_ids=("toolu_d1", "toolu_c1")
        )
    ]
    deferred = {"tool_name": "Bash", "outcome": "deferred", "change": "not_checked"}
    calls = [
        make_call(1, "toolu_d1", checkpoint_seq=None, **deferred),
        make_call(2, "toolu_c1", tool_name="Bash", checkpoint_seq=1, change="not_checked"),
        make_call(3, "toolu_d2", checkpoint_seq=None, **deferred),
    ]

    steps = rows_of(traj, checkpoints=checkpoints, calls=calls, policy=CheckpointPolicy(every=2))

    assert [(row.tool, row.checkpoint_seq) for row in steps.rows] == [
        ("Bash", None),
        ("Read", None),  # d1's changes are in no checkpoint yet
        ("Bash", 1),
        ("Bash", None),
        (None, None),  # nor are d2's
    ]
    assert steps.answered_calls == ("toolu_d1", "toolu_c1", "toolu_d2")


def test_compaction_and_later_user_steps_are_rows() -> None:
    summary = "This session is being continued from a previous conversation."
    traj = trajectory(
        agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]),
        {"timestamp": at(3), "source": "user", "message": summary},
        agent(4, "Continuing."),
    )
    compaction = CompactionRecord(
        uuid="0b5e1d0a-0000-4000-8000-00000000c0de",
        timestamp=T0 + timedelta(seconds=2.5),
        native_file="sessions/projects/-app/x.jsonl",
        trigger="auto",
        pre_tokens=167_000,
    )

    steps = rows_of(
        traj,
        checkpoints=[make_checkpoint(1, "toolu_w1")],
        calls=[make_call(1, "toolu_w1")],
        compactions=[compaction],
    )

    write, boundary, user, final = steps.rows
    assert write.tool == "Write"
    assert boundary == StepRow(
        trial_id=TRIAL_ID,
        step_idx=2,
        source="system",
        message="context compaction",
        t_start=T0 + timedelta(seconds=2.5),
        checkpoint_seq=1,
        checkpoint_id=image(1),
    )
    assert (user.source, user.atif_step_id, user.message, user.tool) == ("user", 3, summary, None)
    assert (user.checkpoint_seq, final.checkpoint_seq) == (1, 1)
    assert steps.prompt == PROMPT


def test_stop_rows_sit_where_they_happened() -> None:
    # A stop hook that keeps the agent going (the repair protocol's) gives two stops. Each row
    # goes before the first row that started after it, and keeps the checkpoint its record
    # names; the first stop's checkpoint is the environment's state until the next change.
    traj = trajectory(
        agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]),
        agent(3, calls=[call("toolu_b3", "Bash", command="make")]),
        agent(4, "Done now."),
    )
    first_stop, second_stop = "stop_1790000002_7", "stop_1790000005_9"
    checkpoints = [
        make_checkpoint(1, "toolu_w1"),
        make_checkpoint(2, first_stop, trigger="stop", tool_name="Stop"),
        make_checkpoint(3, "toolu_b3", tool_name="Bash"),
    ]
    stop = {"trigger": "stop", "tool_name": "Stop"}
    calls = [
        make_call(1, "toolu_w1", requested_at=at_time(1.5)),
        make_call(2, first_stop, change="changed", requested_at=at_time(2), **stop),
        make_call(3, "toolu_b3", tool_name="Bash", change="changed", requested_at=at_time(3.5)),
        unchanged(4, second_stop, 3, requested_at=at_time(5), **stop),
    ]

    steps = rows_of(traj, checkpoints=checkpoints, calls=calls)

    assert [(row.tool_call_id, row.checkpoint_seq) for row in steps.rows] == [
        ("toolu_w1", 1),
        (first_stop, 2),
        ("toolu_b3", 3),
        (None, 3),
        (second_stop, 3),
    ]
    assert steps.owner_step[first_stop] == 2 and steps.owner_step[second_stop] == 5
    assert [row.atif_step_id is None for row in steps.rows] == [False, True, False, False, True]
    assert [row.t_start for row in steps.rows] == sorted(row.t_start for row in steps.rows)


def test_a_stop_without_later_steps_goes_last_and_ties_stay_after() -> None:
    traj = trajectory(agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]))
    stop = {"trigger": "stop", "tool_name": "Stop"}
    calls = [
        make_call(1, "toolu_w1", requested_at=at_time(1)),
        unchanged(2, "stop_1790000001_1", 1, requested_at=at_time(1), **stop),
    ]
    steps = rows_of(traj, checkpoints=[make_checkpoint(1, "toolu_w1")], calls=calls)
    assert [row.tool for row in steps.rows] == ["Write", "Stop"]


def test_output_is_what_the_agent_saw() -> None:
    blocks = [
        {"type": "text", "text": "Here is the plot:"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iV"}},
    ]
    traj = trajectory(
        agent(
            1,
            calls=[
                call("toolu_img", "Read", file_path="/app/plot.png"),
                call("toolu_plain", "Bash", command="echo hi"),
                call("toolu_parts", "Read", file_path="/app/p.png"),
            ],
            results=[
                result("toolu_img", blocks),
                # No raw tool_result: Harbor's content is all there is.
                {"source_call_id": "toolu_plain", "content": "hi\n"},
                {
                    "source_call_id": "toolu_parts",
                    "content": [
                        {"type": "text", "text": "a picture"},
                        {"type": "image", "source": {"media_type": "image/png", "path": "p.png"}},
                    ],
                },
            ],
        )
    )

    assert [row.output for row in rows_of(traj).rows] == [
        "Here is the plot:\n[image]",
        "hi\n",
        "a picture\n[image]",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [({"is_error": True}, True), ({"is_error": False}, False), ({}, None)],
)
def test_is_error_is_claude_codes_flag(raw: dict[str, Any], expected: bool | None) -> None:
    traj = trajectory(
        agent(
            1,
            calls=[call("toolu_x", "Bash", command="false")],
            results=[result("toolu_x", "Exit code 1", **raw)],
        )
    )

    (row,) = rows_of(traj).rows

    assert row.is_error is expected


def test_message_parts_and_naive_timestamps() -> None:
    parts = [
        {"type": "text", "text": "Look:"},
        {"type": "image", "source": {"media_type": "image/png", "path": "a.png"}},
    ]
    traj = trajectory(agent(1, parts, timestamp="2026-09-22T00:49:18.5"))

    (row,) = rows_of(traj).rows

    assert row.message == "Look:\n[image]"
    assert row.t_start == datetime(2026, 9, 22, 0, 49, 18, 500000, tzinfo=UTC)


def test_a_trajectory_without_a_user_prompt_keeps_its_first_step() -> None:
    traj = trajectory(agent(1, "Starting."), prompt=False)

    steps = rows_of(traj)

    assert steps.prompt == ""
    assert [(row.atif_step_id, row.message) for row in steps.rows] == [(1, "Starting.")]


def test_a_call_record_naming_a_missing_checkpoint_is_refused() -> None:
    traj = trajectory(agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]))

    with pytest.raises(StepsError, match="names checkpoint 1, which has no checkpoint step"):
        rows_of(traj, calls=[make_call(1, "toolu_w1")])


def test_two_results_for_one_call_are_refused() -> None:
    traj = trajectory(
        agent(
            1,
            calls=[call("toolu_x", "Bash", command="ls")],
            results=[result("toolu_x", "a"), result("toolu_x", "b")],
        )
    )

    with pytest.raises(StepsError, match="two results for toolu_x"):
        rows_of(traj)


# -- agreement with capture.transcript ---------------------------------------------------------


def observation(
    harbor: Any = "harbor text", raw: dict[str, Any] | None = None
) -> ObservationResult:
    extra = None if raw is None else {"tool_result_metadata": {"raw_tool_result": raw}}
    return ObservationResult(source_call_id="toolu_x", content=harbor, extra=extra)


@pytest.mark.parametrize(
    "result_",
    [
        observation("harbor only"),
        observation(None),
        observation(
            [
                {"type": "text", "text": "t"},
                {"type": "image", "source": {"media_type": "image/png", "path": "i.png"}},
            ]
        ),
        observation(raw={"type": "tool_result", "content": "seen text"}),
        observation(
            raw={
                "type": "tool_result",
                "content": [
                    {"type": "text", "text": "before"},
                    {"type": "image", "source": {"type": "base64", "data": "iVBOR"}},
                    "not a block",
                    {"type": "text"},
                    {"type": "text", "text": "after"},
                ],
            }
        ),
        observation(raw={"type": "tool_result", "tool_use_id": "toolu_x"}),  # no content key
        observation(raw={"type": "tool_result", "content": None}),
        observation(raw={"type": "tool_result", "content": {"unexpected": "shape"}}),
    ],
)
def test_seen_output_agrees_with_the_transcript(result_: ObservationResult) -> None:
    assert seen_output(result_) == transcript.seen_output(result_)


def test_seen_output_agrees_with_the_transcript_on_the_fixture(fixture_trial: Path) -> None:
    results = [
        result_
        for step in load_trajectory(trajectory_path(fixture_trial)).steps
        for result_ in (step.observation.results if step.observation else [])
    ]
    assert results
    for result_ in results:
        assert seen_output(result_) == transcript.seen_output(result_) == FIXTURE_OUTPUT


# -- review findings, 2026-10-18 ----------------------------------------------------------------

REJECTED = "<tool_use_error>String to replace not found in file.\nString: x</tool_use_error>"


def test_a_call_claude_code_rejected_is_not_a_missing_record() -> None:
    # Edit "String to replace not found" is refused before the tool runs: no hook fires and
    # nothing changes (tb21-haiku-v1/write-compressor__DBSxwUL had one), so no record is owed.
    traj = trajectory(
        agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]),
        agent(
            2,
            calls=[call("toolu_e1", "Edit", file_path="/a", old_string="x", new_string="y")],
            results=[result("toolu_e1", REJECTED, is_error=True)],
        ),
        agent(3, calls=[call("toolu_r1", "Read", file_path="/a")]),
    )
    steps = rows_of(
        traj, checkpoints=[make_checkpoint(1, "toolu_w1")], calls=[make_call(1, "toolu_w1")]
    )
    assert steps.rejected_calls == ("toolu_e1",)
    assert steps.hooked_calls == ("toolu_w1",) and steps.unanswered_calls == ()
    # It changed nothing, so the state stays the last checkpoint's.
    assert [row.checkpoint_seq for row in steps.rows] == [1, 1, 1]


def test_an_error_that_is_not_a_rejection_is_still_owed_a_record() -> None:
    traj = trajectory(
        agent(
            1,
            calls=[call("toolu_b1", "Bash", command="false")],
            results=[result("toolu_b1", "Exit code 1", is_error=True)],
        )
    )
    steps = rows_of(traj)
    assert steps.unanswered_calls == ("toolu_b1",) and steps.rejected_calls == ()


def test_a_call_stopped_while_running_is_interrupted() -> None:
    # The time cap killed the agent mid-Bash: the call has no result and no hook fired.
    traj = trajectory(
        agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]),
        agent(2, "Building."),
        agent(3, calls=[call("toolu_b9", "Bash", command="make")], results=[]),
    )
    steps = rows_of(
        traj, checkpoints=[make_checkpoint(1, "toolu_w1")], calls=[make_call(1, "toolu_w1")]
    )
    assert steps.interrupted_calls == ("toolu_b9",) and steps.unanswered_calls == ()
    assert steps.hooked_calls == ("toolu_w1", "toolu_b9")
    assert [row.checkpoint_seq for row in steps.rows] == [1, 1, None]
    assert steps.final_state_recorded is False


def test_the_final_state_is_recorded_only_by_a_last_stop() -> None:
    stop = {"trigger": "stop", "tool_name": "Stop"}
    traj = trajectory(
        agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]),
        agent(3, calls=[call("toolu_w2", "Write", file_path="/b", content="b")]),
    )
    checkpoints = [make_checkpoint(1, "toolu_w1"), make_checkpoint(2, "toolu_w2")]
    first = make_call(1, "toolu_w1", requested_at=at_time(1.5))
    early_stop = unchanged(2, "stop_1790000002_1", 1, requested_at=at_time(2), **stop)
    second = make_call(3, "toolu_w2", checkpoint_seq=2, requested_at=at_time(3.5))
    late_stop = unchanged(4, "stop_1790000004_1", 2, requested_at=at_time(4), **stop)

    # A stop hook kept the agent going after the first stop, and the second stop timed out.
    worked_on = rows_of(traj, checkpoints=checkpoints, calls=[first, early_stop, second])
    assert worked_on.final_state_recorded is False
    ended = rows_of(traj, checkpoints=checkpoints, calls=[first, early_stop, second, late_stop])
    assert ended.final_state_recorded is True


def test_two_checkpoint_steps_with_one_seq_are_refused() -> None:
    traj = trajectory(
        agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]),
        agent(2, calls=[call("toolu_w2", "Write", file_path="/b", content="b")]),
    )
    checkpoints = [make_checkpoint(1, "toolu_w1"), make_checkpoint(1, "toolu_w2")]
    with pytest.raises(StepsError, match="two checkpoint steps for seq 1"):
        rows_of(traj, checkpoints=checkpoints)


def test_rows_after_a_call_without_a_record_have_no_state() -> None:
    # A capture fault: the Write ran (it has a result) but the watcher answered nothing, so no
    # checkpoint is known to hold what it wrote.
    traj = trajectory(
        agent(1, calls=[call("toolu_w1", "Write", file_path="/a", content="a")]),
        agent(
            2,
            calls=[call("toolu_w2", "Write", file_path="/b", content="b")],
            results=[result("toolu_w2", "File created successfully at: /b")],
        ),
        agent(3, "Done."),
    )
    steps = rows_of(
        traj, checkpoints=[make_checkpoint(1, "toolu_w1")], calls=[make_call(1, "toolu_w1")]
    )
    assert steps.unanswered_calls == ("toolu_w2",)
    assert [row.checkpoint_seq for row in steps.rows] == [1, None, None]


def test_a_deferred_call_already_covered_keeps_the_state() -> None:
    # Every-N, parallel calls answered out of order: the second listed call's checkpoint covers
    # the first, which the watcher answered as deferred after it.
    traj = trajectory(
        agent(
            1,
            calls=[
                call("toolu_d1", "Bash", command="a"),
                call("toolu_c1", "Bash", command="b"),
            ],
        ),
        agent(2, "Done."),
    )
    checkpoints = [
        make_checkpoint(
            1, "toolu_c1", tool_name="Bash", covered_tool_call_ids=("toolu_d1", "toolu_c1")
        )
    ]
    deferred = {"tool_name": "Bash", "outcome": "deferred", "change": "not_checked"}
    calls = [
        make_call(1, "toolu_c1", tool_name="Bash", checkpoint_seq=1, change="not_checked"),
        make_call(2, "toolu_d1", checkpoint_seq=None, **deferred),
    ]
    steps = rows_of(traj, checkpoints=checkpoints, calls=calls, policy=CheckpointPolicy(every=2))
    # d1 is listed first, before the checkpoint is reached: unknown then; once c1's checkpoint
    # (which covers d1) is reached, the state after the step is known.
    assert [(row.tool_call_id, row.checkpoint_seq) for row in steps.rows] == [
        ("toolu_d1", None),
        ("toolu_c1", 1),
        (None, 1),
    ]
