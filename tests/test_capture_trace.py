import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    FIXTURE_TOOL_CALL_ID,
    T0,
    assemble_job_dir,
    edit_trajectory,
    make_call,
    make_checkpoint,
    write_capture,
)
from trajlab.capture.trace import (
    OUTPUT_HEAD,
    OUTPUT_TAIL,
    build_trace,
    find_jobs,
    full_transcript,
    trial_dirs,
    trial_summary,
)
from trajlab.contracts import (
    CALL_INPUT_SUFFIX,
    CHANGES_FILENAME,
    PROCESSES_FILENAME,
    TRAJLAB_DIRNAME,
    CallChanges,
    CallRecord,
    FileChange,
    ListeningPort,
    ProcessEntry,
    ProcessSnapshot,
)

HARNESS = (
    ProcessEntry(pid=1, ppid=0, cmd="sleep infinity"),
    ProcessEntry(pid=40, ppid=1, cmd="node /usr/local/bin/claude --print"),
)
HARNESS_PORT = ListeningPort(proto="tcp", address="127.0.0.1", port=35765)
SERVER = "python3 -m http.server 8000"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def at(second: int) -> str:
    return f"2026-09-22T00:49:{second:02d}.000Z"


def agent_step(step_id: int, second: int, message: str = "", calls: tuple = ()) -> dict[str, Any]:
    """An ATIF agent step; each call is (id, tool, arguments, output, failed)."""
    step: dict[str, Any] = {
        "step_id": step_id,
        "timestamp": at(second),
        "source": "agent",
        "message": message,
    }
    if calls:
        step["tool_calls"] = [
            {"tool_call_id": cid, "function_name": tool, "arguments": args}
            for cid, tool, args, _, _ in calls
        ]
        step["observation"] = {
            "results": [
                {
                    "source_call_id": cid,
                    "content": out + ("\n[error] tool reported failure" if failed else ""),
                }
                for cid, _, _, out, failed in calls
            ]
        }
    return step


def write_extras(trial: Path, changes: list[CallChanges], snapshots: list[ProcessSnapshot]) -> None:
    extras = trial / TRAJLAB_DIRNAME
    extras.mkdir(exist_ok=True)
    (extras / CHANGES_FILENAME).write_text("".join(c.model_dump_json() + "\n" for c in changes))
    (extras / PROCESSES_FILENAME).write_text("".join(p.model_dump_json() + "\n" for p in snapshots))


def snapshot(call_id: str, seq: int, extra: tuple = (), ports: tuple = ()) -> ProcessSnapshot:
    return ProcessSnapshot(
        tool_call_id=call_id,
        call_seq=seq,
        processes=HARNESS + extra,
        listening=(HARNESS_PORT, *ports),
        capture_ms=70,
        captured_at=T0 + timedelta(seconds=seq),
    )


