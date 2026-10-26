"""The host process that answers the query tool (ADR-0019): `trajlab store serve STORE JOB_DIR`.

Started before `harbor run` of a record-engine attempt, it watches the job dir for trial dirs
and, in each one's `agent/` (bind-mounted at `/logs/agent`, so this is the one channel to the
container), writes the agent's command `bin/query` (`query.sh`), makes `queries/` writable for
the container's user, and creates the query log. Then it answers each `queries/<id>.sql` the
command writes with `<id>.result` (the rows and a count line) or `<id>.err` (the message),
written whole and renamed into place so the command never reads a partial answer, and appends
one `QueryLogEntry` per request, refusals and errors included, to `agent/query_log.jsonl`.

Each query runs in its own child process through `trajlab.store.query.run_query`, one at a time
per trial and trials in parallel up to `workers`; the server itself never opens the store, so
between queries nothing holds the file, a `store build` between attempts finds it free, and one
during a query fails on DuckDB's file lock instead of corrupting anything. The server is
separate from the checkpoint watcher (`trajlab watch`), so a slow query never delays a
checkpoint's ack. A request is answered once: one with an answer on disk, or one this server
already took up, is never run again; requests found unanswered at startup are answered then.
Only regular files are read or written, never through a symlink, and a request the host cannot
answer (a path it cannot write, say) is logged and left, not retried, so no request can stop
the server.
"""

import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from trajlab.contracts import (
    QUERIES_DIRNAME,
    QUERY_ID,
    QUERY_LOG_FILENAME,
    QUERY_TOOL_RELPATH,
    QueryLogEntry,
)
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
DEFAULT_WORKERS = 4
_QUERY_ID = re.compile(QUERY_ID)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ServeError(ValueError):
    """The server cannot start: the store does not answer, or the job dir is not a directory."""


def query_script() -> str:
    """The container-side command, as written into every served trial's `agent/bin/query`."""
    return files(__package__).joinpath("query.sh").read_text()


def queries_dir(agent_dir: Path) -> Path:
    return agent_dir / QUERIES_DIRNAME


def query_log_path(agent_dir: Path) -> Path:
    return agent_dir / QUERY_LOG_FILENAME


def _write_whole(path: Path, text: str, mode: int = 0o644) -> None:
    """Write `path` whole: a new regular file beside it, renamed into place. A leftover of the
    same name is removed first (a stale `.tmp`, or anything else: the file is made with
    O_EXCL and never through a symlink)."""
    tmp = path.with_name(path.name + TMP_SUFFIX)
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, mode)  # the umask applied at creation
    os.replace(tmp, path)


