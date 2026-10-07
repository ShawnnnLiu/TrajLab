import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.models.task.config import ArtifactConfig
from harbor.models.trial.artifact_manifest import ArtifactManifest, ArtifactManifestEntry
from harbor.models.trial.paths import EnvironmentPaths
from harbor.trial.regrade import RegradeTrial

from trajlab.contracts.groundtruth import CheckResult, ReplayRecord
from trajlab.groundtruth.admission import Claim, Ledger, fits
from trajlab.groundtruth.blame import Hunk, blame, changed_lines, fix_hunks
from trajlab.groundtruth.checks import read_checks, statuses
from trajlab.groundtruth.extract import MANIFEST_FILENAME, describe_state, list_files
from trajlab.groundtruth.replay import ReplayTrial
from trajlab.groundtruth.timeline import CheckTimeline, Classification, classify

# --- checks ---------------------------------------------------------------------------------


def test_read_checks_parses_all_three_outputs(tmp_path: Path) -> None:
    (tmp_path / "ctrf.json").write_text(
        json.dumps(
            {
                "results": {
                    "tests": [
                        {"name": "test_a", "status": "passed"},
                        {"name": "test_b", "status": "failed", "message": "x" * 400},
                    ]
                }
            }
        )
    )
    (tmp_path / "trace_results.json").write_text(
        json.dumps([{"trace_id": "t1", "passed": False, "error": "boom"}])
    )
    (tmp_path / "reward_details.json").write_text(
        json.dumps({"score": 1.0, "combined_raw": 0.97, "base": {"iou": 0.9, "iou_reason": "ok"}})
    )
    checks = read_checks(tmp_path)
    assert [(c.kind, c.check, c.status) for c in checks] == [
        ("pytest", "test_a", "passed"),
        ("pytest", "test_b", "failed"),
        ("trace", "t1", "failed"),
        ("cad", "score", "passed"),
        ("cad", "base.iou", None),
    ]
    assert len(checks[1].message or "") == 300
    assert checks[4].value == 0.9 and checks[4].message == "ok"
    assert statuses(checks) == {
        "pytest:test_a": "passed",
        "pytest:test_b": "failed",
        "trace:t1": "failed",
        "cad:score": "passed",
    }


def test_read_checks_without_output_is_empty(tmp_path: Path) -> None:
    assert read_checks(tmp_path) == []


# --- states ---------------------------------------------------------------------------------


def _state_dir(root: Path, *, content: str = "x", executable: bool = False) -> Path:
    artifacts = root / "artifacts"
    (artifacts / "app" / "src").mkdir(parents=True)
    script = artifacts / "app" / "src" / "a.sh"
    script.write_text(content)
    if executable:
        script.chmod(0o755)
    os.symlink("a.sh", artifacts / "app" / "src" / "link")
    (artifacts / "logs" / "artifacts").mkdir(parents=True)
    entries = [
        ArtifactManifestEntry(
            source="/logs/artifacts",
            destination="artifacts/logs/artifacts",
            type="directory",
            status="empty",
            exclude=[],
        ),
        ArtifactManifestEntry(
            source="/app/src", destination="artifacts/app/src", type="directory", status="ok"
        ),
        ArtifactManifestEntry(
            source="/app/out.txt", destination="artifacts/app/out.txt", type="file", status="failed"
        ),
    ]
    (artifacts / MANIFEST_FILENAME).write_text(
        json.dumps(ArtifactManifest(entries=entries).to_json_data())
    )
    return artifacts


def test_list_files_records_kinds_without_following_links(tmp_path: Path) -> None:
    files = {f.path: f for f in list_files(_state_dir(tmp_path, executable=True))}
    assert MANIFEST_FILENAME not in files
    assert files["app/src/a.sh"].kind == "file" and files["app/src/a.sh"].executable
    assert files["app/src/link"].kind == "symlink" and files["app/src/link"].target == "a.sh"
    assert files["logs/artifacts"].kind == "directory"


def test_state_id_depends_on_bytes_and_modes_only(tmp_path: Path) -> None:
    one = describe_state(_state_dir(tmp_path / "1"), "t")
    same = describe_state(_state_dir(tmp_path / "2"), "t")
    edited = describe_state(_state_dir(tmp_path / "3", content="y"), "t")
    chmodded = describe_state(_state_dir(tmp_path / "4", executable=True), "t")
    assert one.state_id == same.state_id
    assert len({one.state_id, edited.state_id, chmodded.state_id}) == 3
    assert [(e.source, e.status) for e in one.entries] == [
        ("/app/out.txt", "failed"),
        ("/app/src", "ok"),
        ("/logs/artifacts", "empty"),
    ]


