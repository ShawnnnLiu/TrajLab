"""The checkpoint files under `<trial>/agent/checkpoints/` (docs/checkpoint-protocol.md, "Files").

`checkpoints.jsonl` is the capture-time join between tool calls and checkpoints; postprocess
turns it into ATIF system steps (ADR-0003).
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from trajlab.contracts import (
    CHECKPOINTS_DIRNAME,
    POLICY_FILENAME,
    CheckpointPolicy,
    CheckpointRecord,
)

RECORDS_FILENAME = "checkpoints.jsonl"
WATCHER_LOG_FILENAME = "watcher.log"
REQ_SUFFIX = ".req"
ACK_SUFFIX = ".ack"
TIMEOUT_SUFFIX = ".timeout"
# The hook refuses anything else, so a tool_use_id is always a safe file name.
TOOL_USE_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


class CheckpointRequest(BaseModel):
    """A `.req` file, as the hook writes it from its stdin."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tool_use_id: str = Field(pattern=TOOL_USE_ID_PATTERN)
    tool_name: str = Field(min_length=1)
    session_id: str | None = None
    agent_id: str | None = None


def checkpoints_dir(trial_dir: Path) -> Path:
    return trial_dir / "agent" / CHECKPOINTS_DIRNAME


def trial_dir_of(req_path: Path) -> Path:
    """`<trial>/agent/checkpoints/<id>.req` -> `<trial>`."""
    return req_path.parents[2]


def sibling(req_path: Path, suffix: str) -> Path:
    return req_path.with_suffix(suffix)


def is_pending(req_path: Path) -> bool:
    """A request that nobody has answered and the hook has not given up on."""
    return (
        req_path.is_file()
        and not sibling(req_path, ACK_SUFFIX).exists()
        and not sibling(req_path, TIMEOUT_SUFFIX).exists()
    )


def timed_out(req_path: Path) -> bool:
    return sibling(req_path, TIMEOUT_SUFFIX).exists()


def read_request(req_path: Path) -> tuple[CheckpointRequest, datetime]:
    """The request and its `requested_at`, the file's mtime (ADR-0006)."""
    request = CheckpointRequest.model_validate_json(req_path.read_text())
    if f"{request.tool_use_id}{REQ_SUFFIX}" != req_path.name:
        raise ValueError(f"{req_path}: names tool_use_id {request.tool_use_id!r}")
    requested_at = datetime.fromtimestamp(req_path.stat().st_mtime_ns / 1e9, UTC)
    return request, requested_at


def answered_requests(directory: Path) -> list[str]:
    """tool_use_ids of `.req` files with an `.ack` or a `.timeout`, in request order."""
    answered = [
        req
        for req in directory.glob(f"*{REQ_SUFFIX}")
        if sibling(req, ACK_SUFFIX).exists() or sibling(req, TIMEOUT_SUFFIX).exists()
    ]
    answered.sort(key=lambda req: (req.stat().st_mtime_ns, req.name))
    return [req.name.removesuffix(REQ_SUFFIX) for req in answered]


def read_policy(directory: Path) -> CheckpointPolicy | None:
    path = directory / POLICY_FILENAME
    if not path.is_file():
        return None
    return CheckpointPolicy.model_validate_json(path.read_text())


def write_policy(directory: Path, policy: CheckpointPolicy) -> None:
    _write_atomic(directory / POLICY_FILENAME, policy.model_dump_json() + "\n")


def read_records(directory: Path) -> list[CheckpointRecord]:
    path = directory / RECORDS_FILENAME
    if not path.is_file():
        return []
    return [
        CheckpointRecord.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def append_record(directory: Path, record: CheckpointRecord) -> None:
    with (directory / RECORDS_FILENAME).open("a") as stream:
        stream.write(record.model_dump_json() + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def write_ack(directory: Path, record: CheckpointRecord) -> Path:
    """Write `<tool_call_id>.ack` atomically; its appearance releases the hook."""
    path = directory / f"{record.tool_call_id}{ACK_SUFFIX}"
    _write_atomic(path, record.model_dump_json() + "\n")
    return path


def write_deferred_ack(directory: Path, tool_use_id: str) -> Path:
    """Release the hook for a call the next checkpoint will cover (ADR-0007)."""
    path = directory / f"{tool_use_id}{ACK_SUFFIX}"
    _write_atomic(path, json.dumps({"tool_call_id": tool_use_id, "deferred": True}) + "\n")
    return path
