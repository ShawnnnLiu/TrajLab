"""The query tool's channel (ADR-0019): the container-side script run with plain `sh` against
`trajlab store serve` on a temp job dir, with no Docker. The acceptance check of Exp 2
build-order step 4 in a real container is `scripts/2026-10-26_query_tool_docker_check.py`."""

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_store_derive import call, make_store
from tests.test_store_query import FAST
from trajlab.contracts import QUERIES_DIRNAME, QUERY_LOG_FILENAME, QUERY_TOOL_RELPATH, QueryLogEntry
from trajlab.store.query import REFUSED
from trajlab.store.serve import (
    ERROR_SUFFIX,
    QUERY_SUFFIX,
    RESULT_SUFFIX,
    ServeError,
    Server,
    pending_requests,
    query_script,
    setup_agent_dir,
    trial_dirs,
)

TRIAL_NAME = "task__abc1234"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return make_store(tmp_path, [call(1, "ls"), call(2, "cat x"), call(3, "python x.py")])


@pytest.fixture
def job_dir(tmp_path: Path) -> Path:
    job = tmp_path / "jobs" / "job"
    (job / TRIAL_NAME / "agent").mkdir(parents=True)
    (job / "config.json").write_text("{}")  # Harbor's job-level files are not trial dirs
    (job / "inputs").mkdir()
    return job


@pytest.fixture
def server(store: Path, job_dir: Path) -> Iterator[Server]:
    """A server on the job dir, in a thread, stopped at the end."""
    server = Server(store, job_dir, poll_s=0.02, **FAST)
    stop = threading.Event()
    thread = threading.Thread(target=server.run, args=(stop,))
    thread.start()
    try:
        yield server
    finally:
        stop.set()
        thread.join(timeout=30)
        assert not thread.is_alive()


