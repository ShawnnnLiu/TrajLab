"""The write recorder (ADR-0021): which process wrote each file of a running trial's container.

One fanotify group (`trajlab.checkpoint.fanotify`) marks the host filesystem that holds the
containerd snapshots (`FAN_MARK_FILESYSTEM`: the containers' overlay mounts are not in the host's
mount namespace) for creates, deletes, renames into place, closes after writing, and writes. A
write a container makes through its overlay reaches the upper directory on that filesystem with
the writer's host pid, so an event maps to a trial by the upper directory, read from the
container init's `mountinfo`, and the container path is the event path with it removed. Events
of host processes are dropped by pid namespace before any path is resolved.

At each event the recorder reads the writer's `/proc` entry at once, since a short-lived process
is soon gone: its container pid (`NSpid`), command line, session id, start time, and its parents
up to the container's root. It appends one `WriteEvent` per event to the trial's
`agent/trajlab/writes.jsonl` (a `modify` at most once per process and path per second). Each
Bash call's shell first sources its shell snapshot from `/logs/agent/sessions/shell-snapshots/`,
which is on the host's filesystem, so an inode mark on that directory (`FAN_OPEN`) gives one
`open` line per call with the shell read at its start, whether or not it writes. Which
call a process belongs to is decided in the store (`trajlab.store.writes`): Claude Code 2.1.278
runs a Bash call as `bash -c '<setup> && eval <command, shell-quoted> [< /dev/null] && pwd -P >|
/tmp/claude-<id>-cwd'` in a session of its own (`detached: true`), so the call's shell carries
its command text (`tool_command`), and a child that outlives it keeps its session id.

Trials are found as the watcher finds them: `checkpoint/` does not import `capture/`, so the CLI
supplies how a trial dir maps to its name and compose project, and the backend finds its
container. One recorder per jobs dir, held by a `flock` on `<jobs-dir>/.trajlab-recorder.lock`.
"""

import fcntl
import hashlib
import logging
import os
import re
import resource
import select
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from harbor.models.trial.paths import TrialPaths

from trajlab.checkpoint.fanotify import (
    FAN_CLOSE_WRITE,
    FAN_CREATE,
    FAN_DELETE,
    FAN_MODIFY,
    FAN_MOVED_TO,
    FAN_ONDIR,
    FAN_OPEN,
    FAN_Q_OVERFLOW,
    DirectoryPaths,
    Fanotify,
    RawEvent,
    parse_events,
)
from trajlab.checkpoint.watcher import TrialIdentity
from trajlab.contracts import (
    MAX_CMD_CHARS,
    MAX_COMMAND_CHARS,
    WRITES_DIRNAME,
    WRITES_FILENAME,
    ProcessRef,
    ToolShell,
    WriteEvent,
    WriteOp,
)

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".trajlab-recorder.lock"
# Where Claude Code keeps the shell snapshot every Bash call sources first, under agent/; the
# agent dir is /logs/agent in the container.
SHELL_SNAPSHOTS = ("sessions", "shell-snapshots")
AGENT_MOUNT = b"/logs/agent"
# Where the containerd overlayfs snapshotter keeps its snapshots: the filesystem marked.
DEFAULT_SNAPSHOTS = Path("/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs")
MASK = FAN_CREATE | FAN_DELETE | FAN_MOVED_TO | FAN_CLOSE_WRITE | FAN_MODIFY | FAN_ONDIR
# The order a merged event's operations are written in (the kernel keeps no order).
OPS: tuple[tuple[int, WriteOp], ...] = (
    (FAN_CREATE, "create"),
    (FAN_MOVED_TO, "moved_to"),
    (FAN_MODIFY, "modify"),
    (FAN_CLOSE_WRITE, "close_write"),
    (FAN_DELETE, "delete"),
)
DEFAULT_DISCOVER_INTERVAL_S = 1.0
# A process read this recently is not read again, unless its start time changed (a new pid).
PROCESS_FRESH_S = 2.0
PIDNS_FRESH_S = 30.0
# Events under the snapshots of a container not yet known wait this long for discovery.
PENDING_S = 15.0
PENDING_MAX = 50_000
FLUSH_S = 0.5
FLUSH_BYTES = 256 * 1024
STATS_S = 60.0
MAX_ANCESTRY = 32
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


