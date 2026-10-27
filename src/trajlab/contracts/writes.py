"""What the write recorder records per file event in a trial's container (ADR-0021).

`trajlab record-writes` runs as root on the host beside the watcher and appends one `WriteEvent`
per event to `agent/trajlab/writes.jsonl` in the trial dir: a create, delete, rename into
place, close after writing, or write (at most one `modify` per process and path per second) in
the container's upper directory, with the process that made it. Which call a process belongs to
is not decided here: the store maps it from `shell`, `sid`, and the start times
(`trajlab.store.writes`), against the trial's steps.

Times are aware UTC on the host clock, which the container shares on Linux. Pids are host pids
unless named `cpid` (the pid inside the container, the last `NSpid` value).
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# Under the trial's agent/ dir, the bind mount: `<trial>/agent/trajlab/writes.jsonl`.
WRITES_DIRNAME = "trajlab"
WRITES_FILENAME = "writes.jsonl"
# A writer's command line, arguments joined by spaces, cut to this many characters.
MAX_CMD_CHARS = 500
# The start of a Bash call's command kept for reading; matching uses the whole text's sha256.
MAX_COMMAND_CHARS = 200

WriteOp = Literal["create", "modify", "close_write", "delete", "moved_to"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcessRef(_Model):
    """One process above the writer, as the recorder read it at the event."""

    pid: int = Field(ge=1, description="Host pid.")
    cpid: int | None = Field(default=None, ge=1, description="Pid in the container.")
    started_at: AwareDatetime | None = Field(default=None, description="From /proc stat.")
    comm: str = Field(description="The kernel's short name for it.")


class ToolShell(_Model):
    """The shell Claude Code ran a Bash call in: `bash -c '... && eval <command> ...'`."""

    pid: int = Field(ge=1, description="Host pid; also its session id (a new session per call).")
    cpid: int | None = Field(default=None, ge=1)
    started_at: AwareDatetime | None = None
    command_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 of the command text the shell evals, UTF-8; the call's `command` "
        "argument, or that with `>nul` redirects rewritten as Claude Code does.",
    )
    command: str = Field(description=f"Its first {MAX_COMMAND_CHARS} characters.")


class WriteEvent(_Model):
    """One write in a trial container's upper directory and the process that made it."""

    at: AwareDatetime = Field(description="When the recorder read the event (host clock).")
    op: WriteOp
    path: str = Field(
        pattern=r"^/",
        description="Container path; a byte that is not UTF-8 is written \\xNN, as in the store.",
    )
    is_dir: bool = Field(default=False, description="The event is about a directory.")
    pid: int = Field(ge=1, description="The writer's host pid, as fanotify reported it.")
    cpid: int | None = Field(default=None, ge=1)
    comm: str | None = None
    cmd: str | None = Field(default=None, description=f"Cut to {MAX_CMD_CHARS} characters.")
    started_at: AwareDatetime | None = None
    sid: int | None = Field(
        default=None, ge=0, description="Session id: the host pid of the session leader."
    )
    ancestry: tuple[ProcessRef, ...] = Field(
        default=(), description="The writer's parents, nearest first, up to the container's root."
    )
    agent: bool = Field(
        default=False, description="Claude Code's own process is the writer or one of its parents."
    )
    shell: ToolShell | None = Field(
        default=None,
        description="The Bash call's shell among the writer and its parents, outermost first; "
        "for a process whose parents are gone, the one seen above it at an earlier event.",
    )
    gone: bool = Field(
        default=False,
        description="The writer had exited when the recorder read /proc; what is known of it "
        "comes from an earlier event of the same process, if any.",
    )
