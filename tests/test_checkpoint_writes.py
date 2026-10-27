"""The write recorder (ADR-0021) offline: /proc parsing, the Bash tool's shell, process readings
against a fake /proc, and the recorder's handling of events, with no fanotify and no Docker."""

import json
import os
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trajlab.checkpoint.fanotify import (
    FAN_CLOSE_WRITE,
    FAN_CREATE,
    FAN_DELETE,
    FAN_MODIFY,
    FAN_MOVED_TO,
    FAN_ONDIR,
    FAN_Q_OVERFLOW,
    RawEvent,
)
from trajlab.checkpoint.watcher import TrialIdentity
from trajlab.checkpoint.writes import (
    CLOCK_TICKS,
    Processes,
    Recorder,
    RecorderLockedError,
    hold_recorder_lock,
    is_claude_code,
    overlay_upperdir,
    parse_nspid,
    parse_stat,
    recorder_running,
    shell_word,
    tool_command,
    writes_path,
)
from trajlab.contracts import WriteEvent

HOST_NS = "pid:[4026531836]"
CONTAINER_NS = "pid:[4026532999]"
SNAPSHOTS = "/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs"
UPPER = f"{SNAPSHOTS}/snapshots/3115/fs"


def quote(command: str) -> str:
    """Claude Code 2.1.278's shell quoting of a command (its `Jr` and `Nxr`)."""
    if re.fullmatch(r"[A-Za-z0-9_./:=@+,-]+", command):
        return command
    return "'" + command.replace("'", "'\"'\"'") + "'"


def tool_argv(command: str, *, stdin: bool = True, login: bool = False) -> list[str]:
    """The argv of the shell Claude Code 2.1.278 runs a Bash call in (`buildExecCommand`)."""
    script = " && ".join(
        [
            "source /logs/agent/sessions/shell-snapshots/snapshot-bash-17-x.sh 2>/dev/null || true",
            "shopt -u extglob 2>/dev/null || true",
            "{ \\builtin unalias -- 'unsetenv'; \\builtin unset -f -- 'unsetenv'; } "
            ">/dev/null 2>&1 || true",
            f"eval {quote(command)}{' < /dev/null' if stdin else ''}",
            "pwd -P >| /tmp/claude-a1b2-cwd",
        ]
    )
    return ["/bin/bash", "-c", *(["-l"] if login else []), script]


# -- /proc parsing -----------------------------------------------------------------------------


def stat_line(pid: int, comm: str, ppid: int, sid: int, start: int, state: str = "S") -> str:
    rest = [state, ppid, pid, sid, 0, -1, 4194560, *([0] * 12), start, 1000, 100]
    return f"{pid} ({comm}) " + " ".join(str(v) for v in rest) + "\n"


def test_stat_fields_survive_a_name_with_spaces_and_parentheses() -> None:
    parsed = parse_stat(stat_line(77, "a (b) c", 70, 60, 123456))
    assert parsed is not None
    assert (parsed.comm, parsed.ppid, parsed.sid, parsed.start_ticks) == ("a (b) c", 70, 60, 123456)
    assert parse_stat("77 (cut") is None


def test_nspid_lists_the_pid_in_each_namespace() -> None:
    assert parse_nspid("Name:\tsh\nNSpid:\t2046560\t14\nNSsid:\t1\n") == (2046560, 14)
    assert parse_nspid("Name:\tsh\n") == ()


def test_the_overlay_upper_directory_of_the_root_mount() -> None:
    mountinfo = (
        b"1 0 0:50 / / rw,relatime - overlay overlay rw,lowerdir=/a:/b,"
        b"upperdir=" + UPPER.encode() + b"\\040x,workdir=/w\n"
        b"2 1 0:51 / /proc rw - proc proc rw\n"
    )
    assert overlay_upperdir(mountinfo) == UPPER.encode() + b" x"
    assert overlay_upperdir(b"2 1 0:51 / / rw - ext4 /dev/root rw\n") is None


# -- the Bash tool's shell ---------------------------------------------------------------------

