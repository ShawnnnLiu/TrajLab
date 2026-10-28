"""Background-job completion notices from the native session JSONL (ADR-0020)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FIXTURE_TOOL_CALL_ID
from trajlab.atif.jobs import JobNotice, read_job_notices

SESSION_ID = "78b481c6-4620-425b-b2c0-ec60bce7422f"  # the fixture's session


def session_file(trial_dir: Path) -> Path:
    return trial_dir / "agent" / "sessions" / "projects" / "-app" / f"{SESSION_ID}.jsonl"


def notice_text(task: str, call: str = FIXTURE_TOOL_CALL_ID, status: str = "completed") -> str:
    return (
        f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{call}</tool-use-id>\n"
        f"<output-file>/tmp/claude-0/-app/x/tasks/{task}.output</output-file>\n"
        f'<status>{status}</status>\n<summary>Background command "sleep 300" {status}</summary>'
        "\n</task-notification>"
    )


def event(
    operation: str, content: str, timestamp: str = "2026-10-26T19:11:23.304Z", **extra: Any
) -> str:
    return json.dumps(
        {"type": "queue-operation", "operation": operation, "timestamp": timestamp}
        | {"content": content}
        | extra
    )


def append(trial_dir: Path, *lines: str) -> None:
    with session_file(trial_dir).open("a") as out:
        for line in lines:
            out.write(line + "\n")


def test_the_fixture_has_no_notices(fixture_trial: Path) -> None:
    assert read_job_notices(fixture_trial, SESSION_ID) == []


def test_a_notice_is_read_from_its_enqueue_event(trial_copy: Path) -> None:
    append(trial_copy, event("enqueue", notice_text("b4cnj2dq1")))
    assert read_job_notices(trial_copy, SESSION_ID) == [
        JobNotice(
            task_id="b4cnj2dq1",
            tool_call_id=FIXTURE_TOOL_CALL_ID,
            status="completed",
            noticed_at=datetime(2026, 10, 26, 19, 11, 23, 304000, tzinfo=UTC),
        )
    ]


def test_the_removal_of_a_notice_and_other_queued_content_are_not_notices(
    trial_copy: Path,
) -> None:
    append(
        trial_copy,
        event("enqueue", "Please continue."),
        event("enqueue", notice_text("b1", status="failed")),
        event("remove", notice_text("b1", status="failed"), reason="absorbed_mid_turn"),
        event("dequeue", notice_text("b1", status="failed")),
        json.dumps({"type": "user", "message": {"role": "user", "content": notice_text("b2")}}),
    )
    found = read_job_notices(trial_copy, SESSION_ID)
    assert [(n.task_id, n.status) for n in found] == [("b1", "failed")]


def test_a_notice_enqueued_twice_is_kept_once_and_the_list_is_in_time_order(
    trial_copy: Path,
) -> None:
    append(
        trial_copy,
        event("enqueue", notice_text("b2", call="toolu_2"), "2026-10-26T19:20:00Z"),
        event("enqueue", notice_text("b1", call="toolu_1"), "2026-10-26T19:10:00Z"),
        event("enqueue", notice_text("b1", call="toolu_1"), "2026-10-26T19:30:00Z"),
    )
    found = read_job_notices(trial_copy, SESSION_ID)
    assert [(n.task_id, n.noticed_at.minute) for n in found] == [("b1", 10), ("b2", 20)]


def test_a_notice_without_an_aware_timestamp_is_skipped_with_a_warning(
    trial_copy: Path, caplog: pytest.LogCaptureFixture
) -> None:
    append(
        trial_copy,
        event("enqueue", notice_text("b1"), "2026-10-26T19:10:00"),
        event("enqueue", notice_text("b2"), "yesterday"),
        "{not json",
    )
    with caplog.at_level("WARNING"):
        assert read_job_notices(trial_copy, SESSION_ID) == []
    assert caplog.text.count("has no aware timestamp") == 2


def test_a_trial_without_session_logs_has_no_notices(tmp_path: Path) -> None:
    assert read_job_notices(tmp_path, SESSION_ID) == []
