"""The `writes` table: a trial's recorded writes joined to its delta rows (ADR-0021).

`trajlab record-writes` leaves one `WriteEvent` per write in `agent/trajlab/writes.jsonl`, with
the writing process as it was read at the event. Here each event's process is tied to the step
whose call started it, by one ordered rule, and the events on a delta row's path inside that
row's checkpoint interval (after the previous checkpoint's commit, up to this one's) are counted
per writer step. A path with no events of its own there takes those of the nearest directory
above it renamed into place (an added or modified path) or removed (a deleted one) in the
interval, kept in `via_dir`: a directory built under another name and renamed (`pip install
--target`, uv's cache) is one `moved_to` on the directory, and a recursive delete loses the
events of children whose directory is gone before the recorder resolves it.

The rule, first match wins:

1. `command`: the process ran under a Bash tool shell (`WriteEvent.shell`); its call is the
   Bash step whose command is the text the shell evals (Claude Code rewrites `>nul` to
   `>/dev/null` first, so both forms match). Two steps with the same command are told apart by
   the shell's start time: the step running when it started, else the latest started before.
2. `session`: no shell above it any more (it outlived its call and was reparented), but its
   session id is the pid of a tool shell seen at another event: that shell's step, as in 1.
   Claude Code starts each Bash call in a session of its own, which `nohup` and `&` keep.
3. `start_time`: neither, and not Claude Code's own process: the one Bash step whose window
   (`t_start` to `t_end`, give or take `SLACK`) holds the start time of the process's oldest
   ancestor below the container's init, or its own. A `setsid` child lands here.
4. `tool_window`: Claude Code's own process (a Write, Edit, or NotebookEdit call writes the file
   itself): the one write-tool step whose window holds the event's time.
5. Otherwise no step: Claude Code between calls, the harness, a program of the task image, or a
   process gone before it could be read.

Imports only `contracts` (ADR-0014): a store is built from a trial dir without capture's code.
"""

import hashlib
import json
import logging
import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import UUID

from harbor.models.trial.paths import TrialPaths
from pydantic import ValidationError

from trajlab.contracts import (
    WRITES_DIRNAME,
    WRITES_FILENAME,
    DeltaRow,
    StepRow,
    ToolShell,
    WriteEvent,
    WriterMapping,
    WriteRow,
)

logger = logging.getLogger(__name__)

# How far a process's start time may lie outside its call's window: the call's t_start is when
# Claude Code logged the request, t_end when the hook saw it finish, and /proc start times have
# a clock tick's resolution.
SLACK = timedelta(seconds=1)
# The event on a directory above a path with none of its own that stands for the path's: a
# directory renamed into place brings what it holds; a removed one took what it held.
CARRIER_OP = {"add": "moved_to", "modify": "moved_to", "delete": "delete"}
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# A group of events whose processes were tied to one step in several ways says the surest.
_SUREST: tuple[WriterMapping, ...] = ("command", "session", "tool_window", "start_time")
# Claude Code 2.1.278 rewrites a redirect to `nul` into one to /dev/null before it evals the
# command, unless the command has `<`, `$`, or a backquote (its `ELt`).
_NUL_REDIRECT = re.compile(r"(\d?&?>+[ \t]*)[Nn][Uu][Ll](?=\s|$|[|&;)\n])")


def writes_path(trial_dir: Path) -> Path:
    return TrialPaths(trial_dir).agent_dir / WRITES_DIRNAME / WRITES_FILENAME


def read_write_events(trial_dir: Path) -> list[WriteEvent]:
    """The trial's recorded write events in file order; none without the file. A line that does
    not read (a recorder killed mid-line) is skipped with a warning."""
    path = writes_path(trial_dir)
    if not path.is_file():
        return []
    events: list[WriteEvent] = []
    bad = 0
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            events.append(WriteEvent.model_validate_json(line))
        except ValidationError:
            bad += 1
    if bad:
        logger.warning("%s: %d line(s) that are not write events skipped", path, bad)
    return events


def command_hashes(command: str) -> set[str]:
    """The sha256 values a tool shell running `command` records (`ToolShell.command_sha256`)."""
    texts = {command}
    if not any(char in command for char in "<$`"):
        texts.add(_NUL_REDIRECT.sub(r"\g<1>/dev/null", command))
    return {hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest() for text in texts}


def _command(step: StepRow) -> str | None:
    if step.args_json is None:
        return None
    try:
        command = json.loads(step.args_json).get("command")
    except (ValueError, AttributeError):
        return None
    return command if isinstance(command, str) else None


def _holds(step: StepRow, at: datetime) -> bool:
    """`at` lies in the step's window, give or take SLACK."""
    if step.t_start is None:
        return False
    end = step.t_end or step.t_start
    return step.t_start - SLACK <= at <= end + SLACK


@dataclass(frozen=True)
class Writer:
    step_idx: int
    mapped_by: WriterMapping


