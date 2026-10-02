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
    read_calls,
    read_records,
    read_request,
)
from trajlab.checkpoint.watcher import (
    PolicyMismatchError,
    TrialIdentity,
    Watcher,
    WatcherLockedError,
    hold_watcher_lock,
    watcher_running,
)
from trajlab.contracts import CallRecord, CheckpointPolicy, CheckpointRecord

TRIAL_NAME = "hello-world__K3GBok3"
PROJECT = "hello-world__k3gbok3__env"


class FakeBackend(SnapshotBackend):
    """A container with an in-memory filesystem: path -> (type, change time)."""

    name = "docker_commit"

    def __init__(self) -> None:
        self.snapshots: list[tuple[str, str, dict[str, str]]] = []
        self.discarded: list[str] = []
        self.lookups = 0
        self.listings = 0
        self.fail_lookup = False
        self.fail_snapshot = False
        self.gnu_find = True
        self.on_snapshot: object = None
        self.fs: dict[str, tuple[str, float]] = {"/": ("d", 1.0), "/app": ("d", 1.0)}
        self.clock = 100.0

    def write(self, path: str, kind: str = "f") -> None:
        self.clock += 1
        self.fs[path] = (kind, self.clock)

    def remove(self, path: str) -> None:
        del self.fs[path]

    def container_for(self, compose_project: str) -> str:
        assert compose_project == PROJECT
        self.lookups += 1
        if self.fail_lookup:
            raise BackendError("no container")
        return "c0ffee"

    def listing(self, container: str) -> str | None:
        self.listings += 1
        if not self.gnu_find:
            return None
        return "".join(
            "\0".join((kind, "644", "0", "0", "1", str(ctime), "", path)) + "\0"
            for path, (kind, ctime) in self.fs.items()
        )

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


def _watcher(trial: Path, backend: SnapshotBackend, **policy: object) -> Watcher:
    policy = {"every": 1} | policy
    return Watcher(trial.parent.parent, backend, identify, **policy)  # type: ignore[arg-type]


_CLOCK = iter(range(1_790_000_000, 1_800_000_000))


def _request(
    trial: Path,
    tool_use_id: str = FIXTURE_TOOL_CALL_ID,
    tool: str = "Bash",
    event: str = "PostToolUse",
) -> Path:
    directory = checkpoints_dir(trial)
    directory.mkdir(parents=True, exist_ok=True)
    req = directory / f"{tool_use_id}.req"
    fields = {"tool_use_id": tool_use_id, "tool_name": tool, "agent_id": None, "event": event}
    req.write_text(json.dumps(fields))
    stamp = next(_CLOCK)
    os.utime(req, (stamp, stamp))  # request order is mtime order; make it unambiguous
    return req


def _ack(req: Path) -> CallRecord:
    return CallRecord.model_validate_json(req.with_suffix(".ack").read_text())


def _records(trial: Path) -> list[CheckpointRecord]:
    return read_records(checkpoints_dir(trial))


# Every call (gate none) ------------------------------------------------------------------


def test_process_records_and_acks(running_trial: Path) -> None:
    backend = FakeBackend()
    req = _request(running_trial)

    call = _watcher(running_trial, backend).process(req)

    assert call is not None
    assert (call.outcome, call.change, call.checkpoint_seq, call.call_seq) == (
        "checkpoint",
        "not_checked",
        1,
        1,
    )
    assert call.detect_ms is None
    assert backend.listings == 0  # gate none never lists the filesystem
    [record] = _records(running_trial)
    assert record.seq == 1
    assert record.trial_name == TRIAL_NAME
    assert record.tool_call_id == FIXTURE_TOOL_CALL_ID
    assert record.tool_name == "Bash"
    assert record.bytes == 1000
    assert record.requested_at == read_request(req)[1] == call.requested_at
    assert _ack(req) == call
    assert read_calls(req.parent) == [call]
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
    assert "seq 1 Bash" in (req.parent / WATCHER_LOG_FILENAME).read_text()


def test_seq_counts_up_and_container_is_cached(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend)

    calls = [watcher.process(_request(running_trial, f"toolu_{i}")) for i in range(3)]

    assert [c.checkpoint_seq for c in calls] == [1, 2, 3]  # type: ignore[union-attr]
    assert [c.call_seq for c in calls] == [1, 2, 3]  # type: ignore[union-attr]
    assert backend.lookups == 1
    assert [r.seq for r in _records(running_trial)] == [1, 2, 3]


