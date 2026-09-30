"""The host-side watcher: answers every `.req` with a checkpoint (docs/checkpoint-protocol.md).

One watcher per jobs dir, enforced by a `flock` on `<jobs-dir>/.trajlab-watcher.lock`.
`checkpoint/` does not import `capture/` (CLAUDE.md constraint 3), so the caller supplies how a
trial dir maps to its name and compose project.
"""

import fcntl
import logging
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from harbor.models.trial.paths import TrialPaths
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from trajlab.checkpoint.backends.base import BackendError, SnapshotBackend
from trajlab.checkpoint.backends.docker_commit import image_tag
from trajlab.checkpoint.join import (
    REQ_SUFFIX,
    WATCHER_LOG_FILENAME,
    append_record,
    is_pending,
    read_records,
    read_request,
    timed_out,
    trial_dir_of,
    write_ack,
)
from trajlab.contracts import CheckpointRecord

logger = logging.getLogger(__name__)

LOCK_FILENAME = ".trajlab-watcher.lock"
REQ_GLOB = f"*/*/agent/checkpoints/*{REQ_SUFFIX}"
DEFAULT_SWEEP_INTERVAL_S = 2.0
DEFAULT_WORKERS = 8


class WatcherLockedError(RuntimeError):
    """Another watcher already holds this jobs dir."""


@dataclass(frozen=True)
class TrialIdentity:
    trial_name: str
    compose_project: str


Identify = Callable[[Path], TrialIdentity]


def lock_path(jobs_dir: Path) -> Path:
    return jobs_dir / LOCK_FILENAME


@contextmanager
def hold_watcher_lock(jobs_dir: Path) -> Iterator[None]:
    """Hold the jobs dir's watcher lock for the duration. Raises WatcherLockedError."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    with lock_path(jobs_dir).open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WatcherLockedError(f"a watcher already holds {lock_path(jobs_dir)}") from error
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n")
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def watcher_running(jobs_dir: Path) -> bool:
    """True if some process holds the jobs dir's watcher lock."""
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


@dataclass
class _Trial:
    identity: TrialIdentity
    log: logging.Logger
    handler: logging.Handler
    records: dict[str, CheckpointRecord]
    lock: threading.Lock = field(default_factory=threading.Lock)
    container: str | None = None