class RecorderLockedError(RuntimeError):
    """Another recorder already holds this jobs dir."""


def lock_path(jobs_dir: Path) -> Path:
    return jobs_dir / LOCK_FILENAME


@contextmanager
def hold_recorder_lock(jobs_dir: Path) -> Iterator[None]:
    """Hold the jobs dir's recorder lock for the duration. Raises RecorderLockedError."""
    with lock_path(jobs_dir).open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecorderLockedError(f"a recorder already holds {lock_path(jobs_dir)}") from error
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n")
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def recorder_running(jobs_dir: Path) -> bool:
    """True if some process holds the jobs dir's recorder lock."""
    path = lock_path(jobs_dir)
    if not path.is_file():
        return False
    with path.open("r") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


def writes_path(trial_dir: Path) -> Path:
    return TrialPaths(trial_dir).agent_dir / WRITES_DIRNAME / WRITES_FILENAME


# -- What /proc says, parsed ------------------------------------------------------------------


@dataclass(frozen=True)
class Stat:
    """The fields of `/proc/<pid>/stat` the recorder uses."""

    comm: str
    state: str
    ppid: int
    sid: int
    start_ticks: int  # clock ticks after boot


def parse_stat(text: str) -> Stat | None:
    """`/proc/<pid>/stat`; the name may hold spaces and parentheses, so it ends at the last `)`."""
    close = text.rfind(")")
    if close < 0 or "(" not in text[:close]:
        return None
    fields = text[close + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return Stat(
            comm=text[text.index("(") + 1 : close],
            state=fields[0],
            ppid=int(fields[1]),
            sid=int(fields[3]),
            start_ticks=int(fields[19]),
        )
    except ValueError:
        return None


def parse_nspid(status: str) -> tuple[int, ...]:
    """The `NSpid` line of `/proc/<pid>/status`: the pid in each namespace, outermost first."""
    for line in status.splitlines():
        if line.startswith("NSpid:"):
            return tuple(int(value) for value in line.split()[1:] if value.isdigit())
    return ()


_MOUNTINFO_ESCAPE = re.compile(rb"\\([0-7]{3})")


def overlay_upperdir(mountinfo: bytes) -> bytes | None:
    """The upper directory of the overlay mounted at `/`, from a container process's
    `/proc/<pid>/mountinfo`; None if `/` is not an overlay."""
    for line in mountinfo.splitlines():
        before, sep, after = line.partition(b" - ")
        fields, tail = before.split(), after.split()
        if not sep or len(fields) < 5 or len(tail) < 3:
            continue
        if fields[4] != b"/" or tail[0] != b"overlay":
            continue
        for option in tail[2].split(b","):
            if option.startswith(b"upperdir="):
                raw = option.removeprefix(b"upperdir=")
                return _MOUNTINFO_ESCAPE.sub(lambda m: bytes([int(m.group(1), 8)]), raw)
    return None


# -- The Bash tool's shell --------------------------------------------------------------------

_CWD_SUFFIX = re.compile(r" && pwd -P >\| \S+\s*$")
_EVAL = re.compile(r"(?:^|&& )eval ")
_TAIL = ("", " < /dev/null")


def shell_word(text: str, start: int) -> tuple[str, int] | None:
    """One POSIX shell word of `text` from `start` (quotes removed, escapes applied) and where
    it ends; None if a quote is not closed."""
    out: list[str] = []
    i = start
    while i < len(text) and not text[i].isspace():
        char = text[i]
        if char == "'":
            end = text.find("'", i + 1)
            if end < 0:
                return None
            out.append(text[i + 1 : end])
            i = end + 1
        elif char == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in '$`"\\\n':
                    if text[i + 1] != "\n":
                        out.append(text[i + 1])
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            if i >= len(text):
                return None
            i += 1
        elif char == "\\" and i + 1 < len(text):
            if text[i + 1] != "\n":
                out.append(text[i + 1])
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out), i


