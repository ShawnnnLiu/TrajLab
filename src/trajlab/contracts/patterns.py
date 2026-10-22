"""What the watcher records per hooked call for the failure-pattern detector (ADR-0015 §2).

The hook keeps its whole input as `agent/checkpoints/<tool_call_id>.call.json`, beside the
`.req`. The watcher appends one `ProcessSnapshot` and one `CallChanges` per answered call to
files under `<trial>/trajlab/`, outside the bind-mounted `agent/` dir, so the agent never sees
them. Both are keyed by `tool_call_id` and `call_seq`, as in the call's `CallRecord`.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# `<tool_call_id>.call.json` beside the `.req`: the hook's stdin, verbatim.
CALL_INPUT_SUFFIX = ".call.json"
# Under the trial dir, not under agent/: host-side only.
TRAJLAB_DIRNAME = "trajlab"
PROCESSES_FILENAME = "processes.jsonl"
CHANGES_FILENAME = "changes.jsonl"
# The text rule shared with ADR-0014's store: at most 1 MiB, no NUL byte, valid UTF-8.
MAX_TEXT_BYTES = 1024 * 1024


class ProcessEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(ge=1)
    ppid: int | None = Field(default=None, ge=0)
    cmd: str = Field(description="The command line, arguments joined by spaces, cut to 500 chars.")


class ListeningPort(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proto: Literal["tcp", "tcp6"]
    address: str
    port: int = Field(ge=0, le=65535)


class ProcessSnapshot(BaseModel):
    """The container's running programs and listening TCP ports right after one hooked call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1)
    call_seq: int = Field(ge=1)
    processes: tuple[ProcessEntry, ...]
    listening: tuple[ListeningPort, ...]
    capture_ms: int = Field(ge=0)
    captured_at: AwareDatetime


FileOp = Literal["added", "modified", "removed"]
FileKind = Literal["file", "symlink", "other"]
SkipReason = Literal[
    "not_text", "too_large", "too_many_lines", "over_file_cap", "over_budget", "unreadable"
]


class FileChange(BaseModel):
    """One path a call changed. Hashes are of the raw bytes; a diff only for text both sides."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    op: FileOp
    kind: FileKind
    size: int | None = Field(default=None, ge=0, description="After the call; null if removed.")
    sha256: str | None = Field(default=None, description="Content after the call, if read.")
    before_sha256: str | None = Field(default=None, description="Content before, if known.")
    is_text: bool | None = None
    diff: str | None = Field(default=None, description="Unified diff, 3 lines of context.")
    skipped: SkipReason | None = Field(
        default=None, description="Why there is no diff or no hash, when there is none."
    )


class CallChanges(BaseModel):
    """The content of what one hooked call changed, as far as the limits allow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1)
    call_seq: int = Field(ge=1)
    files: tuple[FileChange, ...]
    files_total: int = Field(ge=0, description="Changed files and symlinks, before any cap.")
    baseline: bool = Field(
        default=False,
        description="The trial's first measured call: no earlier listing, so no changes known.",
    )
    capture_ms: int = Field(ge=0)
