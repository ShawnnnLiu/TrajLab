"""Step rows of the Exp 2 store, from a trial's enriched trajectory (ADR-0014).

Input is `agent/trajectory.enriched.json` (postprocess, ADR-0003): Harbor's steps, each with
`extra.trajlab` naming its original step id and the CallRecords of its hooked calls; checkpoint
and compaction system steps inserted among them; the stop CallRecords on the root. Rows, in
order:

- The first original step, if it is a user step, is the prompt (`trials.prompt`), not a row.
- An original step with k tool calls gives k rows, in call order. The step's message and
  reasoning are on the first of them only. Each row has the call's tool, its arguments as JSON
  with sorted keys, and its output as the agent saw it: the observation result whose
  `source_call_id` is the call's id.
- An original step without tool calls gives one row with its message and reasoning; a user
  step after the prompt is such a row.
- A compaction system step gives one row: source `system`, message `context compaction`.
- A checkpoint system step gives no row; its record names the image of one checkpoint seq.
- One row per stop CallRecord (ADR-0011): source `system`, the stop's tool name and request
  id, `t_start` = `t_end` = its `requested_at`. It goes where it happened: before the first row
  whose `t_start` is later than its `requested_at` (stops in call_seq order), so a stop that a
  stop hook turned into another turn sits between the turns, not at the end. Postprocess puts
  stop checkpoints after the last step; this order is the store's own.

A tool output is Claude Code's tool_result content, kept by Harbor in the observation's
`extra.tool_result_metadata.raw_tool_result`, with non-text blocks (an image) shown as
`[<type>]`; without that block it is Harbor's `content` as text. This is the extraction of
`trajlab.capture.transcript.seen_output`, repeated here because the store does not import
capture; a test keeps the two equal.

`checkpoint_seq` is the checkpoint whose state the environment is in right after the row.
Walking the rows in order with `current`, the latest checkpoint reached (none at the start: the
task image's state):

- A call whose CallRecord names a checkpoint takes that seq, and `current` becomes the larger
  of the two. Parallel calls of one step can be answered out of order, so a later row may name
  an earlier checkpoint; the state after the step is still the latest one.
- A hooked call that timed out, or one deferred under every-N (ADR-0007), has no state on
  record: null, and `current` is unchanged.
- Every other row (read-only calls, message and compaction rows, unchanged calls before any
  checkpoint) takes `current`.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from harbor.models.trajectories import ContentPart, ObservationResult, Step, Trajectory
from pydantic import TypeAdapter, ValidationError

from trajlab.contracts import (
    COMPACTION_STEP_MESSAGE,
    HOOKED_TOOLS,
    TRAJLAB_EXTRA_KEY,
    CallRecord,
    CheckpointRecord,
    CheckpointStepExtra,
    CompactionStepExtra,
    EnrichedTrajectoryExtra,
    OriginalStepExtra,
    StepRow,
    TrajlabStepExtra,
)

logger = logging.getLogger(__name__)

_STEP_EXTRA = TypeAdapter(TrajlabStepExtra)


class StepsError(ValueError):
    """The trajectory is not an enriched trajectory these rows can be built from."""


@dataclass
class _Spec:
    """A row before its place and checkpoint are known."""

    fields: dict[str, Any]
    record: CallRecord | None = None
    timed_out: bool = False
    current: bool = False  # takes `current` (message and compaction rows)


def _place_stops(specs: list[_Spec], stops: tuple[CallRecord, ...]) -> list[_Spec]:
    """`specs` with a row for each stop, before the first row that started after it."""
    placed = list(specs)
    for record in stops:  # call_seq order, so equal times keep the stops' own order
        index = next(
            (
                i
                for i, spec in enumerate(placed)
                if spec.record is not record
                and spec.fields.get("t_start") is not None
                and spec.fields["t_start"] > record.requested_at
            ),
            len(placed),
        )
        stop = _Spec(
            fields={
                "source": "system",
                "tool_call_id": record.tool_call_id,
                "tool": record.tool_name,
                "t_start": record.requested_at,
                "t_end": record.requested_at,
            },
            record=record,
        )
        placed.insert(index, stop)
    return placed


@dataclass(frozen=True)
class TrialSteps:
    """A trial's step rows and what the store needs from its capture records."""

    source_sha256: str  # of the trajectory.json the enriched trajectory was built from
    prompt: str
    rows: tuple[StepRow, ...]
    owner_step: Mapping[str, int]  # tool_call_id (a stop's request id included) -> step_idx
    checkpoints: tuple[CheckpointRecord, ...]  # from the checkpoint steps, in seq order
    hooked_calls: tuple[str, ...]  # tool calls of HOOKED_TOOLS, in trajectory order
    answered_calls: tuple[str, ...]  # of those, the ones with a CallRecord
    timed_out_calls: tuple[str, ...]  # every original step's timed_out_tool_call_ids
    unanswered_calls: tuple[str, ...]  # hooked calls with neither a record nor a .timeout
    stop_calls: tuple[CallRecord, ...]  # the root's stop CallRecords, in call_seq order


