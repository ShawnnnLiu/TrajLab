"""What the watcher records per hooked call for the failure-pattern detector (ADR-0015 §2).

The hook keeps its whole input as `agent/checkpoints/<tool_call_id>.call.json`, beside the
`.req`. The watcher appends one `ProcessSnapshot` and one `CallChanges` per answered call to
files under `<trial>/trajlab/`, outside the bind-mounted `agent/` dir, so the agent never sees
them. Both are keyed by `tool_call_id` and `call_seq`, as in the call's `CallRecord`.
"""

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# `<tool_call_id>.call.json` beside the `.req`: the hook's stdin, verbatim.
CALL_INPUT_SUFFIX = ".call.json"
# Under the trial dir, not under agent/: host-side only.
TRAJLAB_DIRNAME = "trajlab"
PROCESSES_FILENAME = "processes.jsonl"
CHANGES_FILENAME = "changes.jsonl"
# The checker's output (ADR-0022), beside the extras.
FLAGS_FILENAME = "flags.jsonl"
CHECKS_FILENAME = "checks.jsonl"
# The text rule shared with ADR-0014's store: at most 1 MiB, no NUL byte, valid UTF-8.
MAX_TEXT_BYTES = 1024 * 1024

# Shared by the trace site (capture/) and the checker (checkpoint/), which import each other
# only through contracts/ (CLAUDE.md constraint 3). Kept as pattern strings, compiled where
# used: contracts/ imports nothing but typing, datetime, uuid, and pydantic.
# Commands that run tests or checks, for the trace site's success-claim hint. The checker
# extends this list with what the off corpus's agents ran (checkpoint/patterns.py).
TEST_COMMAND_PATTERN = (
    r"\b(?:pytest|unittest|tox|nox|ctest|jest|vitest|mocha)\b"
    r"|\b(?:npm|yarn|pnpm)(?: run)? test\b|\b(?:cargo|go) test\b"
    r"|\bmake(?: -C \S+)? (?:test|check)\b|\btest\.sh\b|\brun_tests\b"
)
# Processes of the harness, not the agent's: Claude Code and Harbor's launcher (whose command
# lines name claude), the checkpoint hook (whose script names itself) and its wait loop, and
# the container's keep-alive (pid 1 is `sh -c sleep infinity`).
HARNESS_PROCESS_PATTERN = r"claude|trajlab hook|^(?:sh -c )?sleep (?:0\.2|1|infinity)$"


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


# The checker's patterns in v0 (ADR-0022). ADR-0015 §1's others are left out for now.
FlagPattern = Literal[
    "self_kill", "silent_no_write", "masked_error", "unverified_completion", "missing_output"
]
CheckEvent = Literal["tool_call", "stop"]


class FlagRecord(BaseModel):
    """One problem the checker found, tied to the call (or stop) that triggered it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1, description="tool_use_id, or the stop request id.")
    call_seq: int = Field(ge=1)
    event: CheckEvent
    pattern: FlagPattern
    subtype: str | None = None
    evidence: dict[str, Any] = Field(description="What the flag rests on: paths, text, codes.")
    message: str = Field(description="The one line the `state` arm would send (ADR-0015 §4).")
    detector_version: str
    flagged_at: AwareDatetime


class HeldWrite(BaseModel):
    """A write the checker did not judge because a background job the call started still runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    pid: int
    cmd: str


class UnknownItem(BaseModel):
    """Something the records cannot decide; the checker records it instead of guessing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: FlagPattern
    reason: str
    path: str | None = None
    detail: str | None = None


class CheckRecord(BaseModel):
    """One run of the checker on one call or stop, flagged or not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1)
    call_seq: int = Field(ge=1)
    event: CheckEvent
    check_ms: float = Field(ge=0)
    flags: int = Field(ge=0)
    held: tuple[HeldWrite, ...] = ()
    unknown: tuple[UnknownItem, ...] = ()
    notes: tuple[str, ...] = Field(
        default=(),
        description="Calls the checks saw and passed over, with why (e.g. a "
        "visible failure), for the report's counts.",
    )
    detector_version: str
    checked_at: AwareDatetime