def tool_command(argv: Sequence[str]) -> str | None:
    """The command a Claude Code Bash tool shell evals, from the shell's argv
    (`bash -c [-l] '<setup> && eval <word> [< /dev/null] && pwd -P >| <cwd file>'`); None for
    any other process."""
    if len(argv) < 3 or not argv[0].endswith("sh") or "-c" not in argv[1:3]:
        return None
    script = argv[-1]
    suffix = _CWD_SUFFIX.search(script)
    if suffix is None:
        return None
    body = script[: suffix.start()]
    for match in _EVAL.finditer(body):
        parsed = shell_word(body, match.end())
        if parsed is not None and body[parsed[1] :] in _TAIL:
            return parsed[0]
    return None


def is_claude_code(argv: Sequence[str], comm: str) -> bool:
    """Claude Code's own process (`claude --verbose --output-format=stream-json ... --print`)."""
    return comm == "claude" or (bool(argv) and os.path.basename(argv[0]) == "claude")


# -- Reading processes -------------------------------------------------------------------------


@dataclass
class _Process:
    pid: int
    stat: Stat
    cpid: int | None  # None: the process is in the host's pid namespace
    argv: tuple[str, ...]
    own_shell: ToolShell | None
    started_at: datetime
    read_at: float


@dataclass
class _Reading:
    """A writer as read at its event, or as last read before it was gone."""

    pid: int
    process: _Process | None
    ancestry: tuple[_Process, ...]
    shell: ToolShell | None
    agent: bool
    gone: bool


class Processes:
    """Reads processes from a `/proc`, caching each a short while by pid and start time."""

    def __init__(self, proc: Path = Path("/proc")) -> None:
        self.proc = proc
        self._cache: dict[int, _Process] = {}
        # The outermost Bash tool shell found above a process, kept once its parents are gone.
        self._shells: dict[tuple[int, int], ToolShell] = {}
        self._boot = 0.0
        self._boot_read = 0.0

    def _boot_time(self) -> float:
        now = time.monotonic()
        if now - self._boot_read > 60:
            self._boot = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
            self._boot_read = now
        return self._boot

    def _read(self, pid: int) -> _Process | None:
        base = self.proc / str(pid)
        try:
            stat = parse_stat((base / "stat").read_text(errors="replace"))
        except OSError:
            return None
        if stat is None:
            return None
        now = time.monotonic()
        cached = self._cache.get(pid)
        if (
            cached is not None
            and cached.stat.start_ticks == stat.start_ticks
            and now - cached.read_at < PROCESS_FRESH_S
        ):
            return cached
        try:
            nspid = parse_nspid((base / "status").read_text(errors="replace"))
            raw = (base / "cmdline").read_bytes()
        except OSError:
            nspid, raw = (), b""
        argv = tuple(arg.decode("utf-8", "backslashreplace") for arg in raw.split(b"\0") if arg)
        if not argv and cached is not None and cached.stat.start_ticks == stat.start_ticks:
            argv = cached.argv  # a zombie has no command line left
        started_at = datetime.fromtimestamp(self._boot_time() + stat.start_ticks / CLOCK_TICKS, UTC)
        command = tool_command(argv)
        cpid = nspid[-1] if len(nspid) > 1 else None
        process = _Process(
            pid=pid,
            stat=stat,
            cpid=cpid,
            argv=argv,
            own_shell=None
            if command is None
            else ToolShell(
                pid=pid,
                cpid=cpid,
                started_at=started_at,
                command_sha256=hashlib.sha256(command.encode("utf-8", "surrogatepass")).hexdigest(),
                command=command[:MAX_COMMAND_CHARS],
            ),
            started_at=started_at,
            read_at=now,
        )
        if len(self._cache) > 200_000:
            self._cache.clear()
        self._cache[pid] = process
        return process

    def reading(self, pid: int) -> _Reading:
        """The writer `pid` and its parents inside its container, read now."""
        process = self._read(pid)
        if process is None:
            last = self._cache.get(pid)
            shell = (
                None
                if last is None
                else self._shells.get((pid, last.stat.start_ticks)) or last.own_shell
            )
            return _Reading(pid, last, (), shell, False, gone=True)
        ancestry: list[_Process] = []
        parent = process.stat.ppid
        while parent > 0 and len(ancestry) < MAX_ANCESTRY:
            above = self._read(parent)
            if above is None or above.cpid is None:
                break  # gone, or outside the container (the runtime's shim)
            ancestry.append(above)
            parent = above.stat.ppid
        chain = (process, *ancestry)
        key = (pid, process.stat.start_ticks)
        shell = next((p.own_shell for p in reversed(chain) if p.own_shell is not None), None)
        if shell is not None:
            self._shells[key] = shell
        else:
            shell = self._shells.get(key)
        agent = any(is_claude_code(p.argv, p.stat.comm) for p in chain)
        return _Reading(pid, process, tuple(ancestry), shell, agent, gone=False)

    def forget_before(self, horizon: float) -> None:
        """Drop processes last read before `horizon` (monotonic seconds)."""
        for pid in [pid for pid, p in self._cache.items() if p.read_at < horizon]:
            del self._cache[pid]