def five_call_trial(trial: Path) -> None:
    """A test run, an edit, the same test twice more (whitespace aside), and an edit that puts
    the file back; a server starts, restarts, and dies; then a success claim and the stop."""

    def mutate(data: dict[str, Any]) -> None:
        data["steps"] = [
            data["steps"][0],
            agent_step(
                2,
                20,
                "Running the tests.",
                (("c1", "Bash", {"command": "pytest -q"}, "Exit code 1\n1 failed", True),),
            ),
            agent_step(
                3,
                21,
                "",
                (
                    (
                        "c2",
                        "Write",
                        {"file_path": "/app/a.py", "content": "x = 2\n"},
                        "File written",
                        False,
                    ),
                ),
            ),
            agent_step(4, 22, "", (("c3", "Bash", {"command": "pytest   -q"}, "1 passed", False),)),
            agent_step(5, 23, "", (("c4", "Bash", {"command": "pytest -q"}, "1 passed", False),)),
            agent_step(
                6,
                24,
                "",
                (
                    (
                        "c5",
                        "Write",
                        {"file_path": "/app/a.py", "content": "x = 1\n"},
                        "File written",
                        False,
                    ),
                ),
            ),
            agent_step(
                7,
                25,
                "I fixed /app/a.py and parse.py, see localhost:8080/simple, e.g. Node.js; "
                "all tests pass.",
            ),
        ]

    edit_trajectory(trial, mutate)
    stamp = T0 + timedelta(seconds=30)
    calls = [
        make_call(1, "c1", tool_name="Bash", change="baseline"),
        make_call(2, "c2", change="changed", changed_paths=("~/app/a.py",), changed_paths_total=1),
        make_call(
            3, "c3", tool_name="Bash", outcome="unchanged", change="unchanged", checkpoint_seq=2
        ),
        make_call(
            4, "c4", tool_name="Bash", outcome="unchanged", change="unchanged", checkpoint_seq=2
        ),
        make_call(
            5,
            "c5",
            change="changed",
            changed_paths=("~/app/a.py",),
            changed_paths_total=1,
            checkpoint_seq=3,
        ),
        CallRecord(
            tool_call_id="stop_1_2",
            trial_name="hello-world__K3GBok3",
            tool_name="Stop",
            trigger="stop",
            call_seq=6,
            outcome="unchanged",
            change="unchanged",
            checkpoint_seq=3,
            requested_at=stamp,
            answered_at=stamp + timedelta(milliseconds=300),
        ),
    ]
    write_capture(
        trial,
        checkpoints=[make_checkpoint(1, "c1"), make_checkpoint(2, "c2"), make_checkpoint(3, "c5")],
        calls=calls,
    )

    def change(call_id: str, seq: int, before: str, after: str) -> CallChanges:
        diff = f"--- a/app/a.py\n+++ b/app/a.py\n@@ -1 +1 @@\n-{before.strip()}\n+{after.strip()}"
        file = FileChange(
            path="/app/a.py",
            op="modified",
            kind="file",
            size=len(after),
            sha256=sha(after),
            before_sha256=sha(before),
            is_text=True,
            diff=diff,
        )
        return CallChanges(
            tool_call_id=call_id, call_seq=seq, files=(file,), files_total=1, capture_ms=50
        )

    server = (ProcessEntry(pid=90, ppid=40, cmd=SERVER),)
    restarted = (ProcessEntry(pid=95, ppid=40, cmd=SERVER),)
    port = (ListeningPort(proto="tcp", address="0.0.0.0", port=8000),)
    write_extras(
        trial,
        [
            CallChanges(
                tool_call_id="c1", call_seq=1, files=(), files_total=0, baseline=True, capture_ms=40
            ),
            change("c2", 2, "x = 1\n", "x = 2\n"),
            CallChanges(tool_call_id="c3", call_seq=3, files=(), files_total=0, capture_ms=40),
            CallChanges(tool_call_id="c4", call_seq=4, files=(), files_total=0, capture_ms=40),
            change("c5", 5, "x = 2\n", "x = 1\n"),
        ],
        [
            snapshot("c1", 1),
            snapshot("c3", 3, server, port),
            snapshot("c4", 4, restarted, port),
            snapshot("c5", 5),
        ],
    )


def calls_by_id(trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in trace["events"] if e["kind"] in ("call", "stop")}


def test_trace_of_a_trial_without_capture(fixture_trial: Path) -> None:
    trace = build_trace(fixture_trial)

    assert [e["kind"] for e in trace["events"]] == ["message", "call", "message"]
    call = trace["events"][1]
    assert (call["n"], call["id"], call["tool"]) == (1, FIXTURE_TOOL_CALL_ID, "Write")
    assert call["status"] == "uncaptured" and call["hooked"]
    assert call["summary"] == "/app/hello.txt"
    assert call["output"].startswith("File created successfully")
    assert trace["capture"]["captured"] is False
    assert trace["trial"]["reward"] == 1.0
    assert trace["trial"]["instruction"].startswith("Create a file called hello.txt")
    assert trace["source"] == "trajectory"