def _read_regular(path: Path) -> str:
    """The text of a regular file, never through a symlink; undecodable bytes are replaced."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def setup_agent_dir(agent_dir: Path) -> None:
    """Put the query tool into a trial's `agent/` dir: the command, executable by everyone; the
    requests dir, writable by everyone (the container's user is not the host's); and the log,
    which the host alone appends to. Running it again changes nothing. A requests dir the
    container's user made first (the command makes it, world-writable, when it finds none) is
    left as it is: on Linux the host cannot change its mode, and need not."""
    tool = agent_dir / QUERY_TOOL_RELPATH
    script = query_script()
    if tool.is_symlink() or not tool.is_file() or tool.read_text() != script:
        tool.parent.mkdir(parents=True, exist_ok=True)
        _write_whole(tool, script, mode=0o755)
    requests = queries_dir(agent_dir)
    try:
        requests.mkdir()
    except FileExistsError:
        pass
    else:
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


def pending_requests(
    agent_dir: Path, skip: frozenset[Path] | set[Path] = frozenset()
) -> list[Path]:
    """The `<id>.sql` regular files in the trial's requests dir with no `.result` or `.err` and
    not in `skip`, oldest first, from one listing. A name that is not an id, a symlink, a file
    still being written (`.tmp`), or one that vanished meanwhile is not a request."""
    requests = queries_dir(agent_dir)
    try:
        entries = {entry.name: entry for entry in os.scandir(requests)}
    except OSError:
        return []
    found: list[tuple[float, str, Path]] = []
    for name, entry in entries.items():
        if not name.endswith(QUERY_SUFFIX):
            continue
        query_id = name[: -len(QUERY_SUFFIX)]
        path = Path(entry.path)
        if path in skip or any(f"{query_id}{s}" in entries for s in (RESULT_SUFFIX, ERROR_SUFFIX)):
            continue
        if not _QUERY_ID.match(query_id):
            logger.warning("ignoring %s: not a request id", path)
            continue
        if not entry.is_file(follow_symlinks=False):
            logger.warning("ignoring %s: not a regular file", path)
            continue
        try:
            found.append((entry.stat(follow_symlinks=False).st_mtime, name, path))
        except OSError:
            continue
    found.sort()
    return [path for _, _, path in found]


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
    workers: int = DEFAULT_WORKERS
    known: set[Path] = field(default_factory=set, init=False)
    setup_failed: set[Path] = field(default_factory=set, init=False)
    taken: set[Path] = field(default_factory=set, init=False)
    in_flight: dict[Path, Future[None]] = field(default_factory=dict, init=False)
    answered: int = field(default=0, init=False)
    executor: ThreadPoolExecutor | None = field(default=None, init=False)

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
        """Serve until `stop` is set, then finish the queries under way. Each pass takes new trial
        dirs in and starts each trial's oldest pending request, one request per trial at a
        time, up to `workers` at once."""
        self.check()
        logger.info("serving %s to trials under %s", self.store_dir, self.job_dir)
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            self.executor = executor
            try:
                while not stop.is_set():
                    self.pass_once()
                    stop.wait(self.poll_s)
            finally:
                self.executor = None
        self._reap()
        logger.info("stopped; %d queries answered", self.answered)

    def pass_once(self) -> None:
        """One pass. Without an executor (no `run`), each request is answered before the pass
        returns."""
        self._reap()
        for trial_dir in trial_dirs(self.job_dir):
            agent_dir = trial_dir / "agent"
            if trial_dir not in self.known and not self._take_in(trial_dir, agent_dir):
                continue
            if trial_dir in self.in_flight:
                continue
            pending = pending_requests(agent_dir, self.taken)
            if not pending:
                continue
            request = pending[0]
            self.taken.add(request)
            if self.executor is None:
                self._answer_guarded(trial_dir.name, agent_dir, request)
                self.answered += 1
            else:
                self.in_flight[trial_dir] = self.executor.submit(
                    self._answer_guarded, trial_dir.name, agent_dir, request
                )

    def _take_in(self, trial_dir: Path, agent_dir: Path) -> bool:
        try:
            setup_agent_dir(agent_dir)
        except OSError as error:
            if trial_dir not in self.setup_failed:
                logger.warning("cannot set up %s: %s", agent_dir, error)
                self.setup_failed.add(trial_dir)
            return False
        self.known.add(trial_dir)
        self.setup_failed.discard(trial_dir)
        logger.info("serving %s", trial_dir.name)
        return True

    def _reap(self) -> None:
        """Forget finished answers, counting them; an answer that raised is logged here."""
        for trial_dir, future in list(self.in_flight.items()):
            if future.done():
                del self.in_flight[trial_dir]
                self.answered += 1
                if (error := future.exception()) is not None:
                    logger.error("answering a request of %s failed: %s", trial_dir.name, error)

    def _answer_guarded(self, trial_name: str, agent_dir: Path, request: Path) -> None:
        try:
            self.answer(trial_name, agent_dir, request)
        except Exception:  # whatever it was, the server goes on; the request is not retried
            logger.exception("cannot answer %s", request)

    def answer(self, trial_name: str, agent_dir: Path, request: Path) -> QueryLogEntry:
        """Answer one request: run it, write `.result` or `.err`, append the log entry."""
        received_at = datetime.now(UTC)
        query_id = request.name[: -len(QUERY_SUFFIX)]
        start = time.monotonic()
        try:
            sql = _read_regular(request)
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