def run_tool(
    agent_dir: Path, *args: str, stdin: str = "", wait: str = "20"
) -> subprocess.CompletedProcess[str]:
    """Run the command as the agent would, with the container's environment."""
    env = {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_CONFIG_DIR": str(agent_dir / "sessions"),
        "TRAJLAB_QUERY_WAIT": wait,
    }
    return subprocess.run(
        ["/bin/sh", str(agent_dir / QUERY_TOOL_RELPATH), *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def wait_for(predicate: object, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)


def log_entries(agent_dir: Path) -> list[QueryLogEntry]:
    text = (agent_dir / QUERY_LOG_FILENAME).read_text()
    return [QueryLogEntry.model_validate_json(line) for line in text.splitlines()]


# -- setup ----------------------------------------------------------------------------------


def test_setup_puts_the_tool_the_channel_and_the_log_in_the_agent_dir(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    setup_agent_dir(agent_dir)
    tool = agent_dir / QUERY_TOOL_RELPATH
    assert tool.read_text() == query_script()
    assert tool.read_text().startswith("#!/bin/sh\n")
    assert tool.stat().st_mode & 0o777 == 0o755
    assert (agent_dir / QUERIES_DIRNAME).stat().st_mode & 0o777 == 0o777
    assert (agent_dir / QUERY_LOG_FILENAME).read_text() == ""
    (agent_dir / QUERY_LOG_FILENAME).write_text("kept\n")
    setup_agent_dir(agent_dir)  # again: nothing changes
    assert (agent_dir / QUERY_LOG_FILENAME).read_text() == "kept\n"
    assert not list(tool.parent.glob("*.tmp"))


def test_trial_dirs_are_the_subdirs_with_an_agent_dir(job_dir: Path) -> None:
    assert trial_dirs(job_dir) == [job_dir / TRIAL_NAME]
    (job_dir / ".hidden" / "agent").mkdir(parents=True)
    (job_dir / "later__x" / "agent").mkdir(parents=True)
    assert trial_dirs(job_dir) == [job_dir / "later__x", job_dir / TRIAL_NAME]
    assert trial_dirs(job_dir / "none") == []


def test_pending_requests_are_unanswered_ids_oldest_first(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    setup_agent_dir(agent_dir)
    requests = agent_dir / QUERIES_DIRNAME
    (requests / "q_2.sql").write_text("SELECT 2")
    (requests / "q_1.sql").write_text("SELECT 1")
    os.utime(requests / "q_1.sql", (1, 1))
    (requests / "q_0.sql").write_text("SELECT 0")
    (requests / "q_0.result").write_text("")
    (requests / "q_3.sql").write_text("SELECT 3")
    (requests / "q_3.err").write_text("")
    (requests / "q_4.sql.tmp").write_text("SELECT 4")
    (requests / "bad id.sql").write_text("SELECT 5")
    (requests / "notes.txt").write_text("")
    assert [p.name for p in pending_requests(agent_dir)] == ["q_1.sql", "q_2.sql"]
    assert pending_requests(tmp_path / "none") == []


# -- the script against the server ------------------------------------------------------------


def test_the_tool_answers_a_query_and_the_host_logs_it(server: Server, job_dir: Path) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    wait_for(lambda: (agent_dir / QUERY_TOOL_RELPATH).is_file())
    result = run_tool(agent_dir, "SELECT count(*) AS n FROM steps")
    assert (result.returncode, result.stdout, result.stderr) == (0, '{"n":3}\n-- 1 row\n', "")
    [request] = (agent_dir / QUERIES_DIRNAME).glob(f"*{QUERY_SUFFIX}")
    assert request.read_text() == "SELECT count(*) AS n FROM steps\n"
    assert request.with_name(request.stem + RESULT_SUFFIX).read_text() == result.stdout
    assert not list((agent_dir / QUERIES_DIRNAME).glob("*.tmp"))
    [entry] = log_entries(agent_dir)
    assert entry.query_id == request.stem
    assert entry.trial_name == TRIAL_NAME
    assert entry.sql == "SELECT count(*) AS n FROM steps\n"
    assert (entry.rows, entry.truncated, entry.error) == (1, False, None)
    assert 0 < entry.latency_ms < 10_000
    assert server.answered == 1


def test_the_tool_takes_the_query_from_stdin_and_as_words(server: Server, job_dir: Path) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    wait_for(lambda: (agent_dir / QUERY_TOOL_RELPATH).is_file())
    piped = run_tool(agent_dir, stdin="SELECT step_idx\nFROM steps\nORDER BY 1 DESC LIMIT 1")
    assert (piped.returncode, piped.stdout) == (0, '{"step_idx":3}\n-- 1 row\n')
    words = run_tool(agent_dir, "SELECT", "step_idx", "FROM", "steps", "LIMIT", "1")
    assert (words.returncode, words.stdout) == (0, '{"step_idx":1}\n-- 1 row\n')


def test_a_refused_statement_exits_1_with_the_reason(server: Server, job_dir: Path) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    wait_for(lambda: (agent_dir / QUERY_TOOL_RELPATH).is_file())
    result = run_tool(agent_dir, "DELETE FROM steps")
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == f"{REFUSED}only a SELECT or WITH query\n"
    [request] = (agent_dir / QUERIES_DIRNAME).glob(f"*{QUERY_SUFFIX}")
    assert request.with_name(request.stem + ERROR_SUFFIX).read_text() == result.stderr
    [entry] = log_entries(agent_dir)
    assert (entry.rows, entry.error) == (None, f"{REFUSED}only a SELECT or WITH query")
    failed = run_tool(agent_dir, "SELECT nope FROM steps")
    assert failed.returncode == 1 and "Binder Error" in failed.stderr
    assert len(log_entries(agent_dir)) == 2


def test_an_empty_query_is_refused_by_the_tool_itself(server: Server, job_dir: Path) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    wait_for(lambda: (agent_dir / QUERY_TOOL_RELPATH).is_file())
    result = run_tool(agent_dir, "   ")
    assert (result.returncode, result.stderr) == (1, 'usage: query "SELECT ..."\n')
    assert list((agent_dir / QUERIES_DIRNAME).glob("*")) == []


def test_the_tool_gives_up_without_a_host(job_dir: Path) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    setup_agent_dir(agent_dir)
    start = time.monotonic()
    result = run_tool(agent_dir, "SELECT 1", wait="1")
    assert (result.returncode, result.stderr) == (2, "query: no answer from the host within 1s\n")
    assert time.monotonic() - start < 10
    [request] = (agent_dir / QUERIES_DIRNAME).glob(f"*{QUERY_SUFFIX}")
    assert request.read_text() == "SELECT 1\n"


def test_the_tool_makes_the_channel_when_the_host_has_not(job_dir: Path) -> None:
    # The command runs before the server set the dir up: it creates the dir, writable for all.
    agent_dir = job_dir / TRIAL_NAME / "agent"
    (agent_dir / "bin").mkdir()
    (agent_dir / QUERY_TOOL_RELPATH).write_text(query_script())
    result = run_tool(agent_dir, "SELECT 1", wait="1")
    assert result.returncode == 2
    assert (agent_dir / QUERIES_DIRNAME).stat().st_mode & 0o777 == 0o777


def test_requests_left_unanswered_are_answered_at_startup(
    store: Path, job_dir: Path, tmp_path: Path
) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    setup_agent_dir(agent_dir)
    requests = agent_dir / QUERIES_DIRNAME
    (requests / "q_1.sql").write_text("SELECT 1 AS a")
    (requests / "q_0.sql").write_text("SELECT 0 AS a")
    (requests / "q_0.result").write_text("stale\n")
    server = Server(store, job_dir, poll_s=0.02, **FAST)
    server.pass_once()
    assert (requests / "q_1.result").read_text() == '{"a":1}\n-- 1 row\n'
    assert (requests / "q_0.result").read_text() == "stale\n"
    assert [entry.query_id for entry in log_entries(agent_dir)] == ["q_1"]
    server.pass_once()  # answered once
    assert [entry.query_id for entry in log_entries(agent_dir)] == ["q_1"]


def test_a_trial_dir_that_appears_later_is_served(server: Server, job_dir: Path) -> None:
    later = job_dir / "task__later01" / "agent"
    later.mkdir(parents=True)
    wait_for(lambda: (later / QUERY_TOOL_RELPATH).is_file())
    result = run_tool(later, "SELECT 2 AS b")
    assert (result.returncode, result.stdout) == (0, '{"b":2}\n-- 1 row\n')
    [entry] = log_entries(later)
    assert entry.trial_name == "task__later01"


def test_requests_are_answered_oldest_first_one_at_a_time(server: Server, job_dir: Path) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    wait_for(lambda: (agent_dir / QUERY_TOOL_RELPATH).is_file())
    threads = [
        threading.Thread(target=run_tool, args=(agent_dir, f"SELECT {i} AS i")) for i in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entries = log_entries(agent_dir)
    assert sorted(entry.sql for entry in entries) == [f"SELECT {i} AS i\n" for i in range(3)]
    assert all(entry.error is None for entry in entries)


def test_the_server_refuses_a_store_that_does_not_answer(job_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ServeError, match="does not exist"):
        Server(tmp_path / "none", job_dir, **FAST).run(threading.Event())


def test_the_server_refuses_a_missing_job_dir(store: Path, tmp_path: Path) -> None:
    with pytest.raises(ServeError, match="is not a directory"):
        Server(store, tmp_path / "none", **FAST).run(threading.Event())


def test_the_serve_command_runs_until_a_signal(store: Path, job_dir: Path) -> None:
    agent_dir = job_dir / TRIAL_NAME / "agent"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from trajlab.cli import app; app()",
            "store",
            "serve",
            str(store),
            str(job_dir),
            "--poll",
            "0.02",
            "--timeout",
            "30",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(lambda: (agent_dir / QUERY_TOOL_RELPATH).is_file(), seconds=30)
        result = run_tool(agent_dir, "SELECT count(*) AS n FROM steps")
        assert (result.returncode, result.stdout) == (0, '{"n":3}\n-- 1 row\n')
    finally:
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    assert "stopped after 1 queries" in stderr
