"""Summarize one captured trial: its timeline and the numbers the group plans from.

Reads only the trial dir (after `trajlab postprocess`): the enriched trajectory, result.json,
and the checkpoint files. Prints one JSON object:

- `numbers`: tool calls vs hooked calls vs checkpoints, checkpoint storage, tokens and cost,
  wall-clock time by phase, and the time the hook held the agent;
- `timeline`: one row per tool call and per checkpoint, in enriched-trajectory order, with
  seconds since the agent started, what the call ran, what it changed, and which checkpoint
  holds the state after it.

    uv run python scripts/2026-10-02_trial_report.py corpus/jobs/<job>/<trial> > report.json
"""

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from harbor.models.trial.result import TrialResult

from trajlab.contracts import CallRecord, CheckpointRecord


def seconds(start: datetime | None, end: datetime | None) -> float | None:
    return round((end - start).total_seconds(), 1) if start and end else None


def summary(call: dict[str, Any]) -> str:
    args = call["arguments"]
    text = args.get("command") or args.get("file_path") or json.dumps(args)
    return " ".join(str(text).split())[:120]


def main() -> int:
    trial = Path(sys.argv[1])
    result = TrialResult.model_validate_json((trial / "result.json").read_text())
    enriched = json.loads((trial / "agent/trajectory.enriched.json").read_text())
    ckdir = trial / "agent/checkpoints"
    records = [
        CheckpointRecord.model_validate_json(line)
        for line in (ckdir / "checkpoints.jsonl").read_text().splitlines()
        if line
    ]
    calls = [
        CallRecord.model_validate_json(line)
        for line in (ckdir / "calls.jsonl").read_text().splitlines()
        if line
    ]
    agent_start = result.agent_execution.started_at if result.agent_execution else None

    def offset(stamp: str | datetime) -> float | None:
        moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        return seconds(agent_start, moment)

    timeline: list[dict[str, Any]] = []
    tools: Counter[str] = Counter()
    for step in enriched["steps"]:
        trajlab = step["extra"]["trajlab"]
        if trajlab["kind"] == "checkpoint":
            record = CheckpointRecord.model_validate(step["observation"]["results"][0]["extra"])
            timeline.append(
                {
                    "kind": "checkpoint",
                    "t": offset(record.captured_at),
                    "seq": record.seq,
                    "trigger": record.trigger,
                    "bytes": record.bytes,
                    "capture_ms": record.capture_ms,
                }
            )
            continue
        if trajlab["kind"] != "original":
            timeline.append({"kind": trajlab["kind"], "t": offset(step["timestamp"])})
            continue
        records_by_id = {c["tool_call_id"]: c for c in trajlab.get("calls", [])}
        for call in step.get("tool_calls") or []:
            tools[call["function_name"]] += 1
            record = records_by_id.get(call["tool_call_id"])
            timeline.append(
                {
                    "kind": "tool_call",
                    "t": offset(step["timestamp"]),
                    "step": trajlab["original_step_id"],
                    "tool": call["function_name"],
                    "what": summary(call),
                    "outcome": record["outcome"] if record else "not hooked",
                    "changed": record["changed_paths_total"] if record else None,
                    "changed_paths": record["changed_paths"][:8] if record else [],
                    "checkpoint_seq": record["checkpoint_seq"] if record else None,
                    "tool_failed": record["tool_failed"] if record else None,
                    "hook_s": (
                        round(
                            (
                                datetime.fromisoformat(record["answered_at"])
                                - datetime.fromisoformat(record["requested_at"])
                            ).total_seconds(),
                            2,
                        )
                        if record
                        else None
                    ),
                }
            )
    for call in calls:
        if call.trigger == "stop":
            timeline.append(
                {
                    "kind": "stop",
                    "t": offset(call.requested_at),
                    "outcome": call.outcome,
                    "changed": call.changed_paths_total,
                    "changed_paths": list(call.changed_paths[:8]),
                    "checkpoint_seq": call.checkpoint_seq,
                }
            )

    agent = result.agent_result
    rewards = result.verifier_result.rewards if result.verifier_result else None
    hook_wait = sum((c.answered_at - c.requested_at).total_seconds() for c in calls)
    phases = {
        name: seconds(getattr(result, name).started_at, getattr(result, name).finished_at)
        for name in ("environment_setup", "agent_setup", "agent_execution", "verifier")
        if getattr(result, name) is not None
    }
    numbers = {
        "trial": result.trial_name,
        "task": result.task_name,
        "model": enriched["agent"].get("model_name"),
        "claude_code": enriched["agent"].get("version"),
        "reward": (rewards or {}).get("reward"),
        "tool_calls_total": sum(tools.values()),
        "tool_calls_by_tool": dict(tools),
        "hooked_calls": sum(1 for c in calls if c.trigger == "tool_call"),
        "checkpoints_taken": len(records),
        "checkpoints_on_tool_calls": sum(1 for r in records if r.trigger == "tool_call"),
        "checkpoints_on_stop": sum(1 for r in records if r.trigger == "stop"),
        "unchanged_calls_skipped": sum(1 for c in calls if c.outcome == "unchanged"),
        "checkpoint_bytes_total": sum(r.bytes or 0 for r in records),
        "checkpoint_bytes_max": max((r.bytes or 0 for r in records), default=0),
        "capture_ms_total": sum(r.capture_ms for r in records),
        "hook_wait_s_total": round(hook_wait, 1),
        "input_tokens": agent.n_input_tokens if agent else None,
        "cache_tokens": agent.n_cache_tokens if agent else None,
        "output_tokens": agent.n_output_tokens if agent else None,
        "cost_usd": agent.cost_usd if agent else None,
        "trial_wall_s": seconds(result.started_at, result.finished_at),
        "phase_s": phases,
    }
    json.dump({"numbers": numbers, "timeline": timeline}, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