def test_answered_requests_are_ignored(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend)
    req = _request(running_trial)
    watcher.process(req)

    assert watcher.process(req) is None
    gave_up = _request(running_trial, "toolu_gaveup")
    gave_up.with_suffix(".timeout").touch()
    assert watcher.process(gave_up) is None
    assert len(backend.snapshots) == 1


def test_one_request_one_snapshot_whatever_the_path_spelling(
    running_trial: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression, 2026-09-30 acceptance run: the observer reported an absolute path and the
    # sweep a relative one, so two threads snapshotted the same call with the same seq.
    monkeypatch.chdir(running_trial.parent.parent.parent)
    relative_jobs = Path(running_trial.parent.parent.name)
    backend = FakeBackend()
    watcher = Watcher(relative_jobs, backend, identify, every=1)
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
    assert [r.seq for r in _records(running_trial)] == [1]
    assert len(read_calls(req.parent)) == 1
    assert watcher.pending_requests() == []


def test_late_snapshot_is_discarded(running_trial: Path) -> None:
    backend = FakeBackend()
    req = _request(running_trial)
    # The hook gives up while the snapshot is being taken.
    backend.on_snapshot = lambda: req.with_suffix(".timeout").touch()

    assert _watcher(running_trial, backend).process(req) is None
    assert backend.discarded == ["sha256:" + f"{1:064x}"]
    assert not req.with_suffix(".ack").exists()
    assert not (req.parent / RECORDS_FILENAME).exists()
    assert read_calls(req.parent) == []


def test_restart_reacks_recorded_call(running_trial: Path) -> None:
    backend = FakeBackend()
    req = _request(running_trial)
    call = _watcher(running_trial, backend).process(req)
    req.with_suffix(".ack").unlink()  # lost, e.g. a crash before the write reached disk

    assert _watcher(running_trial, backend).process(req) == call
    assert _ack(req) == call
    assert len(backend.snapshots) == 1


def test_restart_recovers_checkpoint_without_call_record(running_trial: Path) -> None:
    # A previous watcher appended the checkpoint and died before the call record and ack.
    req = _request(running_trial)
    earlier = CheckpointRecord(
        checkpoint_id="sha256:" + "a" * 64,
        trial_name=TRIAL_NAME,
        tool_call_id=FIXTURE_TOOL_CALL_ID,
        seq=1,
        covered_tool_call_ids=(FIXTURE_TOOL_CALL_ID,),
        tool_name="Bash",
        capture_ms=1,
        requested_at=datetime(2026, 9, 30, tzinfo=UTC),
        captured_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    append_record(req.parent, earlier)
    backend = FakeBackend()

    call = _watcher(running_trial, backend).process(req)

    assert call is not None
    assert (call.outcome, call.checkpoint_seq, call.change) == ("checkpoint", 1, "unknown")
    assert backend.snapshots == []
    assert _records(running_trial) == [earlier]


@pytest.mark.parametrize("failure", ["fail_lookup", "fail_snapshot"])
def test_backend_failure_leaves_request_unanswered(
    running_trial: Path, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    backend = FakeBackend()
    setattr(backend, failure, True)
    watcher = _watcher(running_trial, backend)
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
    assert _watcher(running_trial, FakeBackend()).process(req) is None
    assert "no checkpoint" in (req.parent / WATCHER_LOG_FILENAME).read_text()


def test_pending_requests_skip_finished_trials(running_trial: Path, fixture_trial: Path) -> None:
    watcher = _watcher(running_trial, FakeBackend())
    req = _request(running_trial)
    answered = _request(running_trial, "toolu_done")
    answered.with_suffix(".ack").touch()
    assert watcher.pending_requests() == [req]

    (running_trial / "result.json").write_text((fixture_trial / "result.json").read_text())
    assert watcher.pending_requests() == []


def test_request_file_round_trip(running_trial: Path) -> None:
    req = _request(running_trial, tool="Edit")
    request, requested_at = read_request(req)
    assert request == CheckpointRequest(
        tool_use_id=FIXTURE_TOOL_CALL_ID, tool_name="Edit", event="PostToolUse"
    )
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
    watcher = Watcher(running_trial.parent.parent, backend, identify, every=1, sweep_interval=0.2)
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
    records = _records(running_trial)
    assert sorted(r.tool_call_id for r in records) == ["toolu_after", "toolu_before"]
    assert [r.seq for r in records] == [1, 2]


# Every Nth call (ADR-0007) ---------------------------------------------------------------


def test_every_n_defers_then_covers(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, every=3)
    reqs = [_request(running_trial, f"toolu_{i}") for i in range(1, 7)]

    calls = [watcher.process(req) for req in reqs]

    assert [c.outcome for c in calls] == ["deferred", "deferred", "checkpoint"] * 2  # type: ignore[union-attr]
    assert [c.checkpoint_seq for c in calls] == [None, None, 1, None, None, 2]  # type: ignore[union-attr]
    first, second = _records(running_trial)
    assert first.covered_tool_call_ids == ("toolu_1", "toolu_2", "toolu_3")
    assert second.covered_tool_call_ids == ("toolu_4", "toolu_5", "toolu_6")
    assert _ack(reqs[0]).outcome == "deferred"
    assert [labels["trajlab.tool_call_id"] for _, _, labels in backend.snapshots] == [
        "toolu_3",
        "toolu_6",
    ]


def test_every_n_count_survives_restart(running_trial: Path) -> None:
    for i in (1, 2):
        _watcher(running_trial, FakeBackend(), every=3).process(_request(running_trial, f"t_{i}"))

    _watcher(running_trial, FakeBackend(), every=3).process(_request(running_trial, "t_3"))

    [record] = _records(running_trial)
    assert record.covered_tool_call_ids == ("t_1", "t_2", "t_3")


def test_timed_out_calls_are_covered_by_the_next_checkpoint(running_trial: Path) -> None:
    watcher = _watcher(running_trial, FakeBackend(), every=2)
    # The hook gave up on this call while no watcher was running.
    _request(running_trial, "toolu_missed").with_suffix(".timeout").touch()

    watcher.process(_request(running_trial, "toolu_next"))

    [record] = _records(running_trial)
    assert record.covered_tool_call_ids == ("toolu_missed", "toolu_next")


def test_discarded_snapshot_leaves_calls_for_the_next(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend)
    late = _request(running_trial, "toolu_late")
    backend.on_snapshot = lambda: late.with_suffix(".timeout").touch()
    assert watcher.process(late) is None

    backend.on_snapshot = None
    watcher.process(_request(running_trial, "toolu_next"))

    [record] = _records(running_trial)
    assert record.seq == 1
    assert record.covered_tool_call_ids == ("toolu_late", "toolu_next")


def test_policy_is_written_once_and_enforced(running_trial: Path) -> None:
    _watcher(running_trial, FakeBackend(), every=2).process(_request(running_trial, "toolu_1"))
    policy = checkpoints_dir(running_trial) / "policy.json"
    assert CheckpointPolicy.model_validate_json(policy.read_text()) == CheckpointPolicy(every=2)

    backend = FakeBackend()
    req = _request(running_trial, "toolu_2")
    assert _watcher(running_trial, backend, every=1, gate="change").process(req) is None
    assert not req.with_suffix(".ack").exists()
    assert backend.snapshots == []
    assert CheckpointPolicy.model_validate_json(policy.read_text()).every == 2


def test_gate_requires_every_one(running_trial: Path) -> None:
    with pytest.raises(ValueError, match="requires every=1"):
        _watcher(running_trial, FakeBackend(), every=2, gate="change")


# Only calls that changed the filesystem (ADR-0010) ----------------------------------------


def test_first_call_is_the_baseline(running_trial: Path) -> None:
    backend = FakeBackend()
    call = _watcher(running_trial, backend, gate="change").process(_request(running_trial))

    assert call is not None
    assert (call.outcome, call.change, call.checkpoint_seq) == ("checkpoint", "baseline", 1)
    assert call.changed_paths == ()
    assert call.changed_paths_total is None  # nothing to compare this call with
    assert call.detect_ms is not None
    assert len(backend.snapshots) == 1


def test_unchanged_calls_are_not_checkpointed(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, gate="change")
    watcher.process(_request(running_trial, "toolu_baseline"))

    # `ls; cat` changes nothing; harness paths do not count either.
    backend.write("/tmp/claude-0/-app/tasks/x.output")
    backend.write("/root/.local/state/claude/locks/2.1.278.lock")
    read = watcher.process(_request(running_trial, "toolu_read"))
    backend.write("/app/out.txt")
    write = watcher.process(_request(running_trial, "toolu_write", tool="Write"))
    again = watcher.process(_request(running_trial, "toolu_read_again"))

    assert read is not None and write is not None and again is not None
    assert (read.outcome, read.change, read.checkpoint_seq) == ("unchanged", "unchanged", 1)
    assert read.changed_paths == () and read.changed_paths_total == 0
    assert (write.outcome, write.change, write.checkpoint_seq) == ("checkpoint", "changed", 2)
    assert write.changed_paths == ("+/app/out.txt",)
    assert (again.outcome, again.checkpoint_seq) == ("unchanged", 2)
    assert [labels["trajlab.tool_call_id"] for _, _, labels in backend.snapshots] == [
        "toolu_baseline",
        "toolu_write",
    ]
    assert [r.covered_tool_call_ids for r in _records(running_trial)] == [
        ("toolu_baseline",),
        ("toolu_write",),  # the unchanged call holds no effects and is covered by nothing
    ]
    assert [c.call_seq for c in read_calls(checkpoints_dir(running_trial))] == [1, 2, 3, 4]


def test_modifying_and_deleting_count_as_changes(running_trial: Path) -> None:
    backend = FakeBackend()
    backend.write("/app/data.txt")
    backend.write("/app/old.txt")
    watcher = _watcher(running_trial, backend, gate="change")
    watcher.process(_request(running_trial, "toolu_baseline"))

    backend.write("/app/data.txt")  # same path, new change time
    edit = watcher.process(_request(running_trial, "toolu_edit", tool="Edit"))
    backend.remove("/app/old.txt")
    delete = watcher.process(_request(running_trial, "toolu_rm"))

    assert edit is not None and delete is not None
    assert (edit.outcome, edit.changed_paths) == ("checkpoint", ("~/app/data.txt",))
    assert (delete.outcome, delete.changed_paths) == ("checkpoint", ("-/app/old.txt",))


def test_reverting_a_change_is_unchanged_against_the_checkpoint(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, gate="change")
    watcher.process(_request(running_trial, "toolu_baseline"))
    backend.write("/app/tmp.txt")
    watcher.process(_request(running_trial, "toolu_create"))

    backend.remove("/app/tmp.txt")
    removed = watcher.process(_request(running_trial, "toolu_remove"))
    backend.write("/app/tmp2.txt")
    backend.remove("/app/tmp2.txt")
    churn = watcher.process(_request(running_trial, "toolu_churn"))

    assert removed is not None and removed.outcome == "checkpoint"
    # Created and deleted within one call: the state equals the last checkpoint's.
    assert churn is not None and (churn.outcome, churn.checkpoint_seq) == ("unchanged", 3)


def test_discarded_snapshot_keeps_the_old_reference(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, gate="change")
    watcher.process(_request(running_trial, "toolu_baseline"))
    backend.write("/app/a.txt")
    late = _request(running_trial, "toolu_late")
    backend.on_snapshot = lambda: late.with_suffix(".timeout").touch()
    assert watcher.process(late) is None

    backend.on_snapshot = None
    # This call writes nothing, but the state still differs from the last kept checkpoint.
    call = watcher.process(_request(running_trial, "toolu_quiet"))

    assert call is not None
    assert (call.outcome, call.change, call.changed_paths_total) == ("checkpoint", "changed", 0)
    assert _records(running_trial)[-1].covered_tool_call_ids == ("toolu_late", "toolu_quiet")


def test_no_gnu_find_checkpoints_every_call(running_trial: Path) -> None:
    backend = FakeBackend()
    backend.gnu_find = False
    watcher = _watcher(running_trial, backend, gate="change")

    calls = [watcher.process(_request(running_trial, f"toolu_{i}")) for i in range(2)]

    assert [(c.outcome, c.change) for c in calls] == [("checkpoint", "unknown")] * 2  # type: ignore[union-attr]
    assert len(backend.snapshots) == 2


def test_restart_starts_a_new_baseline(running_trial: Path) -> None:
    backend = FakeBackend()
    _watcher(running_trial, backend, gate="change").process(_request(running_trial, "t_1"))

    call = _watcher(running_trial, backend, gate="change").process(_request(running_trial, "t_2"))

    assert call is not None and (call.outcome, call.change) == ("checkpoint", "baseline")


def test_audit_measures_but_checkpoints_every_call(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, gate="audit")
    watcher.process(_request(running_trial, "toolu_baseline"))

    read = watcher.process(_request(running_trial, "toolu_read"))
    backend.write("/app/x")
    write = watcher.process(_request(running_trial, "toolu_write"))

    assert read is not None and write is not None
    assert (read.outcome, read.change) == ("checkpoint", "unchanged")
    assert (write.outcome, write.change) == ("checkpoint", "changed")
    assert len(backend.snapshots) == 3


def test_policy_mismatch_names_both_policies(running_trial: Path) -> None:
    _watcher(running_trial, FakeBackend(), gate="change").process(_request(running_trial, "t_1"))
    watcher = _watcher(running_trial, FakeBackend(), gate="audit")
    with pytest.raises(PolicyMismatchError, match="gate='change'"):
        watcher._trial(running_trial.resolve())


def test_failed_calls_are_measured_and_marked(running_trial: Path) -> None:
    # Regression, 2026-10-01: a Bash call that wrote a script and then exited 1 fired no
    # PostToolUse hook, so its changes were blamed on the next call.
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, gate="change")
    watcher.process(_request(running_trial, "toolu_baseline"))
    backend.write("/app/enc.py")
    failed = watcher.process(_request(running_trial, "toolu_failed", event="PostToolUseFailure"))
    ok = watcher.process(_request(running_trial, "toolu_ok"))

    assert failed is not None and ok is not None
    assert (failed.tool_failed, failed.outcome, failed.changed_paths) == (
        True,
        "checkpoint",
        ("+/app/enc.py",),
    )
    assert (ok.tool_failed, ok.outcome) == (False, "unchanged")


# The end of the turn (ADR-0011) ------------------------------------------------------------


def _stop(trial: Path, n: int = 1, event: str = "Stop") -> Path:
    return _request(trial, f"stop_179000000{n}_42", tool=event, event=event)


def test_stop_checkpoints_what_happened_after_the_last_call(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, gate="change")
    watcher.process(_request(running_trial, "toolu_baseline"))
    backend.write("/app/late.txt")  # a background job finished after the last hooked call

    stop = watcher.process(_stop(running_trial))

    assert stop is not None
    assert (stop.trigger, stop.outcome, stop.change, stop.tool_name) == (
        "stop",
        "checkpoint",
        "changed",
        "Stop",
    )
    record = _records(running_trial)[-1]
    assert (record.trigger, record.tool_call_id) == ("stop", stop.tool_call_id)


def test_stop_with_nothing_new_points_at_the_last_checkpoint(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, gate="change")
    watcher.process(_request(running_trial, "toolu_baseline"))

    stop = watcher.process(_stop(running_trial))

    assert stop is not None
    assert (stop.trigger, stop.outcome, stop.checkpoint_seq) == ("stop", "unchanged", 1)
    assert len(backend.snapshots) == 1


def test_stop_without_any_hooked_call_is_the_baseline(running_trial: Path) -> None:
    # An agent that only read files still leaves a final checkpoint.
    stop = _watcher(running_trial, FakeBackend(), gate="change").process(_stop(running_trial))
    assert stop is not None and (stop.outcome, stop.change) == ("checkpoint", "baseline")


def test_stop_is_never_deferred_under_every_n(running_trial: Path) -> None:
    backend = FakeBackend()
    watcher = _watcher(running_trial, backend, every=5)
    watcher.process(_request(running_trial, "toolu_1"))
    watcher.process(_request(running_trial, "toolu_2"))

    stop = watcher.process(_stop(running_trial, event="StopFailure"))

    assert stop is not None and (stop.outcome, stop.tool_failed) == ("checkpoint", False)
    [record] = _records(running_trial)
    assert record.covered_tool_call_ids == ("toolu_1", "toolu_2", stop.tool_call_id)
    assert record.trigger == "stop"
