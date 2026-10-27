"""The `writes` table (ADR-0021): recorded write events tied to steps and joined to delta rows,
on hand-made events and rows."""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from tests.test_store_derive import START, TRIAL, at, call, change, sha
from trajlab.contracts import ProcessRef, StepRow, ToolShell, WriteEvent, WriteRow
from trajlab.store.writes import (
    Writer,
    Writers,
    command_hashes,
    read_write_events,
    write_rows,
    writes_path,
)

INIT = ProcessRef(pid=1000, cpid=1, started_at=START - timedelta(hours=1), comm="sleep")
CLAUDE = ProcessRef(pid=1100, cpid=7, started_at=START - timedelta(minutes=5), comm="claude")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def shell(command: str, pid: int, started: datetime) -> ToolShell:
    return ToolShell(
        pid=pid,
        cpid=pid - 1000,
        started_at=started,
        command_sha256=digest(command),
        command=command,
    )


def write(
    path: str,
    when: datetime,
    *,
    pid: int = 2000,
    op: str = "modify",
    sh: ToolShell | None = None,
    sid: int | None = None,
    started: datetime | None = None,
    ancestry: tuple[ProcessRef, ...] = (),
    agent: bool = False,
    is_dir: bool = False,
) -> WriteEvent:
    return WriteEvent(
        at=when,
        op=op,  # type: ignore[arg-type]
        path=path,
        is_dir=is_dir,
        pid=pid,
        cpid=pid - 1000,
        comm="python",
        started_at=started,
        sid=sid,
        ancestry=ancestry,
        agent=agent,
        shell=sh,
    )


def checkpointed(step: StepRow, seq: int, seconds: float = 4) -> StepRow:
    assert step.t_end is not None
    return step.model_copy(
        update={
            "checkpoint_seq": seq,
            "checkpoint_id": sha(seq),
            "checkpoint_at": step.t_end + timedelta(seconds=seconds),
        }
    )


# -- which step a process belongs to -----------------------------------------------------------


def test_claude_codes_nul_rewrite_is_matched_too() -> None:
    assert command_hashes("python x.py > nul") == {
        digest("python x.py > nul"),
        digest("python x.py > /dev/null"),
    }
    assert command_hashes("echo $HOME > NUL") == {digest("echo $HOME > NUL")}


def test_a_process_under_a_tool_shell_belongs_to_the_call_with_that_command() -> None:
    launch = "nohup python c.py > log.txt &"
    steps = [call(1, launch), call(2, "sleep 115; tail log.txt", seconds=116)]
    event = write("/app/log.txt", at(2, 30), sh=shell(launch, 1200, at(1, 0.2)))
    assert Writers(steps, [event]).writer(event) == Writer(1, "command")


def test_the_same_command_twice_is_told_apart_by_the_shells_start() -> None:
    steps = [call(1, "make", seconds=20), call(2, "ls"), call(3, "make", seconds=20)]
    events = [
        write("/app/a.o", at(1, 5), sh=shell("make", 1200, at(1, 0.1))),
        write("/app/a.o", at(3, 5), sh=shell("make", 1300, at(3, 0.1))),
    ]
    writers = Writers(steps, events)
    assert [writers.writer(e) for e in events] == [Writer(1, "command"), Writer(3, "command")]


def test_a_child_that_outlived_its_shell_is_found_by_its_session() -> None:
    launch = "for s in 11 12; do nohup python c.py $s > log_$s.txt & done"
    steps = [call(1, launch), call(2, "sleep 115; tail -qn1 log_1[1-2].txt", seconds=116)]
    seen = write("/tmp/claude-a1-cwd", at(1, 0.5), pid=1200, sh=shell(launch, 1200, at(1, 0.2)))
    orphan = write("/tmp/w/log_11.txt", at(2, 110), sid=1200, started=at(1, 0.3), ancestry=(INIT,))
    assert Writers(steps, [seen, orphan]).writer(orphan) == Writer(1, "session")


