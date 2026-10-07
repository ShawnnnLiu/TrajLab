"""Mechanical tests of labels: hunk minimization and regression reverts (ADR-0013, 4 and 5).

`minimize_fix` replays a confirmed fix once without each of its raw hunks (every file a fix
creates or deletes is one hunk) and records which of the fix's checks fail without it, so an item
can keep only the hunks its checks need and split a fix that removes several causes.

`revert_regression` undoes, on the final state, what a regression's first failing point changed
in the graded files (`patch --reverse` with fuzz, since later edits may have moved the lines) and
replays the result: the regression is `confirmed` if every regressed check passes again and no
passing check breaks, `cause_moved` otherwise, and `cannot_revert` if the change cannot be undone
(a binary file, a file created at that point, or a hunk later edits removed).
"""

import fcntl
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from trajlab.contracts.groundtruth import (
    MINIMIZED_FILENAME,
    REVERTS_FILENAME,
    FixRecord,
    LeaveOut,
    MinimizationRecord,
    RevertRecord,
    TimelinePoint,
)
from trajlab.groundtruth.blame import apply_hunks, raw_hunks, split_keep, unified_diff
from trajlab.groundtruth.counterfactual import fix_contents, read_text, try_fix
from trajlab.groundtruth.extract import TrialInputs, groundtruth_dir, read_state, state_dir