class Watcher:
    """Answers `.req` files under a jobs dir with checkpoints."""

    def __init__(
        self,
        jobs_dir: Path,
        backend: SnapshotBackend,
        identify: Identify,
        *,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL_S,
        workers: int = DEFAULT_WORKERS,
    ) -> None:
        # Observers report absolute paths and the sweep reports paths under jobs_dir; both are
        # resolved so one request and one trial have exactly one key.
        self.jobs_dir = jobs_dir.resolve()
        self.backend = backend
        self.identify = identify
        self.sweep_interval = sweep_interval
        self.workers = workers
        self._trials: dict[Path, _Trial] = {}
        self._trials_lock = threading.Lock()
        self._in_flight: set[Path] = set()
        self._in_flight_lock = threading.Lock()
        self._reported_failures: set[Path] = set()
        self._executor: ThreadPoolExecutor | None = None

    # One request -------------------------------------------------------------------------

    def process(self, req_path: Path) -> CheckpointRecord | None:
        """Answer one `.req`. Returns the record, or None if nothing was recorded.

        Never raises for a problem with this request; problems are logged and the hook's
        wait budget turns them into a `.timeout`.
        """
        req_path = req_path.resolve()
        if not is_pending(req_path):
            return None
        trial_dir = trial_dir_of(req_path)
        try:
            trial = self._trial(trial_dir)
        except Exception:
            self._report(req_path, logger, "cannot identify the trial for %s", req_path)
            return None
        with trial.lock:
            if not is_pending(req_path):
                return None
            try:
                return self._checkpoint(req_path, trial)
            except Exception:
                self._report(req_path, trial.log, "no checkpoint for %s", req_path.name)
                return None

    def _checkpoint(self, req_path: Path, trial: _Trial) -> CheckpointRecord | None:
        directory = req_path.parent
        request, requested_at = read_request(req_path)
        existing = trial.records.get(request.tool_use_id)
        if existing is not None:
            # Recorded before a crash but never acknowledged.
            write_ack(directory, existing)
            trial.log.info("re-acked %s (seq %d)", request.tool_use_id, existing.seq)
            return existing
        if trial.container is None:
            trial.container = self.backend.container_for(trial.identity.compose_project)
        seq = len(trial.records) + 1
        try:
            snapshot = self.backend.snapshot(
                trial.container,
                tag=image_tag(trial.identity.trial_name, seq),
                labels={
                    "trajlab.trial_name": trial.identity.trial_name,
                    "trajlab.tool_call_id": request.tool_use_id,
                    "trajlab.seq": str(seq),
                },
            )
        except BackendError:
            # The container may have been replaced; look it up again next time.
            trial.container = None
            raise
        if timed_out(req_path):
            # The agent resumed before the snapshot finished; the image is not this call's state.
            self.backend.discard(snapshot)
            trial.log.warning("discarded late snapshot for %s", request.tool_use_id)
            return None
        record = CheckpointRecord(
            checkpoint_id=snapshot.checkpoint_id,
            trial_name=trial.identity.trial_name,
            tool_call_id=request.tool_use_id,
            seq=seq,
            tool_name=request.tool_name,
            backend="docker_commit",
            capture_ms=snapshot.capture_ms,
            bytes=snapshot.bytes,
            path=snapshot.path,
            requested_at=requested_at,
            captured_at=snapshot.captured_at,
        )
        append_record(directory, record)
        trial.records[record.tool_call_id] = record
        write_ack(directory, record)
        trial.log.info(
            "seq %d %s %s: %s in %d ms",
            seq,
            request.tool_name,
            request.tool_use_id,
            snapshot.checkpoint_id[:19],
            snapshot.capture_ms,
        )
        return record

    def _report(self, req_path: Path, log: logging.Logger, message: str, *args: object) -> None:
        # The sweep retries a failing request every interval; log the traceback once.
        if req_path in self._reported_failures:
            log.debug(message, *args, exc_info=True)
        else:
            self._reported_failures.add(req_path)
            log.error(message, *args, exc_info=True)

    def _trial(self, trial_dir: Path) -> _Trial:
        with self._trials_lock:
            trial = self._trials.get(trial_dir)
            if trial is None:
                identity = self.identify(trial_dir)
                directory = trial_dir / "agent" / "checkpoints"
                log = logging.getLogger(f"{__name__}.{identity.trial_name}")
                handler = logging.FileHandler(directory / WATCHER_LOG_FILENAME)
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                log.addHandler(handler)
                log.setLevel(logging.DEBUG)
                records = {r.tool_call_id: r for r in read_records(directory)}
                trial = _Trial(identity=identity, log=log, handler=handler, records=records)
                self._trials[trial_dir] = trial
            return trial

    def _forget_finished_trials(self) -> None:
        with self._trials_lock:
            for trial_dir in [d for d in self._trials if TrialPaths(d).result_path.exists()]:
                trial = self._trials.pop(trial_dir)
                trial.log.removeHandler(trial.handler)
                trial.handler.close()

    # Discovery ---------------------------------------------------------------------------

    def pending_requests(self) -> list[Path]:
        """Unanswered `.req` files of trials that are still running (replay on start)."""
        return sorted(
            req
            for req in self.jobs_dir.glob(REQ_GLOB)
            if is_pending(req) and not TrialPaths(trial_dir_of(req)).result_path.exists()
        )

    def sweep(self) -> None:
        self._forget_finished_trials()
        for req in self.pending_requests():
            self.submit(req)

    def submit(self, req_path: Path) -> None:
        req_path = req_path.resolve()
        with self._in_flight_lock:
            if req_path in self._in_flight or self._executor is None:
                return
            self._in_flight.add(req_path)
        self._executor.submit(self._run_one, req_path)

    def _run_one(self, req_path: Path) -> None:
        try:
            self.process(req_path)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(req_path)

    def run(self, stop: threading.Event) -> None:
        """Watch until `stop` is set: filesystem events, plus a sweep every interval."""
        handler = _RequestHandler(self)
        observer = Observer()
        observer.schedule(handler, str(self.jobs_dir), recursive=True)
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="ckpt") as pool:
            self._executor = pool
            observer.start()
            logger.info("watching %s with backend %s", self.jobs_dir, self.backend.name)
            try:
                while not stop.is_set():
                    self.sweep()
                    stop.wait(self.sweep_interval)
            finally:
                observer.stop()
                observer.join()
                self._executor = None
        self._forget_finished_trials()


class _RequestHandler(FileSystemEventHandler):
    def __init__(self, watcher: Watcher) -> None:
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_submit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_submit(event.dest_path)

    def _maybe_submit(self, raw: str | bytes) -> None:
        path = Path(os.fsdecode(raw))
        if path.suffix == REQ_SUFFIX and path.parent.name == "checkpoints":
            self.watcher.submit(path)
