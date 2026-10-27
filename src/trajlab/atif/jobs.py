"""Read Claude Code's background-job completion notices from the native session JSONL
(ADR-0020).

Claude Code moves a Bash command that reaches its foreground timeout to the background and
answers the call at once with a task id; the PostToolUse hook fires on that answer, so the
call's checkpoint and `t_end` are the moment of the move, not the job's end. When the job
finishes, Claude Code 2.1.278 queues a notice for the model's next turn and logs the queueing:

    {"type": "queue-operation", "operation": "enqueue", "timestamp": "...",
     "content": "<task-notification>\\n<task-id>b4cnj2dq1</task-id>\\n<tool-use-id>toolu_...
                 </tool-use-id>\\n...<status>completed</status>..."}

No user step carries the notice (it is removed again as `absorbed_mid_turn` or dequeued), so
the enqueue event's own timestamp is the record of when the job ended, within Claude Code's
polling. A notice enqueued twice (Harbor replays files) is kept once, by task id.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trajlab.atif.compaction import session_files

logger = logging.getLogger(__name__)

QUEUE_OPERATION_TYPE = "queue-operation"
ENQUEUE = "enqueue"
_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
_TOOL_USE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
_STATUS = re.compile(r"<status>([^<]+)</status>")


@dataclass(frozen=True)
class JobNotice:
    """One finished background job, as Claude Code reported it."""

    task_id: str
    tool_call_id: str
    status: str
    noticed_at: datetime  # aware


def read_job_notices(trial_dir: Path, session_id: str) -> list[JobNotice]:
    """Every background-job completion notice in the trial's session logs, in time order."""
    notices: dict[str, JobNotice] = {}
    for path in session_files(trial_dir, session_id):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if '"queue-operation"' not in line:
                continue  # cheap pre-check: the files are large and notices rare
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # compaction's reader warns about the line; one warning is enough
            if (
                not isinstance(event, dict)
                or event.get("type") != QUEUE_OPERATION_TYPE
                or event.get("operation") != ENQUEUE
            ):
                continue
            content = event.get("content")
            timestamp = event.get("timestamp")
            if not isinstance(content, str) or not isinstance(timestamp, str):
                continue
            task = _TASK_ID.search(content)
            call = _TOOL_USE_ID.search(content)
            status = _STATUS.search(content)
            if task is None or call is None or status is None:
                continue  # a queued notice of another kind
            if task.group(1) in notices:
                continue
            try:
                when = datetime.fromisoformat(timestamp)
            except ValueError:
                when = None
            if when is None or when.tzinfo is None:
                logger.warning(
                    "%s:%d: notice for task %s has no aware timestamp", path, number, task.group(1)
                )
                continue
            notices[task.group(1)] = JobNotice(
                task_id=task.group(1),
                tool_call_id=call.group(1),
                status=status.group(1),
                noticed_at=when,
            )
    return sorted(notices.values(), key=lambda notice: (notice.noticed_at, notice.task_id))