def _append(path: Path, record: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(record.model_dump_json() + "\n")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_minimized(trial_dir: Path) -> dict[tuple[str, tuple[str, ...]], MinimizationRecord]:
    """The latest minimization of each (fix, checks) pair."""
    path = groundtruth_dir(trial_dir) / MINIMIZED_FILENAME
    if not path.is_file():
        return {}
    records = [MinimizationRecord.model_validate_json(x) for x in path.read_text().splitlines()]
    return {(r.fix_id, r.checks): r for r in records}


def read_reverts(trial_dir: Path) -> dict[tuple[int, tuple[str, ...]], RevertRecord]:
    """The latest revert test of each (point, checks) pair."""
    path = groundtruth_dir(trial_dir) / REVERTS_FILENAME
    if not path.is_file():
        return {}
    records = [RevertRecord.model_validate_json(x) for x in path.read_text().splitlines()]
    return {(r.point, r.checks): r for r in records}


def hunk_index(contents: dict[str, tuple[str | None, str | None]]) -> list[tuple[str, int | None]]:
    """The fix's raw hunks in canonical order: by path, then position; None = the whole file."""
    hunks: list[tuple[str, int | None]] = []
    for path in sorted(contents):
        before, after = contents[path]
        if before is None or after is None:
            hunks.append((path, None))
            continue
        found = raw_hunks(split_keep(before), split_keep(after))
        hunks += [(path, i) for i in range(len(found))]
    return hunks


def _try(inputs: TrialInputs, diff: str, like: FixRecord | None = None) -> FixRecord:
    """Try a diff the way `like` was tried (its mode and command), or on the graded files."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".diff", delete=False, errors="surrogateescape"
    ) as handle:
        handle.write(diff)
        patch = Path(handle.name)
    try:
        if like is None:
            return try_fix(inputs, patch, mode="artifacts")
        return try_fix(
            inputs,
            patch,
            mode=like.mode,
            command=like.command,
            user=like.command_user or "root",
            workdir=like.command_workdir,
            timeout=like.command_timeout_s or 900.0,
        )
    finally:
        patch.unlink(missing_ok=True)


def minimize_fix(
    inputs: TrialInputs, fix: FixRecord, checks: tuple[str, ...]
) -> MinimizationRecord:
    contents = fix_contents(inputs, fix)
    hunks = hunk_index(contents)
    leave_outs: list[LeaveOut] = []
    if len(hunks) > 1:
        for index, (dropped_path, dropped) in enumerate(hunks):
            diff = ""
            for path in sorted(contents):
                before, after = contents[path]
                if path == dropped_path:
                    if dropped is None:
                        continue
                    old = split_keep(before)  # type: ignore[arg-type]
                    new = split_keep(after)  # type: ignore[arg-type]
                    keep = set(range(len(raw_hunks(old, new)))) - {dropped}
                    after = "".join(apply_hunks(old, new, keep))
                diff += unified_diff(path, before, after)
            if not diff:
                leave_outs.append(LeaveOut(raw_hunk=index, needed_for=checks))
                continue
            reduced = _try(inputs, diff, like=fix)
            leave_outs.append(
                LeaveOut(
                    raw_hunk=index,
                    fix_id=reduced.fix_id,
                    # A try without a verdict is no evidence; items then keep every hunk.
                    needed_for=()
                    if reduced.error
                    else tuple(c for c in checks if c not in reduced.fixed),
                    broken=reduced.broken,
                    error=reduced.error,
                )
            )
    record = MinimizationRecord(
        trial_name=inputs.trial_dir.name,
        fix_id=fix.fix_id,
        checks=checks,
        raw_hunks=max(1, len(hunks)),
        leave_outs=tuple(leave_outs),
        created_at=datetime.now(UTC),
    )
    _append(groundtruth_dir(inputs.trial_dir) / MINIMIZED_FILENAME, record)
    return record


def _is_text(text: str | None) -> bool:
    return text is not None and "\x00" not in text and "\udcff" not in text


def _reverse(before: str, after: str, final: str) -> str | None:
    """`final` with the change from `before` to `after` undone, or None if it does not apply."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "before").write_text(before, errors="surrogateescape")
        (root / "after").write_text(after, errors="surrogateescape")
        (root / "final").write_text(final, errors="surrogateescape")
        diff = subprocess.run(
            ["diff", "-u", "before", "after"], cwd=root, capture_output=True, text=True
        ).stdout
        (root / "change.diff").write_text(diff)
        undone = subprocess.run(
            [
                "patch",
                "--reverse",
                "--forward",  # refuse, rather than re-apply, a change already undone
                "--fuzz=3",
                "--no-backup-if-mismatch",
                "--reject-file=-",
                "--output=reverted",
                "final",
                "change.diff",
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if undone.returncode != 0:
            return None
        return (root / "reverted").read_text(errors="surrogateescape")


def revert_regression(
    inputs: TrialInputs, points: list[TimelinePoint], index: int, checks: tuple[str, ...]
) -> RevertRecord:
    trial_dir = inputs.trial_dir
    before_point, at_point, final = points[index - 1], points[index], points[-1]

    def files(point: TimelinePoint) -> dict[str, tuple[str | None, bool]]:
        state = read_state(trial_dir, point.state_id)
        return {f.path: (f.sha256, f.executable) for f in state.files if f.kind == "file"}

    old, new = files(before_point), files(at_point)
    changed = sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))

    def record(verdict: str, **extra: object) -> RevertRecord:
        result = RevertRecord(
            trial_name=trial_dir.name,
            point=index,
            checks=checks,
            verdict=verdict,  # type: ignore[arg-type]
            created_at=datetime.now(UTC),
            **extra,  # type: ignore[arg-type]
        )
        _append(groundtruth_dir(trial_dir) / REVERTS_FILENAME, result)
        return result

    diff = ""
    for path in changed:
        before = read_text(state_dir(trial_dir, before_point.state_id) / "artifacts" / path)
        after = read_text(state_dir(trial_dir, at_point.state_id) / "artifacts" / path)
        now = read_text(state_dir(trial_dir, final.state_id) / "artifacts" / path)
        if before is None:
            return record("cannot_revert", reason=f"{path} was created at P{index}")
        if after is None:
            if now is not None:
                return record(
                    "cannot_revert", reason=f"{path} was deleted at P{index}, then remade"
                )
            diff += unified_diff(path, None, before)
            continue
        if not (_is_text(before) and _is_text(after) and _is_text(now)):
            return record("cannot_revert", reason=f"{path} is not text")
        assert now is not None
        reverted = _reverse(before, after, now)
        if reverted is None:
            return record("cannot_revert", reason=f"the change to {path} no longer applies")
        diff += unified_diff(path, now, reverted)
    if not diff:
        return record("cannot_revert", reason="no textual change to undo")
    attempt = _try(inputs, diff)
    if attempt.error:  # did not apply, or its replay had no verdict: no evidence either way
        return record("cannot_revert", fix_id=attempt.fix_id, reason=attempt.error)
    restored = tuple(c for c in checks if c in attempt.fixed)
    verdict = "confirmed" if set(restored) == set(checks) and not attempt.broken else "cause_moved"
    return record(verdict, fix_id=attempt.fix_id, restored=restored, broken=attempt.broken)