def as_text(content: str | list[ContentPart] | None) -> str:
    """An ATIF message or tool output as text; an image or audio part becomes `[<type>]`."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(part.text if part.text is not None else f"[{part.type}]" for part in content)


def blocks_as_text(content: Any) -> str:
    """Tool_result content as Claude Code sent it: a string, or blocks whose non-text ones (an
    image) become `[<type>]`."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") if block.get("type") == "text" else f"[{block.get('type')}]"
            for block in content
            if isinstance(block, dict)
        )
    return str(content)


def _raw_tool_result(result: ObservationResult) -> Any:
    return ((result.extra or {}).get("tool_result_metadata") or {}).get("raw_tool_result")


def seen_output(result: ObservationResult) -> str:
    """A tool output as the agent saw it, else Harbor's ATIF content."""
    raw = _raw_tool_result(result)
    if isinstance(raw, dict) and "content" in raw:
        return blocks_as_text(raw["content"])
    return as_text(result.content)


def is_error(result: ObservationResult) -> bool | None:
    """Claude Code's `is_error` flag on the tool_result; null when it sent none."""
    raw = _raw_tool_result(result)
    flag = raw.get("is_error") if isinstance(raw, dict) else None
    return flag if isinstance(flag, bool) else None


def step_time(step: Step) -> datetime | None:
    """The step's timestamp, aware; Claude Code logs UTC, so a naive one is UTC (as postprocess
    reads it)."""
    if not step.timestamp:
        return None
    time = datetime.fromisoformat(step.timestamp)
    return time if time.tzinfo else time.replace(tzinfo=UTC)


def _step_extra(step: Step) -> OriginalStepExtra | CheckpointStepExtra | CompactionStepExtra:
    data = (step.extra or {}).get(TRAJLAB_EXTRA_KEY)
    if data is None:
        raise StepsError(
            f"step {step.step_id} has no extra.{TRAJLAB_EXTRA_KEY}: not an enriched trajectory"
        )
    try:
        return _STEP_EXTRA.validate_python(data)
    except ValidationError as error:
        raise StepsError(f"step {step.step_id}: bad extra.{TRAJLAB_EXTRA_KEY}: {error}") from error


def _root_extra(trajectory: Trajectory) -> EnrichedTrajectoryExtra:
    data = (trajectory.extra or {}).get(TRAJLAB_EXTRA_KEY)
    if data is None:
        raise StepsError(f"no root extra.{TRAJLAB_EXTRA_KEY}: not an enriched trajectory")
    try:
        return EnrichedTrajectoryExtra.model_validate(data)
    except ValidationError as error:
        raise StepsError(f"bad root extra.{TRAJLAB_EXTRA_KEY}: {error}") from error


def _checkpoint_record(step: Step, extra: CheckpointStepExtra) -> CheckpointRecord:
    results = step.observation.results if step.observation else []
    if not results:
        raise StepsError(f"checkpoint step {step.step_id} has no record")
    try:
        record = CheckpointRecord.model_validate(results[0].extra)
    except ValidationError as error:
        raise StepsError(f"checkpoint step {step.step_id}: bad record: {error}") from error
    if record.tool_call_id != extra.tool_call_id:
        raise StepsError(
            f"checkpoint step {step.step_id} links {extra.tool_call_id} but its record is "
            f"{record.tool_call_id}'s"
        )
    return record


def _results_by_call(step: Step) -> dict[str, ObservationResult]:
    results: dict[str, ObservationResult] = {}
    for result in step.observation.results if step.observation else []:
        if result.source_call_id is None:
            continue
        if result.source_call_id in results:
            raise StepsError(f"step {step.step_id}: two results for {result.source_call_id}")
        results[result.source_call_id] = result
    return results


