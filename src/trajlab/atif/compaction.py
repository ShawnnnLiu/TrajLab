"""Recover compaction boundaries from Claude Code's native session JSONL.

Harbor's converter drops Claude Code's `compact_boundary` events (docs/harbor-facts.md), so
postprocess reads them back from the same files the converter read and turns each into a
`CompactionRecord`. Claude Code 2.1.278 writes, on each compaction:

    {"type": "system", "subtype": "compact_boundary", "uuid", "timestamp",
     "logicalParentUuid", "compactMetadata": {"trigger", "preTokens", "postTokens", ...}}
    {"type": "user", "isCompactSummary": true, "parentUuid": <the boundary's uuid>,
     "message": {"content": "This session is being continued ..."}}
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from harbor.models.trial.paths import TrialPaths

from trajlab.contracts import CompactionRecord

logger = logging.getLogger(__name__)

COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"


class CompactionError(ValueError):
    """The native session logs cannot be matched to the trajectory."""


def session_files(trial_dir: Path, session_id: str) -> list[Path]:
    """The native JSONL files Harbor converted into trajectory.json, in Harbor's order.

    Harbor's converter reads `<session dir>/*.jsonl` plus `<session dir>/**/subagents/*.jsonl`
    (`ClaudeCode._convert_events_to_trajectory`); the session dir is the one holding the
    Claude Code session id's own file. Empty if the agent wrote no session log.
    """
    projects = TrialPaths(trial_dir).agent_dir / "sessions" / "projects"
    owners = sorted(projects.glob(f"*/{session_id}.jsonl")) if projects.is_dir() else []
    if not owners:
        return []
    if len(owners) > 1:
        raise CompactionError(f"{trial_dir}: session {session_id} logged in {len(owners)} dirs")
    session_dir = owners[0].parent
    return sorted(session_dir.glob("*.jsonl")) + sorted(session_dir.rglob("subagents/*.jsonl"))


def _events(path: Path) -> list[dict[str, Any]]:
    events = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Harbor skips malformed lines too, so the trajectory has nothing from them.
            logger.warning("%s:%d: skipping malformed JSONL line", path, number)
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_compactions(trial_dir: Path, session_id: str) -> list[CompactionRecord]:
    """Every compaction boundary in the trial's session logs, in time order.

    A boundary replayed in more than one file (Harbor dedups events by uuid) is kept once.
    """
    agent_dir = TrialPaths(trial_dir).agent_dir
    records: dict[str, CompactionRecord] = {}
    for path in session_files(trial_dir, session_id):
        events = _events(path)
        summaries = {
            event["parentUuid"]: event["uuid"]
            for event in events
            if event.get("type") == "user"
            and event.get("isCompactSummary") is True
            and isinstance(event.get("parentUuid"), str)
            and isinstance(event.get("uuid"), str)
        }
        for event in events:
            if event.get("type") != "system" or event.get("subtype") != COMPACT_BOUNDARY_SUBTYPE:
                continue
            uuid = event.get("uuid")
            if not isinstance(uuid, str) or not uuid:
                raise CompactionError(f"{path}: compact_boundary event without a uuid")
            if uuid in records:
                continue
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                raise CompactionError(f"{path}: compact_boundary {uuid} without a timestamp")
            metadata = event.get("compactMetadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            trigger = metadata.get("trigger")
            records[uuid] = CompactionRecord(
                uuid=uuid,
                timestamp=datetime.fromisoformat(timestamp),
                native_file=path.relative_to(agent_dir).as_posix(),
                trigger=trigger if isinstance(trigger, str) else None,
                pre_tokens=_int_or_none(metadata.get("preTokens")),
                post_tokens=_int_or_none(metadata.get("postTokens")),
                duration_ms=_int_or_none(metadata.get("durationMs")),
                logical_parent_uuid=event.get("logicalParentUuid"),
                summary_uuid=summaries.get(uuid),
                is_sidechain=event.get("isSidechain") is True,
                agent_id=event.get("agentId"),
            )
    return sorted(records.values(), key=lambda record: (record.timestamp, record.uuid))