# --- replay ---------------------------------------------------------------------------------


def _coverage_problems(tmp_path: Path, status: str) -> list[str]:
    """Harbor's own wording for a manifest entry with this status (regrade.py)."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)
    fake = SimpleNamespace(agent_env_paths=EnvironmentPaths())
    entries = [
        ArtifactManifestEntry(
            source="/logs/artifacts",
            destination="artifacts/logs/artifacts",
            type="directory",
            status="empty",
            exclude=[],
        ),
        ArtifactManifestEntry(
            source="/app/out.txt", destination="artifacts/app/out.txt", type="file", status=status
        ),
    ]
    return RegradeTrial._artifact_coverage_problems(
        fake,  # type: ignore[arg-type]
        source_artifacts_dir=artifacts_dir,
        source_record_dir=tmp_path,
        entries=entries,
        declared_artifacts=[ArtifactConfig(source="/app/out.txt")],
    )


def _replay_trial(absent: frozenset[str]) -> ReplayTrial:
    trial = object.__new__(ReplayTrial)
    trial._absent_sources = absent
    trial.task = SimpleNamespace(name="terminal-bench/t")  # type: ignore[assignment]
    trial._source_paths = SimpleNamespace(trial_dir=Path("/x/state"))  # type: ignore[assignment]
    return trial


def test_replay_trial_accepts_absent_artifacts(tmp_path: Path) -> None:
    problems = _coverage_problems(tmp_path, "failed")
    assert len(problems) == 1  # Harbor's regrade refuses this state
    _replay_trial(frozenset({"/app/out.txt"}))._raise_artifact_coverage_error(problems)


def test_replay_trial_still_refuses_other_problems(tmp_path: Path) -> None:
    skipped = _coverage_problems(tmp_path / "skipped", "skipped")
    with pytest.raises(Exception, match="host-path collision"):
        _replay_trial(frozenset({"/app/out.txt"}))._raise_artifact_coverage_error(skipped)
    failed = _coverage_problems(tmp_path / "failed", "failed")
    with pytest.raises(Exception, match="collection failed"):
        _replay_trial(frozenset())._raise_artifact_coverage_error(failed)


# --- blame ----------------------------------------------------------------------------------


def test_blame_keeps_origins_of_unchanged_lines() -> None:
    versions = [
        (0, ["a", "b"]),
        (1, ["a", "x", "b"]),
        (2, ["a", "x", "c"]),
        (3, ["a", "x", "c"]),
    ]
    assert blame(versions) == [0, 1, 2]


def test_blame_restarts_after_the_file_is_absent() -> None:
    assert blame([(0, ["a"]), (1, None), (2, ["a", "b"])]) == [2, 2]
    with pytest.raises(ValueError):
        blame([(0, ["a"]), (1, None)])


def test_fix_hunks_separate_replacements_from_insertions() -> None:
    hunks = fix_hunks(["a", "b", "c"], ["a", "B", "c", "d"])
    assert hunks == [
        Hunk(removed=(2,), inserted_before=None, added=1),
        Hunk(removed=(), inserted_before=4, added=1),
    ]
    assert changed_lines(hunks) == 3


# --- timeline -------------------------------------------------------------------------------


def _tl(*row: str | None) -> CheckTimeline:
    return CheckTimeline("pytest:t", tuple(row))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (("failed", "passed", "passed", "failed", "failed"), Classification("regression", 3, 2)),
        (("passed", "failed", "absent", "failed"), Classification("regression", 1, 0)),
        (("failed", "failed", "failed"), Classification("never_passed")),
        (("absent", "skipped", "failed"), Classification("never_passed")),
        (("failed", "flaky", "failed"), Classification("unstable")),
        (("passed", "flaky", "failed"), Classification("unstable")),
        (("failed", None, "failed"), Classification("incomplete")),
        (("failed", "passed", "failed", "passed", "passed"), Classification("passes", 3)),
        (("failed", "flaky", "passed", "passed"), Classification("unstable")),
        (("passed", "passed"), Classification("passes", 0)),
    ],
)
def test_classify(row: tuple[str | None, ...], expected: Classification) -> None:
    assert classify(_tl(*row)) == expected


def test_parametrized_rows_take_their_node_ids(tmp_path: Path) -> None:
    rows = [
        {"name": "t.py::test_a", "status": "passed"},
        {"name": "t.py::test_p", "status": "failed"},
        {"name": "t.py::test_p", "status": "passed"},
        {"name": "t.py::test_p", "status": "failed"},
    ]
    (tmp_path / "ctrf.json").write_text(json.dumps({"results": {"tests": rows}}))
    (tmp_path / "test-stdout.txt").write_text(
        "== short test summary info ==\n"
        "PASSED tests/t.py::test_a\n"
        "PASSED tests/t.py::test_p[b]\n"
        "FAILED tests/t.py::test_p[a] - AssertionError\n"
        "FAILED tests/t.py::test_p[c]\n"
    )
    assert [(c.check, c.status) for c in read_checks(tmp_path)] == [
        ("t.py::test_a", "passed"),
        ("t.py::test_p[a]", "failed"),
        ("t.py::test_p[b]", "passed"),
        ("t.py::test_p[c]", "failed"),
    ]


def test_parametrized_rows_fall_back_to_ordinals(tmp_path: Path) -> None:
    rows = [{"name": "t.py::test_p", "status": "failed"}] * 2
    (tmp_path / "ctrf.json").write_text(json.dumps({"results": {"tests": rows}}))
    assert [c.check for c in read_checks(tmp_path)] == ["t.py::test_p#1", "t.py::test_p#2"]


# --- admission ------------------------------------------------------------------------------


def _claim(cpus: float, memory_mb: int = 4096, *, quiet: bool = False) -> Claim:
    return Claim(claim_id=f"c{cpus}{memory_mb}{quiet}", cpus=cpus, memory_mb=memory_mb, quiet=quiet)


@pytest.mark.parametrize(
    ("running", "claim", "expected"),
    [
        ([], _claim(4), True),
        ([_claim(4), _claim(2)], _claim(2), False),  # 8 CPUs > 7
        ([_claim(2, 16384), _claim(2, 8192)], _claim(2, 4096), False),  # memory
        ([], _claim(16, 65536), True),  # larger than the host, but alone
        ([_claim(2)], _claim(2, quiet=True), True),
        ([_claim(4)], _claim(2, quiet=True), False),
        ([_claim(2, quiet=True)], _claim(2), True),
        ([_claim(2, quiet=True), _claim(2)], _claim(2), False),
        ([_claim(2, quiet=True)], _claim(2, quiet=True), False),
    ],
)
def test_admission_fits(running: list[Claim], claim: Claim, expected: bool) -> None:
    assert fits(running, claim) is expected


def test_ledger_drops_claims_of_dead_processes(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    dead = Claim(claim_id="dead", cpus=7, memory_mb=1, pid=2**22 + 7, pid_start="1")
    assert ledger.try_admit(dead) == []
    assert ledger.try_admit(_claim(2)) == []  # the dead claim no longer holds the CPUs
    assert [c.claim_id for c in ledger.running()] == [_claim(2).claim_id]


# --- items, end to end on a synthetic trial -------------------------------------------------


def _write_state(trial_dir: Path, text: str | None) -> str:
    from trajlab.groundtruth.extract import staging_dir, store_state

    staged = staging_dir(trial_dir)
    artifacts = staged / "artifacts"
    (artifacts / "app").mkdir(parents=True)
    entry = {"source": "/app/x.py", "destination": "artifacts/app/x.py", "type": "file"}
    if text is None:
        entry["status"] = "failed"
    else:
        (artifacts / "app" / "x.py").write_text(text)
        entry["status"] = "ok"
    (artifacts / MANIFEST_FILENAME).write_text(json.dumps([{**entry, "exclude": []}]))
    return store_state(trial_dir, staged, "terminal-bench/x").state_id


def _record(state: str, purpose: str, statuses_: dict[str, str], **extra: Any) -> ReplayRecord:
    now = datetime(2026, 10, 7, tzinfo=UTC)
    return ReplayRecord(
        replay_id=f"r-{state[:6]}-{purpose}-{len(extra)}",
        trial_name="x__abc",
        state_id=state,
        purpose=purpose,  # type: ignore[arg-type]
        reward=0.0,
        checks=tuple(
            CheckResult(kind="pytest", check=name, status=status)
            for name, status in statuses_.items()
        ),
        harbor_version="0.23.0",
        task_ref="sha256:0",
        started_at=now,
        finished_at=now,
        **extra,
    )


def test_build_items_blames_the_checkpoint_that_wrote_the_fixed_line(tmp_path: Path) -> None:
    from trajlab.contracts.groundtruth import FixFile, FixRecord, TimelinePoint
    from trajlab.groundtruth.blame import unified_diff
    from trajlab.groundtruth.extract import TrialInputs, groundtruth_dir
    from trajlab.groundtruth.items import build_items
    from trajlab.groundtruth.replay import append_record

    trial = tmp_path / "job" / "x__abc"
    (trial / "verifier").mkdir(parents=True)
    (trial / "agent").mkdir()
    (trial / "config.json").write_text(json.dumps({"task": {"name": "terminal-bench/x"}}))
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 0}}}))
    tests = [{"name": "t1", "status": "failed"}, {"name": "t2", "status": "passed"}]
    (trial / "verifier" / "ctrf.json").write_text(json.dumps({"results": {"tests": tests}}))
    steps = [
        {"step_id": 2, "tool_calls": [{"tool_call_id": "toolu_1", "function_name": "Write"}]},
        {"step_id": 3, "tool_calls": [{"tool_call_id": "toolu_2", "function_name": "Edit"}]},
    ]
    (trial / "agent" / "trajectory.json").write_text(json.dumps({"steps": steps}))
    a = _write_state(trial, "a\nb\n")
    b = _write_state(trial, "a\nBUG\n")
    c = _write_state(trial, "a\nBUG\nc\n")
    fixed = _write_state(trial, "a\nb\nc\n")
    now = datetime(2026, 10, 7, tzinfo=UTC)
    points = [
        TimelinePoint(trial_name="x__abc", index=0, kind="initial", image="i", state_id=a),
        TimelinePoint(
            trial_name="x__abc",
            index=1,
            kind="checkpoint",
            seq=1,
            tool_call_id="toolu_1",
            covered_tool_call_ids=("toolu_1",),
            image="c1",
            state_id=b,
            captured_at=now,
        ),
        TimelinePoint(
            trial_name="x__abc",
            index=2,
            kind="checkpoint",
            seq=2,
            tool_call_id="toolu_2",
            covered_tool_call_ids=("toolu_2",),
            image="c2",
            state_id=c,
            captured_at=now,
        ),
        TimelinePoint(trial_name="x__abc", index=3, kind="final", state_id=c),
    ]
    gt = groundtruth_dir(trial)
    (gt / "points.jsonl").write_text("".join(p.model_dump_json() + "\n" for p in points))
    for state in (a, b):
        append_record(trial, _record(state, "timeline", {"t1": "failed", "t2": "passed"}))
    for n in range(2):
        append_record(trial, _record(c, "final", {"t1": "failed", "t2": "passed"}, load_1m=n))
    diff = unified_diff("app/x.py", "a\nBUG\nc\n", "a\nb\nc\n")
    sha = hashlib.sha256(diff.encode()).hexdigest()
    (gt / "patches").mkdir()
    (gt / "patches" / f"{sha}.diff").write_text(diff)
    append_record(
        trial,
        _record(
            fixed,
            "counterfactual",
            {"t1": "passed", "t2": "passed"},
            base_state_id=c,
            patch_sha256=sha,
        ),
    )
    fix = FixRecord(
        fix_id="fix-1",
        trial_name="x__abc",
        mode="artifacts",
        base_state_id=c,
        patch_sha256=sha,
        files=(FixFile(path="/app/x.py", existed=True, removed=(2,), added=1),),
        state_id=fixed,
        replay_id="r",
        fixed=("pytest:t1",),
        created_at=now,
    )
    (gt / "fixes.jsonl").write_text(fix.model_dump_json() + "\n")
    labels = {
        "trial_name": "x__abc",
        "labeler": "test",
        "causes": [{"checks": ["pytest:t1"], "fix_id": "fix-1", "explanation": "BUG line"}],
    }
    (gt / "labels.json").write_text(json.dumps(labels))
    inputs = TrialInputs(
        trial, "terminal-bench/x", [ArtifactConfig(source="/app/x.py")], "/logs/artifacts"
    )

    [item] = build_items(inputs)

    assert item.kind == "wrong_edit" and item.method == "counterfactual"
    assert item.points == (1,) and item.tool_call_ids == ("toolu_1",) and item.step_ids == (2,)
    [hunk] = item.hunks
    assert (hunk.removed, hunk.origins, hunk.earliest) == ((2,), (1,), (1,))