def test_trace_joins_records_diffs_and_processes_by_tool_call_id(trial_copy: Path) -> None:
    five_call_trial(trial_copy)

    trace = build_trace(trial_copy)
    calls = calls_by_id(trace)

    assert [calls[c]["status"] for c in ("c1", "c2", "c3", "c4", "c5")] == [
        "baseline",
        "changed",
        "unchanged",
        "unchanged",
        "changed",
    ]
    assert [calls[c]["exit_code"] for c in ("c1", "c3")] == [1, 0]
    assert calls["c1"]["is_error"] and not calls["c3"]["is_error"]
    assert calls["c2"]["checkpoint"] == {"seq": 2, "capture_ms": 4000, "bytes": 1024}
    assert calls["c2"]["pause_ms"] == 600
    assert calls["c2"]["changes"]["files"][0]["diff"].endswith("-x = 1\n+x = 2")
    assert calls["c2"]["paths"] == []  # the diffs replace the record's path list
    # The server: started at c3, a new pid with the same command at c4, gone at c5.
    assert calls["c3"]["processes"]["started"] == [SERVER]
    assert calls["c3"]["processes"]["opened"] == [{"proto": "tcp", "port": 8000}]
    assert calls["c4"]["processes"]["started"] == calls["c4"]["processes"]["stopped"] == []
    assert calls["c5"]["processes"]["stopped"] == [SERVER]
    assert calls["c5"]["processes"]["closed"] == [{"proto": "tcp", "port": 8000}]
    assert trace["capture"]["harness_ports"] == [35765]
    assert all("claude" not in cmd for cmd in calls["c3"]["processes"]["running"])
    assert trace["capture"]["counts"] == {"baseline": 1, "changed": 2, "unchanged": 2}
    assert trace["capture"]["stops"] == 1
    # Which checkpoint holds the state after each call, and where the time went.
    assert [calls[c]["state_of"] for c in ("c1", "c2", "c3", "c4", "c5")] == [1, 2, 2, 2, 3]
    assert set(trace["checkpoint_images"]) == {"1", "2", "3"}
    assert all(calls[c]["tool_s"] >= 0 for c in ("c1", "c2", "c3", "c4", "c5"))
    assert trace["steps"]["3"]["model_s"] is not None
    assert trace["timing"]["pause_s"] > 0
    events = [v["event"] for v in trace["files"]["/app/a.py"]]
    assert events == sorted(events) and len(events) == 2


def test_the_stop_comes_after_the_final_message(trial_copy: Path) -> None:
    five_call_trial(trial_copy)

    events = build_trace(trial_copy)["events"]

    assert [e["kind"] for e in events[-2:]] == ["message", "stop"]
    stop = events[-1]
    assert (stop["id"], stop["status"], stop["hooked"]) == ("stop_1_2", "unchanged", True)
    assert "n" not in stop


def test_hints_mark_repeats_reverts_and_claims(trial_copy: Path) -> None:
    five_call_trial(trial_copy)

    trace = build_trace(trial_copy)
    calls = calls_by_id(trace)

    assert "repeat" not in calls["c1"]["hints"]
    assert calls["c3"]["hints"]["repeat"] == {"of": 1, "count": 2, "changed_between": True}
    assert calls["c4"]["hints"]["repeat"] == {"of": 3, "count": 3, "changed_between": False}
    assert calls["c5"]["hints"]["reverts"] == [{"path": "/app/a.py", "to": "original"}]
    assert [v["call"] for v in trace["files"]["/app/a.py"]] == [2, 5]
    claim = trace["events"][-2]
    assert claim["after_call"] == 5
    assert {m["token"]: m["calls"] for m in claim["hints"]["mentions"]} == {
        "/app/a.py": [2, 5],
        "parse.py": [],
    }
    assert claim["hints"]["claim"] == {
        "test_call": 4,
        "test_command": "pytest -q",
        "test_exit_code": 0,
        "changed_since": [5],
    }


def test_a_trial_without_a_trajectory_comes_from_the_hook_files(trial_copy: Path) -> None:
    (trial_copy / "agent" / "trajectory.json").unlink()
    directory = write_capture(
        trial_copy,
        calls=[make_call(1, "toolu_A", tool_name="Bash", change="baseline")],
        timeouts=["toolu_B"],
    )
    for call_id, command, event in (
        ("toolu_A", "ls /app", "PostToolUse"),
        ("toolu_B", "make", "PostToolUse"),
    ):
        hook_input = {
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"stdout": "hello.txt", "stderr": ""},
            "tool_use_id": call_id,
        }
        (directory / f"{call_id}{CALL_INPUT_SUFFIX}").write_text(json.dumps(hook_input))

    trace = build_trace(trial_copy)

    assert trace["source"] == "hook_files"
    assert [(e["n"], e["summary"], e["status"]) for e in trace["events"]] == [
        (1, "ls /app", "baseline"),
        (2, "make", "timeout"),
    ]
    assert trace["events"][0]["output"] == "hello.txt"
    assert trace["events"][0]["exit_code"] == 0


