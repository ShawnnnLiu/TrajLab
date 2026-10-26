"""The host process that answers the query tool (ADR-0019): `trajlab store serve STORE JOB_DIR`.

Started before `harbor run` of a record-engine attempt, it watches the job dir for trial dirs
and, in each one's `agent/` (bind-mounted at `/logs/agent`, so this is the one channel to the
container), writes the agent's command `bin/query` (`query.sh`), makes `queries/` writable for
the container's user, and creates the query log. Then it answers each `queries/<id>.sql` the
command writes with `<id>.result` (the rows and a count line) or `<id>.err` (the message),
written whole and renamed into place so the command never reads a partial answer, and appends
one `QueryLogEntry` per request, refusals and errors included, to `agent/query_log.jsonl`.

Each query runs in its own child process through `trajlab.store.query.run_query`, one at a time per
server, and the server itself never opens the store: between queries nothing holds the file,
so a `store build` between attempts finds it free, and one during a query fails on DuckDB's
file lock instead of corrupting anything. The server is separate from the checkpoint watcher
(`trajlab watch`), so a slow query never delays a checkpoint's ack. Requests found unanswered
at startup are answered then; one with an answer is never answered again.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from trajlab.contracts import QUERIES_DIRNAME, QUERY_LOG_FILENAME, QUERY_TOOL_RELPATH, QueryLogEntry
from trajlab.store.query import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_ROW_CAP,
    DEFAULT_THREADS,
    DEFAULT_TIMEOUT_S,
    Outcome,
    render,
    run_query,
)

logger = logging.getLogger(__name__)

QUERY_SUFFIX = ".sql"
RESULT_SUFFIX = ".result"
ERROR_SUFFIX = ".err"
TMP_SUFFIX = ".tmp"
DEFAULT_POLL_S = 0.1
_QUERY_ID = re.compile(r"[A-Za-z0-9_-]+")


class ServeError(ValueError):
    """The server cannot start: the store does not answer, or the job dir is not a directory."""


def query_script() -> str:
    """The container-side command, as written into every served trial's `agent/bin/query`."""
    return files(__package__).joinpath("query.sh").read_text()


def queries_dir(agent_dir: Path) -> Path:
    return agent_dir / QUERIES_DIRNAME


def query_log_path(agent_dir: Path) -> Path:
    return agent_dir / QUERY_LOG_FILENAME


def setup_agent_dir(agent_dir: Path) -> None:
    """Put the query tool into a trial's `agent/` dir: the command, executable by everyone; the
    requests dir, writable by everyone (the container's user is not the host's); and the log,
    which the host alone appends to. Running it again changes nothing."""
    tool = agent_dir / QUERY_TOOL_RELPATH
    script = query_script()
    if not tool.is_file() or tool.read_text() != script:
        tool.parent.mkdir(parents=True, exist_ok=True)
        tmp = tool.with_name(tool.name + TMP_SUFFIX)
        tmp.write_text(script)
        tmp.chmod(0o755)
        os.replace(tmp, tool)
    requests = queries_dir(agent_dir)
    requests.mkdir(exist_ok=True)
    requests.chmod(0o777)
    query_log_path(agent_dir).touch()


def trial_dirs(job_dir: Path) -> list[Path]:
    """The trial dirs under a job dir: its subdirectories that have an `agent/` dir, which Harbor
    makes before it starts the trial's container."""
    found = []
    try:
        entries = sorted(os.scandir(job_dir), key=lambda entry: entry.name)
    except OSError as error:
        logger.warning("cannot list %s: %s", job_dir, error)
        return []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=True):
            continue
        if (Path(entry.path) / "agent").is_dir():
            found.append(Path(entry.path))
    return found


def pending_requests(agent_dir: Path) -> list[Path]:
    """The `<id>.sql` files in the trial's requests dir with no `.result` or `.err` yet, oldest
    first. A name that is not an id, or still being written (`.tmp`), is not a request."""
    requests = queries_dir(agent_dir)
    try:
        entries = list(os.scandir(requests))
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.name.endswith(QUERY_SUFFIX) or not entry.is_file():
            continue
        query_id = entry.name[: -len(QUERY_SUFFIX)]
        if not _QUERY_ID.fullmatch(query_id):
            logger.warning("ignoring %s: not a request id", entry.path)
            continue
        if any((requests / f"{query_id}{s}").exists() for s in (RESULT_SUFFIX, ERROR_SUFFIX)):
            continue
        found.append(Path(entry.path))
    found.sort(key=lambda path: (path.stat().st_mtime, path.name))
    return found


