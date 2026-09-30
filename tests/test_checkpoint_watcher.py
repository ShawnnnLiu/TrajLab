import json
import os
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import FIXTURE_TOOL_CALL_ID
from trajlab.checkpoint.backends.base import BackendError, Snapshot, SnapshotBackend
from trajlab.checkpoint.join import (
    RECORDS_FILENAME,
    WATCHER_LOG_FILENAME,
    CheckpointRequest,
    append_record,
    checkpoints_dir,
    read_records,
    read_request,
)
from trajlab.checkpoint.watcher import (
    TrialIdentity,
    Watcher,
    WatcherLockedError,
    hold_watcher_lock,
    watcher_running,
)
from trajlab.contracts import CheckpointRecord

TRIAL_NAME = "hello-world__K3GBok3"
PROJECT = "hello-world__k3gbok3__env"


class FakeBackend(SnapshotBackend):
    name = "docker_commit"

    def __init__(self) -> None:
        self.snapshots: list[tuple[str, str, dict[str, str]]] = []
        self.discarded: list[str] = []
        self.lookups = 0
        self.fail_lookup = False
        self.fail_snapshot = False
        self.on_snapshot: object = None

    def container_for(self, compose_project: str) -> str:
        assert compose_project == PROJECT
        self.lookups += 1
        if self.fail_lookup:
            raise BackendError("no container")
        return "c0ffee"

    def snapshot(self, container: str, *, tag: str, labels: Mapping[str, str]) -> Snapshot:
        if self.fail_snapshot:
            raise BackendError("commit failed")
        if callable(self.on_snapshot):
            self.on_snapshot()
        self.snapshots.append((container, tag, dict(labels)))
        n = len(self.snapshots)
        return Snapshot(
            checkpoint_id=f"sha256:{n:064x}",
            capture_ms=470,
            captured_at=datetime(2026, 9, 30, 12, 0, n, tzinfo=UTC),
            bytes=1000 * n,
        )

    def discard(self, snapshot: Snapshot) -> None:
        self.discarded.append(snapshot.checkpoint_id)


def identify(trial_dir: Path) -> TrialIdentity:
    return TrialIdentity(trial_name=TRIAL_NAME, compose_project=PROJECT)


@pytest.fixture
def running_trial(job_dir: Path) -> Path:
    """The fixture trial in a job dir, as it looks mid-run: no result.json yet."""
    trial = job_dir / TRIAL_NAME
    (trial / "result.json").unlink()
    return trial


def _request(trial: Path, tool_use_id: str = FIXTURE_TOOL_CALL_ID, tool: str = "Bash") -> Path:
    directory = checkpoints_dir(trial)
    directory.mkdir(parents=True, exist_ok=True)
    req = directory / f"{tool_use_id}.req"
    req.write_text(json.dumps({"tool_use_id": tool_use_id, "tool_name": tool, "agent_id": None}))
    return req


def test_process_records_and_acks(running_trial: Path) -> None:
    backend = FakeBackend()
    req = _request(running_trial)

    record = Watcher(running_trial.parent.parent, backend, identify).process(req)

    assert record is not None
    assert record.seq == 1
    assert record.trial_name == TRIAL_NAME
    assert record.tool_call_id == FIXTURE_TOOL_CALL_ID
    assert record.tool_name == "Bash"
    assert record.bytes == 1000
    assert record.requested_at == read_request(req)[1]
    directory = req.parent
    assert read_records(directory) == [record]
    ack = CheckpointRecord.model_validate_json(
        (directory / f"{FIXTURE_TOOL_CALL_ID}.ack").read_text()
    )
    assert ack == record
    assert backend.snapshots == [
        (
            "c0ffee",
            f"{TRIAL_NAME}.0001",
            {
                "trajlab.trial_name": TRIAL_NAME,
                "trajlab.tool_call_id": FIXTURE_TOOL_CALL_ID,
                "trajlab.seq": "1",
            },
        )
    ]
    assert "seq 1 Bash" in (directory / WATCHER_LOG_FILENAME).read_text()


