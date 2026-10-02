"""Compaction recovery, checked against Harbor's own converter.

The fixture never compacted, so these tests add the two events Claude Code 2.1.278 writes on a
compaction (a `compact_boundary` system event, then an `isCompactSummary` user event) to a copy
of the fixture's session JSONL, regenerate trajectory.json with Harbor's converter as a real
trial would, and postprocess the result.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from harbor.agents.installed.claude_code import ClaudeCode
from pydantic import TypeAdapter

from tests.conftest import make_call, make_checkpoint, write_capture
from trajlab.atif.compaction import CompactionError, read_compactions, session_files
from trajlab.atif.load import load_trajectory
from trajlab.atif.postprocess import enriched_trajectory_path, postprocess_trial
from trajlab.contracts import (
    CONTEXT_MANAGEMENT_EXTRA_KEY,
    TRAJLAB_EXTRA_KEY,
    CompactionRecord,
    ContextManagementExtra,
    TrajlabStepExtra,
)

SESSION_ID = "78b481c6-4620-425b-b2c0-ec60bce7422f"
SESSION_REL = f"sessions/projects/-app/{SESSION_ID}.jsonl"
BOUNDARY_UUID = "0b5e1d0a-0000-4000-8000-00000000c0de"
SUMMARY_UUID = "0b5e1d0a-0000-4000-8000-0000000005aa"
# The fixture's tool result is at 00:49:17.232Z and its final assistant message at 18.565Z.
BOUNDARY_TIME = "2026-09-22T00:49:17.900Z"
SUMMARY_TEXT = "This session is being continued from a previous conversation. Summary: wrote hello."


def _envelope(**fields: Any) -> dict[str, Any]:
    return {
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "sdk-cli",
        "cwd": "/app",
        "sessionId": SESSION_ID,
        "version": "2.1.278",
        "gitBranch": "HEAD",
    } | fields


def _boundary(uuid: str = BOUNDARY_UUID, timestamp: str = BOUNDARY_TIME, **fields: Any):
    return _envelope(
        parentUuid=None,
        logicalParentUuid="a7d61de5-d4e8-4eaf-a2e5-2df6698f7062",
        type="system",
        subtype="compact_boundary",
        content="Conversation compacted",
        isMeta=False,
        timestamp=timestamp,
        uuid=uuid,
        level="info",
        compactMetadata={
            "trigger": "auto",
            "preTokens": 167_000,
            "postTokens": 9_500,
            "durationMs": 21_000,
        },
        **fields,
    )


def _summary(parent: str = BOUNDARY_UUID, uuid: str = SUMMARY_UUID, **fields: Any):
    return _envelope(
        parentUuid=parent,
        type="user",
        message={"role": "user", "content": SUMMARY_TEXT},
        isVisibleInTranscriptOnly=True,
        isCompactSummary=True,
        uuid=uuid,
        timestamp="2026-09-22T00:49:17.901Z",
        **fields,
    )


def _insert_after_tool_result(trial_dir: Path, *events: dict[str, Any]) -> None:
    path = trial_dir / "agent" / SESSION_REL
    lines = path.read_text().splitlines()
    at = next(i for i, line in enumerate(lines) if '"tool_use_id"' in line) + 1
    lines[at:at] = [json.dumps(event) for event in events]
    path.write_text("\n".join(lines) + "\n")


def _reconvert(trial_dir: Path) -> None:
    """Rewrite trajectory.json the way Harbor does after the agent run."""
    agent = ClaudeCode(logs_dir=trial_dir / "agent", model_name="anthropic/claude-sonnet-5")
    session_dir = (trial_dir / "agent" / SESSION_REL).parent
    trajectory = agent._convert_events_to_trajectory(session_dir)
    assert trajectory is not None
    (trial_dir / "agent" / "trajectory.json").write_text(
        json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False)
    )


def _kind(step: Any) -> str:
    return TypeAdapter(TrajlabStepExtra).validate_python(step.extra[TRAJLAB_EXTRA_KEY]).kind


def test_fixture_has_no_compaction(fixture_trial: Path) -> None:
    assert read_compactions(fixture_trial, SESSION_ID) == []
    assert [p.name for p in session_files(fixture_trial, SESSION_ID)] == [f"{SESSION_ID}.jsonl"]


def test_unknown_session_has_no_files(fixture_trial: Path) -> None:
    assert session_files(fixture_trial, "no-such-session") == []


def test_reads_boundary_and_links_summary(trial_copy: Path) -> None:
    _insert_after_tool_result(trial_copy, _boundary(), _summary())

    assert read_compactions(trial_copy, SESSION_ID) == [
        CompactionRecord(
            uuid=BOUNDARY_UUID,
            timestamp=datetime(2026, 9, 22, 0, 49, 17, 900000, tzinfo=UTC),
            native_file=SESSION_REL,
            trigger="auto",
            pre_tokens=167_000,
            post_tokens=9_500,
            duration_ms=21_000,
            logical_parent_uuid="a7d61de5-d4e8-4eaf-a2e5-2df6698f7062",
            summary_uuid=SUMMARY_UUID,
        )
    ]


def test_compaction_step_precedes_harbors_summary_step(trial_copy: Path) -> None:
    _insert_after_tool_result(trial_copy, _boundary(), _summary())
    _reconvert(trial_copy)
    original = load_trajectory(trial_copy / "agent" / "trajectory.json")
    # Harbor drops the boundary and keeps the summary as a user step.
    assert [s.source for s in original.steps] == ["user", "agent", "user", "agent"]
    assert original.steps[2].message == SUMMARY_TEXT

    result = postprocess_trial(trial_copy)

    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    assert result.compactions == 1
    assert [_kind(s) for s in enriched.steps] == [
        "original",
        "original",
        "compaction",
        "original",
        "original",
    ]
    step = enriched.steps[2]
    assert step.source == "system"
    assert step.message == "context compaction"
    assert ContextManagementExtra.model_validate(
        step.extra[CONTEXT_MANAGEMENT_EXTRA_KEY]  # type: ignore[index]
    ) == ContextManagementExtra(type="compaction", boundary="replace")
    record = CompactionRecord.model_validate(step.observation.results[0].extra)  # type: ignore[union-attr]
    assert record.summary_uuid == SUMMARY_UUID
    assert enriched.steps[3].message == SUMMARY_TEXT


def test_compaction_follows_the_checkpoints_of_the_previous_step(trial_copy: Path) -> None:
    _insert_after_tool_result(trial_copy, _boundary(), _summary())
    _reconvert(trial_copy)
    write_capture(trial_copy, [make_checkpoint(1)], [make_call(1)])

    postprocess_trial(trial_copy)

    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    assert [_kind(s) for s in enriched.steps] == [
        "original",
        "original",
        "checkpoint",
        "compaction",
        "original",
        "original",
    ]


def test_compaction_after_the_last_step_goes_last(trial_copy: Path) -> None:
    _insert_after_tool_result(trial_copy, _boundary(timestamp="2026-09-22T00:50:00.000Z"))

    postprocess_trial(trial_copy)

    enriched = load_trajectory(enriched_trajectory_path(trial_copy))
    assert [_kind(s) for s in enriched.steps][-1] == "compaction"


def test_subagent_compaction_is_marked_sidechain(trial_copy: Path) -> None:
    subagents = (trial_copy / "agent" / SESSION_REL).parent / SESSION_ID / "subagents"
    subagents.mkdir(parents=True)
    boundary = _boundary(isSidechain=True, agentId="a1b2c3")
    # Harbor dedups events by uuid; a boundary seen twice is one compaction.
    (subagents / "agent-a1b2c3.jsonl").write_text(json.dumps(boundary) + "\n")
    _insert_after_tool_result(trial_copy, boundary)

    records = read_compactions(trial_copy, SESSION_ID)

    assert len(records) == 1
    assert records[0].native_file == SESSION_REL
    (trial_copy / "agent" / SESSION_REL).write_text(
        (trial_copy / "agent" / SESSION_REL).read_text().replace(BOUNDARY_UUID, "other")
    )
    by_file = {r.native_file: r for r in read_compactions(trial_copy, SESSION_ID)}
    sidechain = by_file[f"sessions/projects/-app/{SESSION_ID}/subagents/agent-a1b2c3.jsonl"]
    assert sidechain.is_sidechain
    assert sidechain.agent_id == "a1b2c3"


def test_malformed_line_is_skipped_like_harbor(
    trial_copy: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = trial_copy / "agent" / SESSION_REL
    path.write_text(path.read_text() + "{not json\n" + json.dumps(_boundary()) + "\n")

    with caplog.at_level(logging.WARNING):
        records = read_compactions(trial_copy, SESSION_ID)

    assert [r.uuid for r in records] == [BOUNDARY_UUID]
    assert "malformed JSONL line" in caplog.text


@pytest.mark.parametrize("missing", ["uuid", "timestamp"])
def test_boundary_without_identity_fails(trial_copy: Path, missing: str) -> None:
    boundary = _boundary()
    del boundary[missing]
    _insert_after_tool_result(trial_copy, boundary)

    with pytest.raises(CompactionError):
        read_compactions(trial_copy, SESSION_ID)