def event_line(at: datetime, op: WriteOp, path: str, is_dir: bool, reading: _Reading) -> WriteEvent:
    """One `WriteEvent` from what was read about its writer."""
    process = reading.process
    return WriteEvent(
        at=at,
        op=op,
        path=path,
        is_dir=is_dir,
        pid=reading.pid,
        cpid=None if process is None else process.cpid,
        comm=None if process is None else process.stat.comm,
        cmd=None if process is None else " ".join(process.argv)[:MAX_CMD_CHARS],
        started_at=None if process is None else process.started_at,
        # /proc can say -1 for a process whose session it cannot name (seen on a live trial).
        sid=None if process is None or process.stat.sid < 0 else process.stat.sid,
        ancestry=tuple(
            ProcessRef(pid=p.pid, cpid=p.cpid, started_at=p.started_at, comm=p.stat.comm)
            for p in reading.ancestry
        ),
        agent=reading.agent,
        shell=reading.shell,
        gone=reading.gone,
    )


# -- Trials --------------------------------------------------------------------------------------


class Containers(Protocol):
    """How a compose project's main container and its init process are found."""

    def container_for(self, compose_project: str) -> str: ...

    def init_pid(self, container: str) -> int: ...


@dataclass
class _Trial:
    trial_dir: Path
    name: str
    container: str
    init_pid: int
    init_start: int
    upper: bytes
    pidns: str
    out: BinaryIO
    agent: bytes  # the agent dir on the host, which the container sees as /logs/agent
    shells_marked: Path | None = None  # the shell-snapshot dir, once marked
    pending: list[bytes] = field(default_factory=list)
    pending_bytes: int = 0
    ops: Counter[str] = field(default_factory=Counter)
    gone: int = 0


@dataclass(frozen=True)
class _Held:
    """An event under the snapshots of a container no trial is known for yet."""

    at: float
    event: RawEvent
    path: bytes
    reading: _Reading