def test_a_setsid_child_is_found_by_its_start_time() -> None:
    steps = [call(1, "setsid python serve.py &", seconds=2), call(2, "curl localhost:8000")]
    daemon = write(
        "/app/server.log", at(2, 0.5), pid=2500, sid=2500, started=at(1, 1), ancestry=(INIT,)
    )
    assert Writers(steps, [daemon]).writer(daemon) == Writer(1, "start_time")
    # Its oldest ancestor below the container's init starts the window, not itself.
    worker = write(
        "/app/w.log",
        at(2, 0.5),
        pid=2600,
        sid=2500,
        started=at(1, 40),
        ancestry=(ProcessRef(pid=2500, cpid=15, started_at=at(1, 1), comm="python"), INIT),
    )
    assert Writers(steps, [worker]).writer(worker) == Writer(1, "start_time")


def test_claude_codes_own_write_belongs_to_the_write_tool_call_running() -> None:
    steps = [call(1, "ls"), call(2, "ignored", tool="Write", seconds=2)]
    own = write("/app/plan.json", at(2, 1), pid=1100, agent=True, ancestry=())
    between = write("/tmp/claude-0/x", at(3, 30), pid=1100, agent=True)
    writers = Writers(steps, [own, between])
    assert writers.writer(own) == Writer(2, "tool_window")
    assert writers.writer(between) is None


def test_a_shell_whose_command_matches_no_call_falls_back_to_its_start_time() -> None:
    steps = [call(1, "python train.py", seconds=30)]
    event = write("/app/m.pt", at(1, 20), sh=shell("something else", 1200, at(1, 0.1)))
    assert Writers(steps, [event]).writer(event) == Writer(1, "start_time")


def test_a_process_known_by_nothing_has_no_call() -> None:
    steps = [call(1, "ls")]
    gone = WriteEvent(at=at(5), op="close_write", path="/app/x", pid=4321, gone=True)
    assert Writers(steps, [gone]).writer(gone) is None


# -- rows per delta row ------------------------------------------------------------------------


def test_events_join_the_delta_row_of_their_checkpoint_interval() -> None:
    launch = "nohup python c.py > log.txt &"
    steps = [
        checkpointed(call(1, launch), 1),
        checkpointed(call(2, "sleep 60; tail log.txt", seconds=61), 2),
        call(3, "cat log.txt"),  # no checkpoint
    ]
    sh = shell(launch, 1200, at(1, 0.2))
    events = [
        write("/app/log.txt", at(1, 0.5), op="create", sh=sh),  # before checkpoint 1
        write("/app/log.txt", at(2, 10), sh=sh),
        write("/app/log.txt", at(2, 40), op="close_write", sh=sh),
        write("/app/log.txt", at(2, 50), pid=1100, agent=True),  # Claude Code, no call
        write("/app/log.txt", at(2, 70), sh=sh),  # after checkpoint 2
        write("/app/other", at(2, 10), sh=sh),  # no delta row
    ]
    deltas = [change(2, "/app/log.txt")]
    assert write_rows(TRIAL, steps, deltas, events) == (
        WriteRow(
            trial_id=TRIAL,
            step_idx=2,
            path="/app/log.txt",
            writer_step=1,
            mapped_by="command",
            events=2,
            ops=("close_write", "modify"),
            first_at=at(2, 10),
            last_at=at(2, 40),
        ),
        WriteRow(
            trial_id=TRIAL,
            step_idx=2,
            path="/app/log.txt",
            events=1,
            ops=("modify",),
            first_at=at(2, 50),
            last_at=at(2, 50),
        ),
    )
    first = write_rows(TRIAL, steps, [change(1, "/app/log.txt", before=None)], events)
    assert [(r.step_idx, r.events, r.ops) for r in first] == [(1, 1, ("create",))]