def _write_whole(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + TMP_SUFFIX)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Server:
    """Answers the query tool's requests from every trial under `job_dir` against one store."""

    store_dir: Path
    job_dir: Path
    cap: int = DEFAULT_ROW_CAP
    timeout_s: float = DEFAULT_TIMEOUT_S
    memory_limit: str | None = DEFAULT_MEMORY_LIMIT
    threads: int | None = DEFAULT_THREADS
    poll_s: float = DEFAULT_POLL_S
    known: set[Path] = field(default_factory=set, init=False)
    answered: int = field(default=0, init=False)

    def check(self) -> None:
        """Raise `ServeError` unless the store answers a query the way the agent's would run and
        the job dir exists. Done once, before serving, so a stale or missing store fails the
        attempt before it starts rather than query by query."""
        if not self.job_dir.is_dir():
            raise ServeError(f"{self.job_dir} is not a directory")
        outcome = self._run("SELECT count(*) FROM trials")
        if outcome.error is not None:
            raise ServeError(f"{self.store_dir} does not answer: {outcome.error}")

    def run(self, stop: threading.Event) -> None:
        """Serve until `stop` is set. Each pass takes new trial dirs in, then answers every
        pending request of every trial, oldest first."""
        self.check()
        logger.info("serving %s to trials under %s", self.store_dir, self.job_dir)
        while not stop.is_set():
            self.pass_once()
            stop.wait(self.poll_s)
        logger.info("stopped; %d queries answered", self.answered)

    def pass_once(self) -> None:
        for trial_dir in trial_dirs(self.job_dir):
            agent_dir = trial_dir / "agent"
            if trial_dir not in self.known:
                try:
                    setup_agent_dir(agent_dir)
                except OSError as error:
                    logger.warning("cannot set up %s: %s", agent_dir, error)
                    continue
                self.known.add(trial_dir)
                logger.info("serving %s", trial_dir.name)
            for request in pending_requests(agent_dir):
                self.answer(trial_dir.name, agent_dir, request)

    def answer(self, trial_name: str, agent_dir: Path, request: Path) -> QueryLogEntry:
        """Answer one request: run it, write `.result` or `.err`, append the log entry."""
        received_at = datetime.now(UTC)
        query_id = request.name[: -len(QUERY_SUFFIX)]
        start = time.monotonic()
        try:
            sql = request.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            outcome = Outcome(None, False, f"the host cannot read the request: {error}", 0)
            sql = ""
        else:
            outcome = self._run(sql)
        latency_ms = round((time.monotonic() - start) * 1000)
        if outcome.error is None:
            _write_whole(request.with_name(query_id + RESULT_SUFFIX), render(outcome, self.cap))
        else:
            _write_whole(request.with_name(query_id + ERROR_SUFFIX), outcome.error + "\n")
        entry = QueryLogEntry(
            query_id=query_id,
            trial_name=trial_name,
            received_at=received_at,
            sql=sql,
            rows=None if outcome.rows is None else len(outcome.rows),
            truncated=outcome.truncated,
            latency_ms=latency_ms,
            error=outcome.error,
        )
        with query_log_path(agent_dir).open("a", encoding="utf-8") as log:
            log.write(entry.model_dump_json() + "\n")
        self.answered += 1
        if entry.error is None:
            logger.info(
                "%s %s: %d rows%s in %d ms",
                trial_name,
                query_id,
                entry.rows,
                " (cut)" if entry.truncated else "",
                entry.latency_ms,
            )
        else:
            logger.info("%s %s: %s", trial_name, query_id, entry.error.splitlines()[0])
        return entry

    def _run(self, sql: str) -> Outcome:
        return run_query(
            self.store_dir,
            sql,
            cap=self.cap,
            timeout_s=self.timeout_s,
            memory_limit=self.memory_limit,
            threads=self.threads,
        )