class Writers:
    """Ties a trial's write events to its steps by the rule in the module docstring."""

    def __init__(self, steps: Sequence[StepRow], events: Sequence[WriteEvent]) -> None:
        self.bash = [s for s in steps if s.tool == "Bash" and s.t_start is not None]
        self.write_tools = [s for s in steps if s.tool in WRITE_TOOLS and s.t_start is not None]
        self.by_hash: dict[str, list[StepRow]] = defaultdict(list)
        for step in self.bash:
            command = _command(step)
            if command is not None:
                for digest in command_hashes(command):
                    self.by_hash[digest].append(step)
        self.shells: dict[int, list[ToolShell]] = defaultdict(list)
        for event in events:
            if event.shell is not None and event.shell not in self.shells[event.shell.pid]:
                self.shells[event.shell.pid].append(event.shell)

    def _by_command(self, shell: ToolShell) -> StepRow | None:
        candidates = self.by_hash.get(shell.command_sha256, [])
        if len(candidates) <= 1 or shell.started_at is None:
            return candidates[0] if candidates else None
        started = shell.started_at
        running = [s for s in candidates if _holds(s, started)]
        if running:
            return min(running, key=lambda s: (s.t_end or s.t_start or started, s.step_idx))
        before = [s for s in candidates if s.t_start is not None and s.t_start - SLACK <= started]
        return max(before, key=lambda s: (s.t_start, s.step_idx)) if before else None

    def _by_start(self, started: datetime | None) -> StepRow | None:
        if started is None:
            return None
        holding = [s for s in self.bash if _holds(s, started)]
        return holding[0] if len(holding) == 1 else None

    def _session_shell(self, event: WriteEvent) -> ToolShell | None:
        if event.sid is None:
            return None
        shells = self.shells.get(event.sid, [])
        if event.started_at is not None:
            # A pid used again later is another shell: keep those started before this process.
            shells = [
                s
                for s in shells
                if s.started_at is None or s.started_at <= event.started_at + SLACK
            ]
        return max(shells, key=lambda s: s.started_at or event.at) if shells else None

    def writer(self, event: WriteEvent) -> Writer | None:
        if event.shell is not None:
            step = self._by_command(event.shell)
            if step is not None:
                return Writer(step.step_idx, "command")
            step = self._by_start(event.shell.started_at)
            if step is not None:
                return Writer(step.step_idx, "start_time")
        session = self._session_shell(event)
        if session is not None:
            step = self._by_command(session) or self._by_start(session.started_at)
            if step is not None:
                return Writer(step.step_idx, "session")
        if event.agent:
            if event.shell is None:
                holding = [s for s in self.write_tools if _holds(s, event.at)]
                if len(holding) == 1:
                    return Writer(holding[0].step_idx, "tool_window")
            return None
        origin = next(
            (p for p in reversed(event.ancestry) if p.cpid != 1 and p.started_at is not None),
            None,
        )
        step = self._by_start(origin.started_at if origin is not None else event.started_at)
        return None if step is None else Writer(step.step_idx, "start_time")


def write_rows(
    trial_id: UUID,
    steps: Sequence[StepRow],
    deltas: Sequence[DeltaRow],
    events: Sequence[WriteEvent],
) -> tuple[WriteRow, ...]:
    """The `writes` rows of one trial: per delta row, the events on its path in its checkpoint
    interval, grouped by writer step (null for none)."""
    if not events or not deltas:
        return ()
    writers = Writers(steps, events)
    by_path: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for index, event in enumerate(events):
        by_path[event.path].append((event.at, index))
    for found in by_path.values():
        found.sort()
    at_seq = {s.checkpoint_seq: s.checkpoint_at for s in steps if s.checkpoint_seq is not None}
    owner = {s.step_idx: s for s in steps}
    cache: dict[int, Writer | None] = {}
    rows: list[WriteRow] = []
    for delta in deltas:
        step = owner.get(delta.step_idx)
        if step is None or step.checkpoint_seq is None or step.checkpoint_at is None:
            continue
        start = at_seq.get(step.checkpoint_seq - 1)
        end = step.checkpoint_at
        via_dir = None
        indices = _between(by_path.get(delta.path), start, end)
        if not indices:
            carrier = CARRIER_OP[delta.op]
            for parent in PurePosixPath(delta.path).parents:
                if str(parent) == "/":
                    break
                indices = [
                    index
                    for index in _between(by_path.get(str(parent)), start, end)
                    if events[index].op == carrier and events[index].is_dir
                ]
                if indices:
                    via_dir = str(parent)
                    break
        if not indices:
            continue
        groups: dict[int | None, list[WriteEvent]] = defaultdict(list)
        mapped: dict[int | None, WriterMapping | None] = {}
        for index in indices:
            if index not in cache:
                cache[index] = writers.writer(events[index])
            writer = cache[index]
            key = None if writer is None else writer.step_idx
            groups[key].append(events[index])
            if writer is not None:
                held = mapped.get(key)
                if held is None or _SUREST.index(writer.mapped_by) < _SUREST.index(held):
                    mapped[key] = writer.mapped_by
        for writer_step, group in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0])):
            rows.append(
                WriteRow(
                    trial_id=trial_id,
                    step_idx=delta.step_idx,
                    path=delta.path,
                    writer_step=writer_step,
                    mapped_by=mapped.get(writer_step),
                    events=len(group),
                    ops=tuple(sorted({e.op for e in group})),
                    first_at=group[0].at,
                    last_at=group[-1].at,
                    via_dir=via_dir,
                )
            )
    return tuple(rows)


def _between(
    found: list[tuple[datetime, int]] | None, start: datetime | None, end: datetime
) -> list[int]:
    """The indices of the events in `found` (sorted by time) after `start` and up to `end`."""
    if not found:
        return []
    times = [at for at, _ in found]
    low = 0 if start is None else bisect_right(times, start)
    return [index for _, index in found[low : bisect_right(times, end)]]
