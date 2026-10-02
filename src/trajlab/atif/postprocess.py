"""Postprocess: join a trial's checkpoints and compactions into `agent/trajectory.enriched.json`.

Shape: ADR-0003 and its amendments. Every step of Harbor's `trajectory.json` is copied with
`extra.trajlab` naming its original step id and the watcher's `CallRecord` for each of its
hooked tool calls. A system step per checkpoint is inserted after the agent step that owns the
checkpoint's `tool_call_id`; stop checkpoints (ADR-0011) go after the last step. A system step
per compaction boundary recovered from the native JSONL is inserted before the first step at or
after the boundary's time. `trajectory.json` itself is never modified.

Reads the checkpoint files directly with the `contracts` models: `atif` does not import
`checkpoint` (CLAUDE.md constraint 3).
"""

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.models.trajectories import Step, Trajectory
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import TrialPaths
from pydantic import BaseModel, ValidationError

from trajlab.atif.compaction import read_compactions
from trajlab.atif.load import load_trajectory, trajectory_path
from trajlab.atif.validate import validate_trajectory
from trajlab.contracts import (
    CALLS_FILENAME,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINT_STEP_MESSAGE,
    CHECKPOINTS_DIRNAME,
    COMPACTION_STEP_MESSAGE,
    CONTEXT_MANAGEMENT_EXTRA_KEY,
    ENRICHED_TRAJECTORY_FILENAME,
    POLICY_FILENAME,
    STOP_ID_PREFIX,
    TIMEOUT_SUFFIX,
    TRAJLAB_EXTRA_KEY,
    CallRecord,
    CheckpointPolicy,
    CheckpointRecord,
    CheckpointStepExtra,
    CompactionRecord,
    CompactionStepExtra,
    ContextManagementExtra,
    EnrichedTrajectoryExtra,
    OriginalStepExtra,
)

logger = logging.getLogger(__name__)


class PostprocessError(ValueError):
    """The trial's records cannot be joined to its trajectory; nothing was written."""


@dataclass(frozen=True)
class CaptureRecords:
    """What the hook and watcher left under `agent/checkpoints/`; all empty for a stock trial."""

    policy: CheckpointPolicy | None = None
    checkpoints: tuple[CheckpointRecord, ...] = ()
    calls: tuple[CallRecord, ...] = ()
    timed_out_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostprocessResult:
    path: Path
    steps: int
    checkpoints: int
    compactions: int


def enriched_trajectory_path(trial_dir: Path) -> Path:
    return TrialPaths(trial_dir).agent_dir / ENRICHED_TRAJECTORY_FILENAME


def _read_jsonl[M: BaseModel](path: Path, model: type[M]) -> list[M]:
    if not path.is_file():
        return []
    records = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValidationError as error:
            raise PostprocessError(f"{path}:{number}: not a {model.__name__}: {error}") from error
    return records


def read_capture_records(trial_dir: Path, trial_name: str) -> CaptureRecords:
    """Read `policy.json`, `checkpoints.jsonl`, `calls.jsonl`, and the `.timeout` markers."""
    directory = TrialPaths(trial_dir).agent_dir / CHECKPOINTS_DIRNAME
    if not directory.is_dir():
        return CaptureRecords()
    policy_path = directory / POLICY_FILENAME
    policy = (
        CheckpointPolicy.model_validate_json(policy_path.read_text())
        if policy_path.is_file()
        else None
    )
    checkpoints = _read_jsonl(directory / CHECKPOINT_RECORDS_FILENAME, CheckpointRecord)
    calls = _read_jsonl(directory / CALLS_FILENAME, CallRecord)
    for record in [*checkpoints, *calls]:
        if record.trial_name != trial_name:
            raise PostprocessError(
                f"{directory}: record for trial {record.trial_name!r}, expected {trial_name!r}"
            )
    answered = {call.tool_call_id for call in calls} | {
        call_id for record in checkpoints for call_id in record.covered_tool_call_ids
    }
    timed_out = []
    for marker in sorted(directory.glob(f"*{TIMEOUT_SUFFIX}")):
        call_id = marker.name.removesuffix(TIMEOUT_SUFFIX)
        if call_id in answered:
            # The hook gave up just as the watcher answered; the record stands (protocol).
            logger.warning(
                "%s: %s has both a record and a .timeout; keeping the record", directory, call_id
            )
            continue
        timed_out.append(call_id)
    return CaptureRecords(
        policy=policy,
        checkpoints=tuple(checkpoints),
        calls=tuple(calls),
        timed_out_ids=tuple(timed_out),
    )


def _step_time(step: Step) -> datetime | None:
    if not step.timestamp:
        return None
    time = datetime.fromisoformat(step.timestamp)
    # ISO 8601 without an offset; Claude Code always logs UTC.
    return time if time.tzinfo else time.replace(tzinfo=UTC)