COMMANDS = [
    "ls",
    "echo 'hi' > /tmp/x.txt",
    "cd /tmp/w && for s in 11 12; do nohup python c.py $s > log_$s.txt 2>&1 & done",
    'python -c "print(\\"a\\")" | tail -3',
    "cat > f.py <<'EOF'\nimport os\nprint('it''s')\nEOF\npython f.py",
    "grep -n 'a && eval b' x && echo \"$HOME\" `pwd`",
    "printf 'é\\n' > /app/ünïcode.txt",
]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("stdin", [True, False])
@pytest.mark.parametrize("login", [False, True])
def test_the_command_is_read_back_from_the_tool_shell(
    command: str, stdin: bool, login: bool
) -> None:
    assert tool_command(tool_argv(command, stdin=stdin, login=login)) == command


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/bash", "-c", "make && make test"],
        ["python3", "train.py"],
        ["/bin/bash", "-c", "eval 'ls' && pwd -P"],
        ["/bin/bash", "-c", "eval 'unclosed && pwd -P >| /tmp/claude-1-cwd"],
        ["/bin/bash"],
    ],
)
def test_other_processes_are_no_tool_shell(argv: list[str]) -> None:
    assert tool_command(argv) is None


def test_a_shell_word_applies_quotes_and_escapes() -> None:
    text = """a'b c'"d \\"e\\" \\$f"g\\ h rest"""
    assert shell_word(text, 0) == ('ab cd "e" $fg h', len(text) - len(" rest"))
    assert shell_word("'open", 0) is None


def test_claude_codes_own_process() -> None:
    assert is_claude_code(["claude", "--verbose", "--output-format=stream-json"], "claude")
    assert is_claude_code(["/root/.local/bin/claude", "--print"], "MainThread")
    assert not is_claude_code(["python3", "claude.py"], "python3")


# -- process readings against a fake /proc -----------------------------------------------------