def test_without_extras_a_call_lists_the_records_paths(trial_copy: Path) -> None:
    write_capture(
        trial_copy,
        checkpoints=[make_checkpoint(1)],
        calls=[
            make_call(
                1, change="changed", changed_paths=("+/app/hello.txt",), changed_paths_total=1
            )
        ],
    )

    call = calls_by_id(build_trace(trial_copy))[FIXTURE_TOOL_CALL_ID]

    assert call["changes"] is None
    assert call["paths"] == [{"path": "/app/hello.txt", "op": "added"}]
    assert call["processes"] is None


def test_a_bad_record_line_is_skipped(trial_copy: Path) -> None:
    directory = write_capture(trial_copy, calls=[make_call(1)])
    with (directory / "calls.jsonl").open("a") as stream:
        stream.write('{"not": "a record"}\n')

    assert build_trace(trial_copy)["capture"]["hooked_calls"] == 1


def test_find_jobs_tells_a_jobs_dir_from_a_job_dir(job_dir: Path) -> None:
    # Job dirs have a config.json too; only trial dirs also have agent/.
    jobs_dir = job_dir.parent

    assert find_jobs(jobs_dir) == {job_dir.name: job_dir}
    assert find_jobs(job_dir) == {job_dir.name: job_dir}
    assert [t.name for t in trial_dirs(job_dir)] == ["hello-world__K3GBok3"]
    assert trial_dirs(jobs_dir) == []


def test_trial_summary_reads_only_small_files(tmp_path: Path) -> None:
    trial = assemble_job_dir(tmp_path) / "hello-world__K3GBok3"
    stop = make_call(
        3, "stop_9_9", tool_name="Stop", trigger="stop", outcome="unchanged", change="unchanged"
    )
    write_capture(
        trial,
        checkpoints=[make_checkpoint(1)],
        calls=[make_call(1), make_call(2, "c2", outcome="unchanged", change="unchanged"), stop],
        timeouts=["c3"],
    )

    summary = trial_summary(trial)

    assert summary["task"] == "hello-world/hello-world"
    assert (summary["finished"], summary["died"], summary["reward"]) == (True, False, 1.0)
    assert (
        summary["hooked_calls"],
        summary["stops"],
        summary["checkpoints"],
        summary["timeouts"],
    ) == (2, 1, 1, 1)
    assert summary["extras"] is False


def test_full_transcript_has_the_whole_run_in_order_with_nothing_cut(trial_copy: Path) -> None:
    long_output = "x" * (OUTPUT_HEAD + OUTPUT_TAIL + 500)

    def mutate(data: dict[str, Any]) -> None:
        data["steps"][1]["reasoning_content"] = "The file should hold exactly that text."
        result = data["steps"][1]["observation"]["results"][0]
        result["content"] = long_output
        result.pop("extra", None)  # no raw tool_result: the ATIF content is what was seen

    edit_trajectory(trial_copy, mutate)

    text = full_transcript(trial_copy, {FIXTURE_TOOL_CALL_ID: 7})

    order = [
        "Create a file called hello.txt",
        "[thinking]\nThe file should hold exactly that text.",
        "[message]\nI'll create the file now.",
        f"[tool call #7 Write · {FIXTURE_TOOL_CALL_ID}]",
        "file_path: /app/hello.txt",
        "[output of #7 Write]",
        long_output,
        "Created `/app/hello.txt`",
    ]
    positions = [text.index(piece) for piece in order]
    assert positions == sorted(positions)
    assert text.startswith("Trial:  hello-world-trial\nTask:   hello-world/hello-world")


def test_full_transcript_needs_a_trajectory(trial_copy: Path) -> None:
    (trial_copy / "agent" / "trajectory.json").unlink()

    with pytest.raises(FileNotFoundError):
        full_transcript(trial_copy)


def test_full_transcript_prints_multi_line_arguments_as_written(trial_copy: Path) -> None:
    script = "cat > /app/run.sh <<'X'\necho \"hi\"\nX"

    def mutate(data: dict[str, Any]) -> None:
        data["steps"][1]["tool_calls"][0]["arguments"] = {"command": script, "timeout": 60}

    edit_trajectory(trial_copy, mutate)

    text = full_transcript(trial_copy)

    assert f"----- command -----\n{script}\n----- end of command -----\ntimeout: 60" in text