class _Walk:
    """Step rows in order, with the checkpoint the environment is in after each."""

    def __init__(self, trial_id: UUID, images: Mapping[int, str]) -> None:
        self.trial_id = trial_id
        self.images = images
        self.current: int | None = None
        self.rows: list[StepRow] = []
        self.owner: dict[str, int] = {}

    def state_after(self, record: CallRecord | None, *, timed_out: bool = False) -> int | None:
        if timed_out:
            return None
        if record is not None and record.checkpoint_seq is not None:
            seq = record.checkpoint_seq
            self.current = seq if self.current is None else max(self.current, seq)
            return seq
        if record is not None and record.outcome == "deferred":
            return None
        return self.current

    def add(self, checkpoint_seq: int | None, **fields: Any) -> None:
        step_idx = len(self.rows) + 1
        if checkpoint_seq is not None and checkpoint_seq not in self.images:
            raise StepsError(
                f"row {step_idx} ({fields.get('tool_call_id')}) names checkpoint "
                f"{checkpoint_seq}, which has no checkpoint step"
            )
        call_id = fields.get("tool_call_id")
        if call_id is not None:
            if call_id in self.owner:
                raise StepsError(f"{call_id} names two rows")
            self.owner[call_id] = step_idx
        self.rows.append(
            StepRow(
                trial_id=self.trial_id,
                step_idx=step_idx,
                checkpoint_seq=checkpoint_seq,
                checkpoint_id=None if checkpoint_seq is None else self.images[checkpoint_seq],
                **fields,
            )
        )


def build_steps(trajectory: Trajectory, *, trial_id: UUID) -> TrialSteps:
    """The step rows of one trial's enriched trajectory, and its capture records.

    Raises `StepsError` if the trajectory is not enriched or its records do not fit together.
    """
    root = _root_extra(trajectory)
    parsed = [(step, _step_extra(step)) for step in trajectory.steps]

    checkpoints: dict[int, CheckpointRecord] = {}
    for step, extra in parsed:
        if isinstance(extra, CheckpointStepExtra):
            record = _checkpoint_record(step, extra)
            if record.seq in checkpoints:
                raise StepsError(f"two checkpoint steps for seq {record.seq}")
            checkpoints[record.seq] = record

    prompt = ""
    first = next((i for i, (_, e) in enumerate(parsed) if isinstance(e, OriginalStepExtra)), None)
    if first is not None and parsed[first][0].source == "user":
        prompt = as_text(parsed[first][0].message)
        del parsed[first]
    else:
        logger.warning("trajectory %s does not start with a user step", trajectory.session_id)

    specs: list[_Spec] = []
    hooked: list[str] = []
    answered: list[str] = []
    timed_out_all: list[str] = []
    unanswered: list[str] = []
    for step, extra in parsed:
        if isinstance(extra, CheckpointStepExtra):
            continue
        if isinstance(extra, CompactionStepExtra):
            fields = {"source": step.source, "message": COMPACTION_STEP_MESSAGE}
            specs.append(_Spec(fields | {"t_start": step_time(step)}, current=True))
            continue
        message = as_text(step.message)
        if not step.tool_calls:
            fields = {
                "atif_step_id": extra.original_step_id,
                "source": step.source,
                "message": message,
                "reasoning": step.reasoning_content,
                "t_start": step_time(step),
            }
            specs.append(_Spec(fields, current=True))
            continue
        records = {record.tool_call_id: record for record in extra.calls}
        timed_out = set(extra.timed_out_tool_call_ids)
        timed_out_all.extend(extra.timed_out_tool_call_ids)
        results = _results_by_call(step)
        for n, call in enumerate(step.tool_calls):
            record = records.get(call.tool_call_id)
            if call.function_name in HOOKED_TOOLS:
                hooked.append(call.tool_call_id)
                if record is not None:
                    answered.append(call.tool_call_id)
                elif call.tool_call_id not in timed_out:
                    unanswered.append(call.tool_call_id)
            result = results.get(call.tool_call_id)
            fields = {
                "atif_step_id": extra.original_step_id,
                "source": step.source,
                "tool_call_id": call.tool_call_id,
                "tool": call.function_name,
                "args_json": json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
                "output": None if result is None else seen_output(result),
                "is_error": None if result is None else is_error(result),
                "message": message if n == 0 else None,
                "reasoning": step.reasoning_content if n == 0 else None,
                "t_start": step_time(step),
                "t_end": None if record is None else record.requested_at,
            }
            specs.append(_Spec(fields, record=record, timed_out=call.tool_call_id in timed_out))

    stop_calls = tuple(sorted(root.stop_calls, key=lambda record: record.call_seq))
    walk = _Walk(trial_id, {seq: record.checkpoint_id for seq, record in checkpoints.items()})
    for spec in _place_stops(specs, stop_calls):
        seq = (
            walk.current
            if spec.current
            else walk.state_after(spec.record, timed_out=spec.timed_out)
        )
        walk.add(seq, **spec.fields)

    return TrialSteps(
        source_sha256=root.source_sha256,
        prompt=prompt,
        rows=tuple(walk.rows),
        owner_step=walk.owner,
        checkpoints=tuple(checkpoints[seq] for seq in sorted(checkpoints)),
        hooked_calls=tuple(hooked),
        answered_calls=tuple(answered),
        timed_out_calls=tuple(timed_out_all),
        unanswered_calls=tuple(unanswered),
        stop_calls=stop_calls,
    )