class FakeProc:
    """A /proc tree under a temporary dir: what `Processes` and `Recorder` read."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "self" / "ns").mkdir(parents=True)
        os.symlink(HOST_NS, root / "self" / "ns" / "pid")

    def add(
        self,
        pid: int,
        argv: Sequence[str],
        *,
        ppid: int,
        sid: int,
        start: int,
        cpid: int | None,
        comm: str | None = None,
        mountinfo: bytes | None = None,
    ) -> None:
        base = self.root / str(pid)
        (base / "ns").mkdir(parents=True, exist_ok=True)
        name = comm or os.path.basename(argv[0])[:15]
        (base / "stat").write_text(stat_line(pid, name, ppid, sid, start))
        nspid = f"{pid}\t{cpid}" if cpid is not None else f"{pid}"
        (base / "status").write_text(f"Name:\t{name}\nNSpid:\t{nspid}\n")
        (base / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
        link = base / "ns" / "pid"
        if link.is_symlink():
            link.unlink()
        os.symlink(CONTAINER_NS if cpid is not None else HOST_NS, link)
        if mountinfo is not None:
            (base / "mountinfo").write_bytes(mountinfo)

    def remove(self, pid: int) -> None:
        for path in sorted((self.root / str(pid)).rglob("*"), reverse=True):
            path.unlink() if path.is_file() or path.is_symlink() else path.rmdir()
        (self.root / str(pid)).rmdir()


SHIM, INIT, CLAUDE, SHELL, CHILD = 900, 1000, 1100, 1200, 1300
INIT_MOUNTINFO = (
    b"1 0 0:50 / / rw - overlay overlay rw,upperdir=" + UPPER.encode() + b",workdir=/w\n"
)


def container(proc: FakeProc, command: str = "nohup python c.py > log.txt &") -> None:
    """The shim (a host process), the container's init, Claude Code, a Bash call's shell, and
    a child of that shell."""
    proc.add(SHIM, ["containerd-shim-runc-v2"], ppid=1, sid=1, start=10, cpid=None)
    proc.add(
        INIT, ["sleep", "infinity"], ppid=SHIM, sid=INIT, start=20, cpid=1, mountinfo=INIT_MOUNTINFO
    )
    proc.add(CLAUDE, ["claude", "--verbose", "--print"], ppid=SHIM, sid=CLAUDE, start=30, cpid=7)
    proc.add(SHELL, tool_argv(command), ppid=CLAUDE, sid=SHELL, start=40, cpid=12, comm="bash")
    proc.add(CHILD, ["python", "c.py"], ppid=SHELL, sid=SHELL, start=41, cpid=13)


def test_a_writer_under_a_tool_shell(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path)
    container(proc)
    reading = Processes(tmp_path).reading(CHILD)
    assert not reading.gone
    assert [p.pid for p in reading.ancestry] == [SHELL, CLAUDE]  # the shim is outside
    assert reading.agent  # Claude Code is above it
    assert reading.shell is not None
    assert (reading.shell.pid, reading.shell.cpid) == (SHELL, 12)
    assert reading.shell.command == "nohup python c.py > log.txt &"
    assert reading.process is not None and reading.process.cpid == 13


def test_the_outermost_tool_shell_names_the_call(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path)
    container(proc)
    # `( ... &)` forks the shell: the subshell has the same command line until it execs.
    proc.add(
        1400, tool_argv("nohup python c.py > log.txt &"), ppid=SHELL, sid=SHELL, start=41, cpid=14
    )
    proc.add(1500, ["python"], ppid=1400, sid=SHELL, start=42, cpid=15)
    reading = Processes(tmp_path).reading(1500)
    assert reading.shell is not None and reading.shell.pid == SHELL


def test_a_child_that_outlives_its_shell_keeps_the_shell_seen_above_it(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path)
    container(proc)
    processes = Processes(tmp_path)
    first = processes.reading(CHILD)
    proc.remove(SHELL)
    proc.add(CHILD, ["python", "c.py"], ppid=INIT, sid=SHELL, start=41, cpid=13)
    processes._cache.clear()  # past the freshness window
    later = processes.reading(CHILD)
    assert [p.pid for p in later.ancestry] == [INIT]
    assert later.shell == first.shell
    assert not later.agent


def test_a_gone_writer_is_marked_and_keeps_what_was_read_before(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path)
    container(proc)
    processes = Processes(tmp_path)
    seen = processes.reading(CHILD)
    proc.remove(CHILD)
    gone = processes.reading(CHILD)
    assert gone.gone and gone.process == seen.process and gone.shell == seen.shell
    never = processes.reading(4321)
    assert never.gone and never.process is None and never.shell is None


def test_start_times_are_wall_clock_times(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path)
    container(proc)
    reading = Processes(tmp_path).reading(CHILD)
    assert reading.process is not None
    boot = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    assert abs(reading.process.started_at.timestamp() - (boot + 41 / CLOCK_TICKS)) < 1


# -- the recorder ------------------------------------------------------------------------------


class Containers:
    def __init__(self) -> None:
        self.started = False

    def container_for(self, compose_project: str) -> str:
        if not self.started:
            raise RuntimeError("expected one running main container, found 0")
        return "c0ffee" * 10

    def init_pid(self, container: str) -> int:
        return INIT


class Paths:
    """Directory handles that are their own paths."""

    def __init__(self) -> None:
        self.forgotten = 0

    def resolve(self, handle: bytes) -> bytes | None:
        return handle or None

    def forget(self) -> None:
        self.forgotten += 1


def raw(mask: int, pid: int, directory: str, name: str | bytes) -> RawEvent:
    encoded = name.encode() if isinstance(name, str) else name
    return RawEvent(mask=mask, pid=pid, fsid=b"f", handle=directory.encode(), name=encoded)


@pytest.fixture
def jobs(tmp_path: Path) -> Path:
    trial = tmp_path / "jobs" / "job" / "task__abc"
    (trial / "agent").mkdir(parents=True)
    (trial / "config.json").write_text("{}")
    return tmp_path / "jobs"


def recorder(jobs: Path, proc_root: Path, containers: Containers) -> Recorder:
    return Recorder(
        jobs,
        lambda _: TrialIdentity("task__abc", "task__abc__env"),
        containers,
        snapshots=Path(SNAPSHOTS),
        proc=proc_root,
    )


def lines(jobs: Path) -> list[WriteEvent]:
    path = writes_path(jobs / "job" / "task__abc")
    return [WriteEvent.model_validate_json(line) for line in path.read_bytes().splitlines()]


def test_the_recorder_writes_one_line_per_operation_of_a_container_write(
    jobs: Path, tmp_path: Path
) -> None:
    proc = FakeProc(tmp_path / "proc")
    container(proc)
    containers = Containers()
    containers.started = True
    rec = recorder(jobs, tmp_path / "proc", containers)
    rec.discover()
    paths = Paths()
    now = datetime(2026, 10, 27, 5, 0, 1, 250000, tzinfo=UTC).timestamp()
    rec.handle(
        iter(
            [
                raw(FAN_CREATE | FAN_MODIFY | FAN_CLOSE_WRITE, CHILD, f"{UPPER}/tmp/w", "log.txt"),
                raw(FAN_MODIFY, CHILD, f"{UPPER}/tmp/w", "log.txt"),  # same second: dropped
                raw(FAN_MODIFY, SHIM, f"{UPPER}/tmp/w", "host.txt"),  # a host process
                raw(FAN_MODIFY, os.getpid(), f"{UPPER}/tmp/w", "self.txt"),  # the recorder
                raw(FAN_MOVED_TO | FAN_ONDIR, CHILD, UPPER, "tmp"),  # a copy-up of /tmp
                raw(FAN_DELETE, SHELL, f"{UPPER}/app", b"gone\xff.txt"),  # not UTF-8
                raw(FAN_MODIFY, CHILD, "/var/lib/docker/elsewhere", "x"),  # not a container's
                raw(FAN_Q_OVERFLOW, 0, "", ""),
            ]
        ),
        now,
        paths,  # type: ignore[arg-type]
    )
    rec.handle(iter([raw(FAN_MODIFY, CHILD, f"{UPPER}/tmp/w", "log.txt")]), now + 1, paths)  # type: ignore[arg-type]
    rec.flush_all()
    found = lines(jobs)
    assert [(e.op, e.path, e.pid, e.is_dir) for e in found] == [
        ("create", "/tmp/w/log.txt", CHILD, False),
        ("modify", "/tmp/w/log.txt", CHILD, False),
        ("close_write", "/tmp/w/log.txt", CHILD, False),
        ("moved_to", "/tmp", CHILD, True),
        ("delete", "/app/gone\\xff.txt", SHELL, False),
        ("modify", "/tmp/w/log.txt", CHILD, False),
    ]
    first = found[0]
    assert first.at == datetime.fromtimestamp(now, UTC)
    assert (first.cpid, first.sid, first.comm, first.cmd) == (13, SHELL, "python", "python c.py")
    assert [p.pid for p in first.ancestry] == [SHELL, CLAUDE]
    assert first.shell is not None and first.shell.command == "nohup python c.py > log.txt &"
    assert first.agent and not first.gone
    assert paths.forgotten == 1  # a directory moved: cached paths may be stale
    assert rec.stats["overflow"] == 1


def test_events_before_the_trial_is_known_wait_for_discovery(jobs: Path, tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    container(proc)
    containers = Containers()
    rec = recorder(jobs, tmp_path / "proc", containers)
    rec.discover()  # no container yet
    assert not writes_path(jobs / "job" / "task__abc").exists()
    now = datetime.now(UTC).timestamp()
    rec.handle(iter([raw(FAN_CREATE, CHILD, f"{UPPER}/app", "early.txt")]), now, Paths())  # type: ignore[arg-type]
    proc.remove(CHILD)  # gone before discovery: it was read at the event
    containers.started = True
    rec.discover()
    rec.flush_all()
    (early,) = lines(jobs)
    assert (early.path, early.cpid, early.gone) == ("/app/early.txt", 13, False)


def test_a_trial_that_ended_is_let_go(jobs: Path, tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    container(proc)
    containers = Containers()
    containers.started = True
    rec = recorder(jobs, tmp_path / "proc", containers)
    rec.discover()
    assert rec._trials
    (jobs / "job" / "task__abc" / "result.json").write_text("{}")
    rec.discover()
    assert not rec._trials
    assert writes_path(jobs / "job" / "task__abc").stat().st_uid == os.getuid()


def test_one_recorder_per_jobs_dir(tmp_path: Path) -> None:
    assert not recorder_running(tmp_path)
    with hold_recorder_lock(tmp_path):
        assert recorder_running(tmp_path)
        with pytest.raises(RecorderLockedError), hold_recorder_lock(tmp_path):
            pass
    assert not recorder_running(tmp_path)
    assert json.loads((tmp_path / ".trajlab-recorder.lock").read_text()) == os.getpid()