def _checkpoint_step(record: CheckpointRecord) -> dict[str, Any]:
    return {
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


def _compaction_step(record: CompactionRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp.isoformat(),
        "source": "system",
        "message": COMPACTION_STEP_MESSAGE,
        "observation": {"results": [{"extra": record.model_dump(mode="json")}]},
        "extra": {
            TRAJLAB_EXTRA_KEY: CompactionStepExtra().model_dump(mode="json"),
            CONTEXT_MANAGEMENT_EXTRA_KEY: ContextManagementExtra(
                type="compaction", boundary="replace"
            ).model_dump(mode="json"),
        },
    }


def enrich(
    trajectory: Trajectory,
    capture: CaptureRecords,
    compactions: list[CompactionRecord],
    *,
    source_sha256: str,
) -> Trajectory:
    """Build the enriched trajectory. Pure: raises `PostprocessError` if a record has no step."""
    originals = trajectory.steps
    owner: dict[str, int] = {}
    for index, step in enumerate(originals):
        if step.extra and TRAJLAB_EXTRA_KEY in step.extra:
            raise PostprocessError(f"step {step.step_id} already has extra.{TRAJLAB_EXTRA_KEY}")
        for call in step.tool_calls or []:
            if call.tool_call_id in owner:
                raise PostprocessError(f"tool_call_id {call.tool_call_id} in two steps")
            owner[call.tool_call_id] = index

    def owning_index(call_id: str, what: str) -> int:
        if call_id not in owner:
            raise PostprocessError(f"{what} {call_id} names no tool call in trajectory.json")
        return owner[call_id]

    checkpoints_after: defaultdict[int, list[CheckpointRecord]] = defaultdict(list)
    stop_checkpoints: list[CheckpointRecord] = []
    for record in sorted(capture.checkpoints, key=lambda r: r.seq):
        if record.trigger == "stop":
            stop_checkpoints.append(record)
        else:
            checkpoints_after[owning_index(record.tool_call_id, "checkpoint")].append(record)

    calls_of: defaultdict[int, list[CallRecord]] = defaultdict(list)
    stop_calls: list[CallRecord] = []
    for call in sorted(capture.calls, key=lambda c: c.call_seq):
        if call.trigger == "stop":
            stop_calls.append(call)
        else:
            calls_of[owning_index(call.tool_call_id, "call record")].append(call)

    timed_out_of: defaultdict[int, list[str]] = defaultdict(list)
    timed_out_stops: list[str] = []
    for call_id in capture.timed_out_ids:
        if call_id.startswith(STOP_ID_PREFIX):
            timed_out_stops.append(call_id)
        else:
            timed_out_of[owning_index(call_id, "timed-out call")].append(call_id)

    # A boundary goes before the first step at or after it; Harbor orders steps by time, and
    # Claude Code logs the boundary after the summarizing call returns, before the summary.
    times = [_step_time(step) for step in originals]
    compactions_before: defaultdict[int, list[CompactionRecord]] = defaultdict(list)
    for record in compactions:
        position = next(
            (i for i, t in enumerate(times) if t is not None and t >= record.timestamp),
            len(originals),
        )
        compactions_before[position].append(record)

    steps: list[dict[str, Any]] = []
    for index, step in enumerate(originals):
        steps.extend(_compaction_step(record) for record in compactions_before[index])
        order = {call.tool_call_id: n for n, call in enumerate(step.tool_calls or [])}
        trajlab = OriginalStepExtra(
            original_step_id=step.step_id,
            calls=tuple(sorted(calls_of[index], key=lambda c: order[c.tool_call_id])),
            timed_out_tool_call_ids=tuple(
                sorted(timed_out_of[index], key=lambda call_id: order[call_id])
            ),
        )
        copied = step.model_dump(mode="json", exclude_none=True)
        copied["extra"] = (step.extra or {}) | {TRAJLAB_EXTRA_KEY: trajlab.model_dump(mode="json")}
        steps.append(copied)
        steps.extend(_checkpoint_step(record) for record in checkpoints_after[index])
    steps.extend(_compaction_step(record) for record in compactions_before[len(originals)])
    steps.extend(_checkpoint_step(record) for record in stop_checkpoints)
    for step_id, step in enumerate(steps, start=1):
        step["step_id"] = step_id

    data = trajectory.to_json_dict()
    data["steps"] = steps
    data["extra"] = (trajectory.extra or {}) | {
        TRAJLAB_EXTRA_KEY: EnrichedTrajectoryExtra(
            source_sha256=source_sha256,
            policy=capture.policy,
            stop_calls=tuple(stop_calls),
            timed_out_stop_ids=tuple(timed_out_stops),
        ).model_dump(mode="json")
    }
    if trajectory.final_metrics is not None:
        data["final_metrics"]["total_steps"] = len(steps)
    return Trajectory.model_validate(data)


def postprocess_trial(trial_dir: Path) -> PostprocessResult:
    """Write `agent/trajectory.enriched.json` for one finished trial, replacing any earlier one.

    The file is validated with Harbor's validator before it replaces the old one; on any error
    nothing is written. Raises `FileNotFoundError` without `agent/trajectory.json`.
    """
    source = trajectory_path(trial_dir)
    source_bytes = source.read_bytes()
    trajectory = load_trajectory(source)
    trial_name = TrialConfig.model_validate_json(
        TrialPaths(trial_dir).config_path.read_text()
    ).trial_name
    capture = read_capture_records(trial_dir, trial_name)
    compactions = read_compactions(trial_dir, trajectory.session_id)
    enriched = enrich(
        trajectory,
        capture,
        compactions,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )

    path = enriched_trajectory_path(trial_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(enriched.to_json_dict(), indent=2, ensure_ascii=False) + "\n")
    errors = validate_trajectory(tmp)
    if errors:
        tmp.unlink()
        raise PostprocessError(
            f"{path}: enriched trajectory is invalid ATIF:\n" + "\n".join(errors)
        )
    tmp.replace(path)
    logger.debug(
        "%s: %d steps, %d checkpoints, %d compactions",
        path,
        len(enriched.steps),
        len(capture.checkpoints),
        len(compactions),
    )
    return PostprocessResult(
        path=path,
        steps=len(enriched.steps),
        checkpoints=len(capture.checkpoints),
        compactions=len(compactions),
    )