def test_a_group_says_its_surest_mapping() -> None:
    launch = "for s in 1 2; do nohup python c.py $s > log_$s.txt & done"
    steps = [checkpointed(call(1, launch), 1), checkpointed(call(2, "sleep 60", seconds=61), 2)]
    sh = shell(launch, 1200, at(1, 0.2))
    events = [
        write("/app/log_1.txt", at(2, 5), sid=1200, started=at(1, 0.3), ancestry=(INIT,)),
        write("/app/log_1.txt", at(2, 6), sh=sh),
    ]
    (row,) = write_rows(TRIAL, steps, [change(2, "/app/log_1.txt")], events)
    assert (row.writer_step, row.mapped_by, row.events) == (1, "command", 2)


def test_a_directory_renamed_into_place_stands_for_what_it_holds() -> None:
    install = "pip install --target /tmp/nxref networkx"
    steps = [checkpointed(call(1, install, seconds=3), 1)]
    sh = shell(install, 1200, at(1, 0.1))
    events = [
        write("/tmp/pip-target-x/networkx/a.py", at(1, 1), op="close_write", sh=sh),
        write("/tmp/nxref", at(1, 0.5), op="create", sh=sh, is_dir=True),
        write("/tmp/nxref/networkx", at(1, 2), op="moved_to", sh=sh, is_dir=True),
    ]
    deltas = [
        change(1, "/tmp/nxref/networkx/utils/a.py", before=None),
        change(1, "/tmp/nxref/networkx", before=None),
    ]
    rows = write_rows(TRIAL, steps, deltas, events)
    assert [(r.path, r.writer_step, r.ops, r.via_dir) for r in rows] == [
        # The nearest directory renamed into place, not its parent's create.
        ("/tmp/nxref/networkx/utils/a.py", 1, ("moved_to",), "/tmp/nxref/networkx"),
        # The directory's own event is its own.
        ("/tmp/nxref/networkx", 1, ("moved_to",), None),
    ]


def test_a_removed_directory_stands_for_the_children_it_took() -> None:
    steps = [
        checkpointed(call(1, "python render.py", seconds=3), 1),
        checkpointed(call(2, "sleep 60", seconds=61), 2),
    ]
    sh = shell("python render.py", 1200, at(1, 0.1))
    events = [
        write("/tmp/profile-a", at(2, 5), op="delete", sh=sh, is_dir=True),
        write("/tmp/profile-a/Default/kept", at(2, 4), op="delete", sh=sh),
        write("/tmp/profile-b", at(2, 5), op="moved_to", sh=sh, is_dir=True),
        write("/tmp/profile-c", at(1, 2), op="delete", sh=sh, is_dir=True),  # not this interval
    ]
    deltas = [
        change(2, "/tmp/profile-a/Default/lost", after=None),
        change(2, "/tmp/profile-a/Default/kept", after=None),
        change(2, "/tmp/profile-b/x", after=None),  # a rename is no delete
        change(2, "/tmp/profile-c/x", after=None),
        change(2, "/tmp/profile-a/Default/new", before=None),  # a delete brings nothing
    ]
    rows = write_rows(TRIAL, steps, deltas, events)
    assert [(r.path, r.writer_step, r.via_dir) for r in rows] == [
        ("/tmp/profile-a/Default/lost", 1, "/tmp/profile-a"),
        ("/tmp/profile-a/Default/kept", 1, None),
    ]


def test_no_record_no_rows(tmp_path: Path) -> None:
    steps = [checkpointed(call(1, "touch /app/x"), 1)]
    assert write_rows(TRIAL, steps, [change(1, "/app/x")], []) == ()
    assert read_write_events(tmp_path) == []


def test_the_recorders_file_is_read_and_a_cut_line_skipped(tmp_path: Path) -> None:
    event = write("/app/x", at(1, 1))
    path = writes_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(event.model_dump_json() + "\n" + json.dumps({"at": "cut"})[:9])
    assert read_write_events(tmp_path) == [event]