def _owner(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_uid, info.st_gid


class Recorder:
    """Records the writes of every running trial under a jobs dir (ADR-0021)."""

    def __init__(
        self,
        jobs_dir: Path,
        identify: Callable[[Path], TrialIdentity],
        containers: Containers,
        *,
        snapshots: Path = DEFAULT_SNAPSHOTS,
        discover_interval: float = DEFAULT_DISCOVER_INTERVAL_S,
        proc: Path = Path("/proc"),
    ) -> None:
        self.jobs_dir = jobs_dir.resolve()
        self.identify = identify
        self.containers = containers
        self.snapshots = snapshots
        self.discover_interval = discover_interval
        self.proc = proc
        self.processes = Processes(proc)
        self._trials: dict[Path, _Trial] = {}
        self._lock = threading.Lock()
        self._held: deque[_Held] = deque(maxlen=PENDING_MAX)
        self._wake = threading.Event()
        self._modified: dict[tuple[int, bytes], int] = {}
        self._pidns: dict[int, tuple[str | None, float]] = {}
        self._own_pid = os.getpid()
        self._host_pidns = os.readlink(proc / "self" / "ns" / "pid")
        self._snapshots_prefix = os.fsencode(snapshots) + b"/"
        self._failures: dict[Path, str] = {}
        self.stats: Counter[str] = Counter()
        # Marks (or unmarks) a directory for opens of its files; set by `run`.
        self.mark_directory: Callable[[Path, bool], None] | None = None
        self.reader_failed = False

    # Trials ------------------------------------------------------------------------------

    def _running_trial_dirs(self) -> list[Path]:
        return sorted(
            config.parent
            for config in self.jobs_dir.glob("*/*/config.json")
            if not TrialPaths(config.parent).result_path.exists()
        )

    def _alive(self, trial: _Trial) -> bool:
        try:
            stat = parse_stat((self.proc / str(trial.init_pid) / "stat").read_text())
        except OSError:
            return False
        return stat is not None and stat.start_ticks == trial.init_start

    def discover(self) -> None:
        """Take on trials whose container has started; let go of trials that ended."""
        running = self._running_trial_dirs()
        for trial_dir in running:
            if trial_dir in self._trials:
                continue
            try:
                trial = self._open(trial_dir)
            except Exception as error:  # not started yet, or gone: try again next pass
                message = str(error)
                if self._failures.get(trial_dir) != message:
                    logger.debug("not yet recording %s: %s", trial_dir.name, message)
                    self._failures[trial_dir] = message
                continue
            self._failures.pop(trial_dir, None)
            with self._lock:
                self._trials[trial_dir] = trial
                self._release_held()
            logger.info(
                "recording %s: container %s, init pid %d, upper %s",
                trial.name,
                trial.container[:12],
                trial.init_pid,
                os.fsdecode(trial.upper),
            )
        for trial in list(self._trials.values()):
            if trial.shells_marked is None:
                self._mark_shells(trial)
        ended = [
            trial
            for trial_dir, trial in list(self._trials.items())
            if trial_dir not in running or not self._alive(trial)
        ]
        for trial in ended:
            with self._lock:
                self._trials.pop(trial.trial_dir, None)
                self._flush(trial)
            trial.out.close()
            if trial.shells_marked is not None and self.mark_directory is not None:
                try:
                    self.mark_directory(trial.shells_marked, True)
                except OSError:
                    pass  # the directory is gone with its mark
            logger.info(
                "stopped recording %s: %d event lines (%s), %d with the writer gone",
                trial.name,
                trial.ops.total(),
                ", ".join(f"{op} {n}" for op, n in sorted(trial.ops.items())),
                trial.gone,
            )

    def _mark_shells(self, trial: _Trial) -> None:
        """Watch opens in the trial's shell-snapshot dir once Claude Code has made it."""
        directory = TrialPaths(trial.trial_dir).agent_dir.joinpath(*SHELL_SNAPSHOTS)
        if self.mark_directory is None or not directory.is_dir():
            return
        try:
            self.mark_directory(directory, False)
        except OSError as error:
            logger.warning("%s: no mark on %s: %s", trial.name, directory, error)
        trial.shells_marked = directory

    def _open(self, trial_dir: Path) -> _Trial:
        identity = self.identify(trial_dir)
        container = self.containers.container_for(identity.compose_project)
        init_pid = self.containers.init_pid(container)
        base = self.proc / str(init_pid)
        stat = parse_stat((base / "stat").read_text())
        upper = overlay_upperdir((base / "mountinfo").read_bytes())
        if stat is None or upper is None:
            raise RuntimeError(f"container {container[:12]} has no overlay root to record")
        pidns = os.readlink(base / "ns" / "pid")
        path = writes_path(trial_dir)
        uid, gid = _owner(path.parent.parent)
        path.parent.mkdir(exist_ok=True)
        out = path.open("ab")
        for made in (path.parent, path):
            os.chown(made, uid, gid)
        return _Trial(
            trial_dir=trial_dir,
            name=identity.trial_name,
            container=container,
            init_pid=init_pid,
            init_start=stat.start_ticks,
            upper=upper.rstrip(b"/"),
            pidns=pidns,
            out=out,
            agent=os.fsencode(TrialPaths(trial_dir).agent_dir.resolve()),
        )

    def _match(self, path: bytes, *, agent: bool = False) -> _Trial | None:
        """The trial whose upper directory (or agent dir) holds `path`."""
        for trial in self._trials.values():
            root = trial.agent if agent else trial.upper
            if path.startswith(root) and path[len(root) : len(root) + 1] in (b"/", b""):
                return trial
        return None

    # Events -------------------------------------------------------------------------------

    def _pid_namespace(self, pid: int) -> str | None:
        now = time.monotonic()
        cached = self._pidns.get(pid)
        if cached is not None and now - cached[1] < PIDNS_FRESH_S:
            return cached[0]
        try:
            namespace: str | None = os.readlink(self.proc / str(pid) / "ns" / "pid")
        except OSError:
            namespace = None
        if len(self._pidns) > 200_000:
            self._pidns.clear()
        self._pidns[pid] = (namespace, now)
        return namespace

    def handle(self, events: Iterator[RawEvent], now: float, paths: DirectoryPaths) -> None:
        """Record a batch of events read at `now` (epoch seconds). Holds the trials' lock.

        An event that cannot be recorded is counted and logged, never raised: one odd process
        must not stop the recording of every trial.
        """
        with self._lock:
            for event in events:
                self.stats["seen"] += 1
                try:
                    self._handle_one(event, now, paths)
                except Exception:
                    self.stats["failed"] += 1
                    if self.stats["failed"] <= 3:
                        logger.exception("event of pid %d not recorded", event.pid)

    def _handle_one(self, event: RawEvent, now: float, paths: DirectoryPaths) -> None:
        if event.mask & FAN_Q_OVERFLOW:
            self.stats["overflow"] += 1
            logger.error("fanotify queue overflow: events were lost")
            return
        if event.pid == self._own_pid or not event.handle:
            return
        if self._pid_namespace(event.pid) == self._host_pidns:
            return
        directory = paths.resolve(event.handle)
        if directory is None:
            self.stats["unresolved"] += 1
            return
        path = directory + b"/" + event.name if event.name not in (b"", b".") else directory
        if event.mask & FAN_OPEN:
            self._shell_start(event, path, now)
            return
        if event.mask & FAN_ONDIR and event.mask & (FAN_MOVED_TO | FAN_DELETE):
            paths.forget()
        trial = self._match(path)
        if trial is None:
            if path.startswith(self._snapshots_prefix):
                reading = self.processes.reading(event.pid)
                self._held.append(_Held(now, event, path, reading))
                self.stats["held"] += 1
                self._wake.set()
            return
        self._record(trial, now, event, path, self.processes.reading(event.pid))

    def _shell_start(self, event: RawEvent, path: bytes, now: float) -> None:
        """A Bash call's shell sourcing its snapshot: one `open` line, with the shell."""
        trial = self._match(path, agent=True)
        if trial is None:
            return
        reading = self.processes.reading(event.pid)
        if reading.process is None or reading.process.own_shell is None:
            return  # Claude Code writing the snapshot, or another reader
        self._record(trial, now, event, path, reading, ops=((FAN_OPEN, "open"),))

    def _record(
        self,
        trial: _Trial,
        now: float,
        event: RawEvent,
        path: bytes,
        reading: _Reading,
        ops: tuple[tuple[int, WriteOp], ...] = OPS,
    ) -> None:
        if path.startswith(trial.agent):
            container_path = (AGENT_MOUNT + path[len(trial.agent) :]).decode(
                "utf-8", "backslashreplace"
            )
        else:
            container_path = (path[len(trial.upper) :] or b"/").decode("utf-8", "backslashreplace")
        at = datetime.fromtimestamp(now, UTC)
        second = int(now)
        for bit, op in ops:
            if not event.mask & bit:
                continue
            if op == "modify":
                key = (event.pid, path)
                if self._modified.get(key) == second:
                    continue
                self._modified[key] = second
            line = event_line(at, op, container_path, bool(event.mask & FAN_ONDIR), reading)
            data = line.model_dump_json().encode() + b"\n"
            trial.pending.append(data)
            trial.pending_bytes += len(data)
            trial.ops[op] += 1
            trial.gone += reading.gone
            self.stats["recorded"] += 1
        if trial.pending_bytes >= FLUSH_BYTES:
            self._flush(trial)

    def _release_held(self) -> None:
        """Record held events whose container is now known; drop those held too long."""
        now = time.time()
        kept: deque[_Held] = deque(maxlen=PENDING_MAX)
        for held in self._held:
            trial = self._match(held.path)
            if trial is not None:
                self._record(trial, held.at, held.event, held.path, held.reading)
            elif now - held.at < PENDING_S:
                kept.append(held)
        self._held = kept

    def _flush(self, trial: _Trial) -> None:
        if trial.pending:
            trial.out.write(b"".join(trial.pending))
            trial.out.flush()
            trial.pending.clear()
            trial.pending_bytes = 0

    def flush_all(self) -> None:
        with self._lock:
            for trial in self._trials.values():
                self._flush(trial)
            now = int(time.time())
            if len(self._modified) > 100_000:
                self._modified = {k: s for k, s in self._modified.items() if now - s < 2}

    # Running ------------------------------------------------------------------------------

    def _read_loop(self, group: Fanotify, stop: threading.Event) -> None:
        """Read until `stop`; if reading itself fails, stop the recorder rather than let it
        run on recording nothing."""
        try:
            self._read_until(group, stop)
        except BaseException:
            logger.exception("the fanotify reader failed; stopping the recorder")
            self.reader_failed = True
            stop.set()
            self._wake.set()
            raise

    def _read_until(self, group: Fanotify, stop: threading.Event) -> None:
        paths = DirectoryPaths(group)
        poller = select.poll()
        poller.register(group.fd, select.POLLIN)
        flushed = time.monotonic()
        while not stop.is_set():
            poller.poll(200)
            while data := group.read():
                self.handle(parse_events(data), time.time(), paths)
            if time.monotonic() - flushed >= FLUSH_S:
                self.flush_all()
                self.processes.forget_before(time.monotonic() - 600)
                flushed = time.monotonic()

    def run(self, stop: threading.Event) -> None:
        """Record until `stop` is set."""
        group = Fanotify()
        self.mark_directory = lambda path, remove: group.mark_directory(
            str(path), FAN_OPEN, remove=remove
        )
        try:
            group.mark_filesystem(str(self.snapshots), MASK)
            logger.info(
                "recording writes of trials under %s on the filesystem of %s",
                self.jobs_dir,
                self.snapshots,
            )
            reader = threading.Thread(
                target=self._read_loop, args=(group, stop), name="fanotify", daemon=True
            )
            reader.start()
            reported = time.monotonic()
            while not stop.is_set():
                self.discover()
                if time.monotonic() - reported >= STATS_S:
                    self._report(time.monotonic() - reported)
                    reported = time.monotonic()
                self._wake.wait(self.discover_interval)
                self._wake.clear()
            reader.join()
            if self.reader_failed:
                raise RuntimeError("the fanotify reader failed; see the log above")
        finally:
            with self._lock:
                for trial in self._trials.values():
                    self._flush(trial)
                    trial.out.close()
                self._trials.clear()
            group.close()

    def _report(self, seconds: float) -> None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        stats, self.stats = self.stats, Counter()
        logger.info(
            "%.0f s: %d events seen (%.0f/s), %d lines written, %d held, %d unresolved, "
            "%d overflows, %d not recorded; %d trials; cpu %.1f s total",
            seconds,
            stats["seen"],
            stats["seen"] / seconds,
            stats["recorded"],
            stats["held"],
            stats["unresolved"],
            stats["overflow"],
            stats["failed"],
            len(self._trials),
            usage.ru_utime + usage.ru_stime,
        )