def test_seq_counts_up_and_container_is_cached(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = Watcher(running_trial.parent.parent, backend, identify)

    seqs = [watcher.process(_request(running_trial, f"toolu_{i}")).seq for i in range(3)]  # type: ignore[union-attr]

    assert seqs == [1, 2, 3]
    assert backend.lookups == 1
    assert [r.seq for r in read_records(checkpoints_dir(running_trial))] == [1, 2, 3]


def test_answered_requests_are_ignored(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = Watcher(running_trial.parent.parent, backend, identify)
    req = _request(running_trial)
    watcher.process(req)

    assert watcher.process(req) is None
    _request(running_trial, "toolu_gaveup")
    (req.parent / "toolu_gaveup.timeout").touch()
    assert watcher.process(req.parent / "toolu_gaveup.req") is None
    assert len(backend.snapshots) == 1


def test_one_request_one_snapshot_whatever_the_path_spelling(
    running_trial: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression, 2026-09-30 acceptance run: the observer reported an absolute path and the
    # sweep a relative one, so two threads snapshotted the same call with the same seq.
    monkeypatch.chdir(running_trial.parent.parent.parent)
    relative_jobs = Path(running_trial.parent.parent.name)
    backend = FakeBackend()
    watcher = Watcher(relative_jobs, backend, identify)
    req = _request(running_trial)
    relative_req = req.relative_to(Path.cwd())
    started = threading.Barrier(2)
    backend.on_snapshot = lambda: time.sleep(0.2)

    def process(path: Path) -> None:
        started.wait()
        watcher.process(path)

    threads = [threading.Thread(target=process, args=(p,)) for p in (req, relative_req)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(backend.snapshots) == 1
    assert [r.seq for r in read_records(req.parent)] == [1]
    assert watcher.pending_requests() == []
    log_lines = (req.parent / WATCHER_LOG_FILENAME).read_text().splitlines()
    assert len([line for line in log_lines if "seq 1" in line]) == 1


def test_late_snapshot_is_discarded(running_trial: Path) -> None:
    backend = FakeBackend()
    req = _request(running_trial)
    # The hook gives up while the snapshot is being taken.
    backend.on_snapshot = lambda: req.with_suffix(".timeout").touch()

    assert Watcher(running_trial.parent.parent, backend, identify).process(req) is None
    assert backend.discarded == ["sha256:" + f"{1:064x}"]
    assert not req.with_suffix(".ack").exists()
    assert not (req.parent / RECORDS_FILENAME).exists()


def test_restart_reacks_recorded_request(running_trial: Path) -> None:
    # A previous watcher appended the record and died before writing the ack.
    req = _request(running_trial)
    earlier = CheckpointRecord(
        checkpoint_id="sha256:" + "a" * 64,
        trial_name=TRIAL_NAME,
        tool_call_id=FIXTURE_TOOL_CALL_ID,
        seq=1,
        tool_name="Bash",
        capture_ms=1,
        requested_at=datetime(2026, 9, 30, tzinfo=UTC),
        captured_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    append_record(req.parent, earlier)
    backend = FakeBackend()

    assert Watcher(running_trial.parent.parent, backend, identify).process(req) == earlier
    assert backend.snapshots == []
    assert req.with_suffix(".ack").exists()
    assert read_records(req.parent) == [earlier]


@pytest.mark.parametrize("failure", ["fail_lookup", "fail_snapshot"])
def test_backend_failure_leaves_request_unanswered(
    running_trial: Path, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    backend = FakeBackend()
    setattr(backend, failure, True)
    watcher = Watcher(running_trial.parent.parent, backend, identify)
    req = _request(running_trial)

    assert watcher.process(req) is None
    assert watcher.process(req) is None  # the sweep retries
    assert not req.with_suffix(".ack").exists()
    assert sum(r.levelname == "ERROR" for r in caplog.records) == 1

    setattr(backend, failure, False)
    assert watcher.process(req) is not None


def test_malformed_request_is_logged(running_trial: Path) -> None:
    req = _request(running_trial)
    req.write_text('{"tool_use_id": "toolu_other", "tool_name": "Bash"}')
    assert Watcher(running_trial.parent.parent, FakeBackend(), identify).process(req) is None
    assert "no checkpoint" in (req.parent / WATCHER_LOG_FILENAME).read_text()


def test_pending_requests_skip_finished_trials(running_trial: Path, fixture_trial: Path) -> None:
    watcher = Watcher(running_trial.parent.parent, FakeBackend(), identify)
    req = _request(running_trial)
    answered = _request(running_trial, "toolu_done")
    answered.with_suffix(".ack").touch()
    assert watcher.pending_requests() == [req]

    (running_trial / "result.json").write_text((fixture_trial / "result.json").read_text())
    assert watcher.pending_requests() == []


def test_request_file_round_trip(running_trial: Path) -> None:
    req = _request(running_trial, tool="Edit")
    request, requested_at = read_request(req)
    assert request == CheckpointRequest(tool_use_id=FIXTURE_TOOL_CALL_ID, tool_name="Edit")
    assert requested_at.tzinfo is not None
    assert abs(requested_at.timestamp() - os.stat(req).st_mtime) < 0.001


def test_lock_admits_one_watcher(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    assert not watcher_running(jobs)
    with hold_watcher_lock(jobs):
        assert watcher_running(jobs)
        with pytest.raises(WatcherLockedError), hold_watcher_lock(jobs):
            pass
    assert not watcher_running(jobs)


def test_run_answers_requests_as_they_appear(running_trial: Path) -> None:
    backend = FakeBackend()
    stuck = _request(running_trial, "toolu_before")  # waiting before the watcher started
    watcher = Watcher(running_trial.parent.parent, backend, identify, sweep_interval=0.2)
    stop = threading.Event()
    thread = threading.Thread(target=watcher.run, args=(stop,))
    thread.start()
    try:
        # The hook's atomic write: a temp file renamed to .req.
        directory = checkpoints_dir(running_trial)
        tmp = directory / "toolu_after.req.tmp"
        tmp.write_text(json.dumps({"tool_use_id": "toolu_after", "tool_name": "Write"}))
        tmp.rename(directory / "toolu_after.req")
        deadline = time.monotonic() + 10
        acks = [stuck.with_suffix(".ack"), directory / "toolu_after.ack"]
        while not all(a.exists() for a in acks) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=10)

    assert all(a.exists() for a in acks)
    assert not thread.is_alive()
    records = read_records(directory)
    assert sorted(r.tool_call_id for r in records) == ["toolu_after", "toolu_before"]
    assert [r.seq for r in records] == [1, 2]
