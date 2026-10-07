"""Ground-truth items from the timeline and from confirmed fixes (ADR-0013, decisions 4 and 5).

`build_items` rewrites a trial's `groundtruth/items.jsonl`. Checks that gate 2 excludes (flaky
or irreproducible) never appear in an item.

- One `regression` item per timeline point at which checks that pass just before it fail from
  it to the end, once both states have `REPEAT_SAMPLES` replays with a verdict. Its revert test
  (`trajlab.groundtruth.minimize.revert_regression`) is recorded as a flag.
- For each cause in the labeler's `labels.json`: a fix confirms it if every counterfactual
  replay of the fix's state turns every claimed check from failing to passing and breaks none.
  The labeler's fix is the primary answer when it confirms; every other recorded fix that
  confirms the same checks is an alternative answer. The primary fix is blamed hunk by hunk
  (`trajlab.groundtruth.blame`), and if it was minimized, only the hunks the checks need are
  kept and checks that need disjoint hunks become separate items. A cause no fix confirms is an
  `unconfirmed` item.

Flags tell a reviewer or a scorer what to know about an item; they are listed in ADR-0013.
"""

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harbor.models.trial.paths import TrialPaths

from trajlab.contracts.checkpoint import CALLS_FILENAME, CHECKPOINTS_DIRNAME, CallRecord
from trajlab.contracts.groundtruth import (
    ITEMS_FILENAME,
    LABELS_FILENAME,
    PROGRESS_FILENAME,
    REFUTATIONS_FILENAME,
    AlternativeFix,
    BlamedHunk,
    FirstPass,
    FixRecord,
    GradedChange,
    GroundTruthItem,
    HunkKind,
    ItemKind,
    LabelCause,
    MinimizationRecord,
    RefutationRecord,
    ReplayRecord,
    TimelinePoint,
    TrialLabels,
    TrialProgress,
)
from trajlab.groundtruth.blame import (
    blame,
    first_seen,
    normalize,
    raw_hunks,
    split_keep,
    split_lines,
    view_of,
)
from trajlab.groundtruth.checks import PASSED, read_checks, statuses
from trajlab.groundtruth.counterfactual import (
    artifact_roots,
    file_history,
    fix_contents,
    in_artifacts,
    read_fixes,
)
from trajlab.groundtruth.extract import TrialInputs, groundtruth_dir, read_points, read_state
from trajlab.groundtruth.gates import excluded_checks
from trajlab.groundtruth.minimize import hunk_index, read_minimized, read_reverts
from trajlab.groundtruth.replay import FIX_SAMPLES, QUIET_FIX_SAMPLES, read_records
from trajlab.groundtruth.timeline import (
    CheckTimeline,
    Classification,
    check_timelines,
    classify,
    state_samples,
)
from trajlab.groundtruth.traits import traits

REPEAT_SAMPLES = 3  # agreeing replays of each state around a regression
LARGE_FIX_LINES = 40  # a fix this large localizes little; flagged, not dropped
JSON_VIEW_MAX_LINES = 3  # a JSON file this short is blamed in its pretty-printed view
MAX_RELATED_CALLS = 20
_RESTORE = re.compile(
    r"(^|[\s;&|(])(cp|mv|rsync|install|tar|unzip|git\s+(checkout|restore|stash|reset))\s"
)
_LITERAL = re.compile(
    r"""(?<![\w.])-?\d+\.\d+(?![\w.])|(?<![\w.])\d{3,}(?![\w.])|"[^"\n]{6,}"|'[^'\n]{6,}'"""
)
_KIND_ORDER: tuple[HunkKind, ...] = (
    "wrong_edit",
    "incomplete_edit",
    "missed_fix",
    "missing_artifact",
    "omission",
)


def items_path(trial_dir: Path) -> Path:
    return groundtruth_dir(trial_dir) / ITEMS_FILENAME


def labels_path(trial_dir: Path) -> Path:
    return groundtruth_dir(trial_dir) / LABELS_FILENAME


def read_items(trial_dir: Path) -> list[GroundTruthItem]:
    path = items_path(trial_dir)
    if not path.is_file():
        return []
    return [GroundTruthItem.model_validate_json(line) for line in path.read_text().splitlines()]


@dataclass(frozen=True)
class Call:
    step_id: int
    function_name: str
    arguments: Any


def trajectory_calls(trial_dir: Path) -> dict[str, Call]:
    """Every tool call in Harbor's agent/trajectory.json, by tool_call_id."""
    path = TrialPaths(trial_dir).agent_dir / "trajectory.json"
    if not path.is_file():
        return {}
    found: dict[str, Call] = {}
    for step in json.loads(path.read_text()).get("steps", []):
        for call in step.get("tool_calls") or []:
            found[call["tool_call_id"]] = Call(
                step["step_id"], call["function_name"], call.get("arguments")
            )
    return found


def call_records(trial_dir: Path) -> list[CallRecord]:
    path = TrialPaths(trial_dir).agent_dir / CHECKPOINTS_DIRNAME / CALLS_FILENAME
    if not path.is_file():
        return []
    return [CallRecord.model_validate_json(line) for line in path.read_text().splitlines()]


@dataclass
class TrialTimeline:
    """Everything the replays say about one trial."""

    points: list[TimelinePoint]
    timelines: list[CheckTimeline]
    verdicts: dict[str, Classification]
    samples: dict[str, int]  # non-counterfactual replays with a verdict, per state
    records: list[ReplayRecord] = field(default_factory=list)
    final_failing: list[str] = field(default_factory=list)


def trial_timeline(trial_dir: Path) -> TrialTimeline:
    points = read_points(trial_dir)
    records = read_records(trial_dir)
    recorded = statuses(read_checks(TrialPaths(trial_dir).verifier_dir))
    timelines = check_timelines(points, records, recorded)
    verdicts = {t.key: classify(t) for t in timelines}
    samples = {state: len(found) for state, found in state_samples(records).items()}
    failing = [t.key for t in timelines if t.final not in (PASSED, None)]
    return TrialTimeline(points, timelines, verdicts, samples, records, failing)


def _samples(timeline: TrialTimeline, point: TimelinePoint) -> int:
    """Samples of a point's state; the final state also has the original verifier run."""
    return timeline.samples.get(point.state_id, 0) + (point.kind == "final")


def regression_repeats(timeline: TrialTimeline) -> dict[str, int]:
    """States around a regression that still need replays, and how many."""
    needed: dict[str, int] = {}
    for verdict in timeline.verdicts.values():
        if verdict.verdict != "regression" or verdict.point is None:
            continue
        assert verdict.last_pass is not None
        for index in (verdict.last_pass, verdict.point):
            point = timeline.points[index]
            missing = REPEAT_SAMPLES - _samples(timeline, point)
            if missing > 0:
                needed[point.state_id] = max(needed.get(point.state_id, 0), missing)
    return needed


def regressions(trial_dir: Path, timeline: TrialTimeline) -> dict[int, tuple[str, ...]]:
    """Confirmed-by-repeats regressions: first failing point -> checks, gate-2 exclusions out."""
    excluded = excluded_checks(trial_dir)
    by_point: dict[int, list[str]] = defaultdict(list)
    for key, verdict in timeline.verdicts.items():
        if verdict.verdict == "regression" and verdict.point is not None and key not in excluded:
            by_point[verdict.point].append(key)
    found = {}
    for index, checks in sorted(by_point.items()):
        before, point = timeline.points[index - 1], timeline.points[index]
        if min(_samples(timeline, before), _samples(timeline, point)) >= REPEAT_SAMPLES:
            found[index] = tuple(sorted(checks))
    return found


def _calls(points: list[TimelinePoint], indexes: list[int] | tuple[int, ...]) -> tuple[str, ...]:
    calls: list[str] = []
    for index in indexes:
        for call in points[index].covered_tool_call_ids:
            if call not in calls:
                calls.append(call)
    return tuple(calls)


def _steps(call_ids: tuple[str, ...], calls: dict[str, Call]) -> tuple[int, ...]:
    return tuple(sorted({calls[c].step_id for c in call_ids if c in calls}))


def _point_flags(
    points: list[TimelinePoint], indexes: list[int] | tuple[int, ...], calls: dict[str, Call]
) -> list[str]:
    flags = []
    for index in indexes:
        point = points[index]
        if len(point.covered_tool_call_ids) > 1:
            flags.append(f"covered_ambiguous:P{index}")
        if point.seq == 1:
            flags.append(f"baseline_checkpoint:P{index}")
        for call_id in point.covered_tool_call_ids:
            call = calls.get(call_id)
            command = (call.arguments or {}).get("command") if call else None
            if isinstance(command, str) and _RESTORE.search(command):
                flags.append(f"restore_like:{call_id}")
    return flags


def _check_flags(inputs: TrialInputs, checks: tuple[str, ...], has_points: bool) -> list[str]:
    task = traits(inputs.task_name)
    flags = []
    if task.message_blind:
        flags.append("message_blind")
    if set(checks) & task.timing_checks:
        flags.append("timing_check")
    if set(checks) & task.aggregate_checks:
        flags.append("aggregate_check")
    if set(checks) & task.self_test_checks:
        flags.append("self_test_check")
    if not has_points and not any(p.kind == "checkpoint" for p in read_points(inputs.trial_dir)):
        flags.append("no_checkpoints")
    return flags


def regression_items(
    inputs: TrialInputs, timeline: TrialTimeline, calls: dict[str, Call]
) -> list[GroundTruthItem]:
    trial_dir = inputs.trial_dir
    reverts = read_reverts(trial_dir)
    items = []
    for index, checks in regressions(trial_dir, timeline).items():
        point = timeline.points[index]
        blamed = [index] if point.kind == "checkpoint" else []
        call_ids = _calls(timeline.points, blamed)
        flags = _point_flags(timeline.points, blamed, calls)
        if point.kind != "checkpoint":
            flags.append("after_last_checkpoint")
        before = read_state(trial_dir, timeline.points[index - 1].state_id)
        if any(entry.status == "failed" for entry in before.entries):
            flags.append("prior_pass_vacuous")  # the passing state lacked a graded artifact
        revert = reverts.get((index, checks))
        flags.append(f"revert_{revert.verdict}" if revert else "revert_untested")
        flags += _check_flags(inputs, checks, bool(blamed))
        items.append(
            GroundTruthItem(
                item_id=f"{trial_dir.name}/regression-P{index}",
                trial_name=trial_dir.name,
                task_name=inputs.task_name,
                artifact_class=traits(inputs.task_name).artifact_class,
                checks=checks,
                kind="regression",
                method="timeline",
                points=tuple(blamed),
                tool_call_ids=call_ids,
                step_ids=_steps(call_ids, calls),
                revert_fix_id=revert.fix_id if revert else None,
                flags=tuple(flags),
            )
        )
    return items


def fix_confirms(
    fix: FixRecord,
    checks: tuple[str, ...],
    final_state: str,
    records: list[ReplayRecord],
    base: dict[str, str],
    required: int = 1,
) -> str | None:
    """Why a fix does not confirm these checks, or None if it does in every replay of it.

    Every claimed check must fail at the end of the trial; the fix's state needs `required`
    replays with a verdict and none without one (a fix whose state sometimes times out does not
    confirm anything), and every verdict replay must pass the claimed checks and break none.
    """
    if fix.replay_id is None or fix.state_id is None:
        return f"the fix produced no replay: {fix.error}"
    if fix.base_state_id != final_state:
        return "the fix does not start from the final state"
    not_failing = sorted(c for c in checks if base.get(c) in (None, PASSED))
    if not_failing:
        return f"claims checks that do not fail at the end: {not_failing}"
    replays = [r for r in records if r.state_id == fix.state_id and r.purpose == "counterfactual"]
    if any(r.outcome == "no_verdict" for r in replays):
        return "a replay of the fix's state ended without a verdict"
    samples = [r for r in replays if r.outcome == "verdict"]
    if len(samples) < required:
        return f"{len(samples)} of {required} confirming replays so far"
    for sample in samples:
        now = statuses(sample.checks)
        unfixed = sorted(c for c in checks if now.get(c) != PASSED)
        if unfixed:
            return f"replay {sample.replay_id} leaves {unfixed} failing"
        broken = sorted(c for c, s in base.items() if s == PASSED and now.get(c) != PASSED)
        if broken:
            return f"replay {sample.replay_id} breaks {broken}"
    return None


def _base_statuses(timeline: TrialTimeline) -> dict[str, str]:
    return {t.key: t.final for t in timeline.timelines if t.final and t.final != "flaky"}


@dataclass
class FixBlame:
    kind: ItemKind
    points: list[int]
    earliest: list[int]
    hunks: list[BlamedHunk]
    flags: list[str]
    size: int
    paths: list[str]


class HistoryCache:
    """File histories by path and fix contents by fix, each read from the images once."""

    def __init__(self, inputs: TrialInputs) -> None:
        self.inputs = inputs
        self._found: dict[str, list[tuple[TimelinePoint, list[str] | None]]] = {}
        self._contents: dict[str, dict[str, tuple[str | None, str | None]]] = {}

    def __call__(self, path: str) -> list[tuple[TimelinePoint, list[str] | None]]:
        if path not in self._found:
            self._found[path] = file_history(self.inputs, path)
        return self._found[path]

    def contents(self, fix: FixRecord) -> dict[str, tuple[str | None, str | None]]:
        if fix.fix_id not in self._contents:
            self._contents[fix.fix_id] = fix_contents(self.inputs, fix)
        return self._contents[fix.fix_id]


def _kind_of(points: list[TimelinePoint], origins: tuple[int, ...], replace: bool) -> HunkKind:
    by_agent = any(points[o].kind == "checkpoint" for o in origins)
    if replace:
        return "wrong_edit" if by_agent else "missed_fix"
    return "incomplete_edit" if by_agent else "omission"


def blame_fix(
    fix: FixRecord,
    points: list[TimelinePoint],
    history: HistoryCache,
    keep: set[int] | None = None,
) -> FixBlame:
    """Blame a fix's hunks; with `keep`, only the raw hunks it names."""
    inputs = history.inputs
    contents = history.contents(fix)
    order = hunk_index(contents)
    hunks: list[BlamedHunk] = []
    flags: list[str] = []
    size = 0
    for raw, (path, local) in enumerate(order):
        if keep is not None and raw not in keep:
            continue
        before, after = contents[path]
        container_path = f"/{path}"
        if local is None:  # the fix creates or deletes the whole file
            if before is None:
                kind: HunkKind = "missing_artifact" if in_artifacts(path, inputs) else "omission"
                added = len(split_lines(after or ""))
                hunks.append(BlamedHunk(path=container_path, kind=kind, raw_hunk=raw, added=added))
                size += added
                continue
            after = ""  # a deletion: every line is removed
        old = split_lines(before)  # type: ignore[arg-type]
        new = split_lines(after or "")
        versions = [(p.index, lines) for p, lines in history(container_path)]
        if not versions or versions[-1][1] is None:
            flags.append(f"no_history:{container_path}")
            continue
        if versions[-1][1] != old:
            flags.append(f"history_mismatch:{container_path}")
        view, _ = view_of(path, old)
        if view == "json" and len(old) <= JSON_VIEW_MAX_LINES:
            if local not in (None, 0):
                continue  # the file's view hunks were listed with its first raw hunk
            old = view_of(path, old)[1] or []
            new = view_of(path, new)[1] or []
            versions = [(i, view_of(path, lines)[1]) for i, lines in versions]
            local_hunks = raw_hunks(old, new)
        else:
            view = "lines"
            local_hunks = raw_hunks(
                split_keep(before),  # type: ignore[arg-type]
                split_keep(after or ""),
            )
            if local is not None:
                local_hunks = [local_hunks[local]]
        origins = blame(versions)
        seen = first_seen(versions)
        for i1, i2, j1, j2 in local_hunks:
            size += (i2 - i1) + (j2 - j1)
            if i2 > i1:
                removed = tuple(range(i1 + 1, min(i2, len(origins)) + 1))
                removed_origins = tuple(origins[line - 1] for line in removed)
                earliest = tuple(
                    seen.get(normalize(old[line - 1]), origin)
                    for line, origin in zip(removed, removed_origins, strict=True)
                )
                hunks.append(
                    BlamedHunk(
                        path=container_path,
                        view=view,
                        kind=_kind_of(points, removed_origins, replace=True),
                        raw_hunk=raw,
                        removed=removed,
                        origins=removed_origins,
                        earliest=earliest,
                        added=j2 - j1,
                    )
                )
            else:
                beside = anchors_of(i1, len(origins))
                beside_origins = tuple(origins[line - 1] for line in beside)
                hunks.append(
                    BlamedHunk(
                        path=container_path,
                        view=view,
                        kind=_kind_of(points, beside_origins, replace=False),
                        raw_hunk=raw,
                        inserted_before=i1 + 1,
                        anchors=beside,
                        anchor_origins=beside_origins,
                        added=j2 - j1,
                    )
                )
    kind: ItemKind = next((k for k in _KIND_ORDER if any(h.kind == k for h in hunks)), "omission")
    blamed: set[int] = set()
    earliest: set[int] = set()
    for hunk in hunks:
        if hunk.kind == "wrong_edit":
            blamed |= {o for o in hunk.origins if points[o].kind == "checkpoint"}
            earliest |= {
                e
                for e, o in zip(hunk.earliest, hunk.origins, strict=True)
                if points[o].kind == "checkpoint"
            }
        elif hunk.kind == "incomplete_edit" and kind == "incomplete_edit":
            blamed |= {o for o in hunk.anchor_origins if points[o].kind == "checkpoint"}
    if any(h.kind == "wrong_edit" for h in hunks) and any(h.kind == "missed_fix" for h in hunks):
        flags.append("also_initial_lines")
    if earliest and min(earliest) < min(blamed or earliest):
        flags.append("text_seen_earlier")
    if size > LARGE_FIX_LINES:
        flags.append("large_fix")
    paths = sorted({h.path for h in hunks})
    return FixBlame(kind, sorted(blamed), sorted(earliest), hunks, flags, size, paths)


ANCHOR_WINDOW = 3  # lines on each side of an insertion whose origins classify it


def anchors_of(insert_at: int, length: int) -> tuple[int, ...]:
    """Final-version lines (1-based) within `ANCHOR_WINDOW` of an insertion before `insert_at`.

    A window, not just the two neighbours, so that moving an insertion by a line or two does
    not change whether it is an incomplete edit of the agent's code or an omission next to
    initial code.
    """
    first = max(1, insert_at - ANCHOR_WINDOW + 1)
    last = min(length, insert_at + ANCHOR_WINDOW)
    return tuple(range(first, last + 1))


def related_calls(
    inputs: TrialInputs, paths: list[str], blamed: tuple[str, ...], records: list[CallRecord]
) -> tuple[str, ...]:
    """Calls that changed ungraded files in the directories of the fixed files' artifacts."""
    roots = artifact_roots(inputs)
    graded = [f"/{root}" for root in roots]
    near: set[str] = set()
    for path in paths:
        owner = next((g for g in graded if path == g or path.startswith(f"{g}/")), None)
        near.add(str(Path(owner or path).parent))
    found: list[str] = []
    for record in records:
        if record.tool_call_id in blamed or record.trigger != "tool_call":
            continue
        for change in record.changed_paths:
            changed = change[1:]
            if any(changed == g or changed.startswith(f"{g}/") for g in graded):
                continue
            if any(changed.startswith(f"{d.rstrip('/')}/") for d in near if d != "/"):
                found.append(record.tool_call_id)
                break
    return tuple(dict.fromkeys(found))[:MAX_RELATED_CALLS]


_CALL_ID = re.compile(r"\btoolu_[A-Za-z0-9]+")


def cited_calls(explanation: str, calls: dict[str, Call], blamed: tuple[str, ...]) -> list[str]:
    """Calls the labeler's explanation names besides the blamed ones (e.g. the call that wrote
    a generator whose output another call copied): not ground truth, but citing them is no
    false accusation."""
    return [
        c for c in dict.fromkeys(_CALL_ID.findall(explanation)) if c in calls and c not in blamed
    ]


def literal_flags(
    inputs: TrialInputs, contents: dict[str, tuple[str | None, str | None]]
) -> list[str]:
    """Flag a fix that adds literals found in the task's tests but not its instruction."""
    if inputs.task_dir is None:
        return []
    tests = "\n".join(
        p.read_text(errors="replace")
        for p in (inputs.task_dir / "tests").rglob("*")
        if p.is_file() and p.stat().st_size < 5_000_000
    )
    instruction = (inputs.task_dir / "instruction.md").read_text(errors="replace")
    copied = []
    for before, after in contents.values():
        added = set(split_lines(after or "")) - set(split_lines(before or ""))
        for line in added:
            for literal in _LITERAL.findall(line):
                if (
                    literal in tests
                    and literal not in instruction
                    and literal not in (before or "")
                ):
                    copied.append(literal)
    flags = []
    if copied:
        flags.append("test_literal")
        if traits(inputs.task_name).artifact_class == "output":
            flags.append("answer_substitution")
    return flags


def cause_items(
    inputs: TrialInputs,
    timeline: TrialTimeline,
    fixes: list[FixRecord],
    cause: LabelCause,
    number: int,
    calls: dict[str, Call],
    regression_list: list[GroundTruthItem],
    history: HistoryCache,
    minimized: dict[str, MinimizationRecord],
    excluded: set[str],
    records: list[CallRecord],
) -> list[GroundTruthItem]:
    trial_dir = inputs.trial_dir
    points = timeline.points
    checks = tuple(sorted(c for c in cause.checks if c not in excluded))
    if not checks:
        return []
    base = _base_statuses(timeline)
    final_state = points[-1].state_id
    required = QUIET_FIX_SAMPLES if traits(inputs.task_name).quiet else FIX_SAMPLES
    confirming = [
        f
        for f in fixes
        if fix_confirms(f, checks, final_state, timeline.records, base, required) is None
    ]
    common = {
        "trial_name": trial_dir.name,
        "task_name": inputs.task_name,
        "artifact_class": traits(inputs.task_name).artifact_class,
        "explanation": cause.explanation,
        "category": cause.category,
    }
    primary = next((f for f in confirming if f.fix_id == cause.fix_id), None)
    flags: list[str] = []
    if primary is None and confirming:
        primary = min(confirming, key=lambda f: sum(len(c.removed) + c.added for c in f.files))
        flags.append("primary_from_alternative")
    if primary is None:
        labeled = next((f for f in fixes if f.fix_id == cause.fix_id), None)
        problem = (
            "the labeler found no fix"
            if cause.fix_id is None
            else f"no fix {cause.fix_id} in fixes.jsonl"
            if labeled is None
            else fix_confirms(labeled, checks, final_state, timeline.records, base, required)
        )
        suspects = cause.suspected_tool_call_ids
        return [
            GroundTruthItem(
                **common,
                item_id=f"{trial_dir.name}/cause-{cause_key(checks)}",
                checks=checks,
                kind="unconfirmed",
                method="labeler",
                tool_call_ids=suspects,
                step_ids=_steps(suspects, calls),
                fix_id=cause.fix_id,
                flags=(f"not_confirmed: {problem}", *_check_flags(inputs, checks, False)),
            )
        ]
    # Which raw hunks each check needs, from the minimization record if there is one.
    groups: list[tuple[tuple[str, ...], set[int] | None]] = [(checks, None)]
    record = minimized.get((primary.fix_id, checks))
    if record is not None and any(lo.error for lo in record.leave_outs):
        flags.append("minimization_incomplete")  # some reduced try had no verdict: keep all
    elif record is not None and record.leave_outs:
        needs = {
            check: {lo.raw_hunk for lo in record.leave_outs if check in lo.needed_for}
            for check in checks
        }
        everything = set(range(record.raw_hunks))
        for check, needed in needs.items():
            if not needed:  # no single hunk is necessary: the hunks are redundant
                needs[check] = everything
                flags.append("redundant_hunks")
        groups = _components(needs)
        unneeded = everything - set().union(*needs.values())
        if unneeded:
            flags.append(f"unneeded_hunks:{sorted(unneeded)}")
    elif len(hunk_index(history.contents(primary))) > 1:
        flags.append("not_minimized")
    alternatives = []
    for other in confirming:
        if other.fix_id == primary.fix_id:
            continue
        alt = blame_fix(other, points, history)
        alternatives.append(
            AlternativeFix(
                fix_id=other.fix_id,
                kind=alt.kind,
                points=tuple(alt.points),
                tool_call_ids=_calls(points, alt.points),
                hunks=tuple(alt.hunks),
                fix_size=alt.size,
            )
        )
    copied = literal_flags(inputs, history.contents(primary))
    items = []
    for part, (group_checks, keep) in enumerate(groups, start=1):
        found = blame_fix(primary, points, history, keep)
        item_flags = flags + found.flags + copied + _point_flags(points, found.points, calls)
        item_flags += _check_flags(inputs, group_checks, bool(found.points))
        for regression in regression_list:
            if set(regression.checks) & set(group_checks):
                agree = set(regression.points) & set(found.points)
                item_flags.append(
                    "agrees_with_regression" if agree else "disagrees_with_regression"
                )
        call_ids = _calls(points, found.points)
        suffix = f".{part}" if len(groups) > 1 else ""
        items.append(
            GroundTruthItem(
                **common,
                item_id=f"{trial_dir.name}/cause-{cause_key(checks)}{suffix}",
                checks=group_checks,
                kind=found.kind,
                method="counterfactual",
                points=tuple(found.points),
                tool_call_ids=call_ids,
                step_ids=_steps(call_ids, calls),
                earliest_points=tuple(found.earliest),
                related_tool_call_ids=tuple(
                    dict.fromkeys(
                        [
                            *related_calls(inputs, found.paths, call_ids, records),
                            *cited_calls(cause.explanation, calls, call_ids),
                        ]
                    )
                ),
                hunks=tuple(found.hunks),
                fix_id=primary.fix_id,
                fix_size=found.size,
                alternatives=tuple(alternatives),
                replays=(primary.replay_id,) if primary.replay_id else (),
                flags=tuple(dict.fromkeys(item_flags)),
            )
        )
    return items


def cause_key(checks: tuple[str, ...]) -> str:
    """A cause's id within its trial, from its checks, so it survives relabeling in any order."""
    return hashlib.sha256("\n".join(sorted(checks)).encode()).hexdigest()[:10]


def _components(needs: dict[str, set[int]]) -> list[tuple[tuple[str, ...], set[int]]]:
    """Group checks that share a needed hunk; each group keeps the union of its hunks."""
    groups: list[tuple[set[str], set[int]]] = []
    for check, hunks in sorted(needs.items()):
        merged = [g for g in groups if g[1] & hunks]
        group_checks = {check}.union(*(g[0] for g in merged))
        group_hunks = set(hunks).union(*(g[1] for g in merged))
        groups = [g for g in groups if g not in merged] + [(group_checks, group_hunks)]
    return [(tuple(sorted(c)), h) for c, h in sorted(groups, key=lambda g: sorted(g[0]))]


def read_refutations(trial_dir: Path) -> dict[str, RefutationRecord]:
    """The latest refutation of each item."""
    path = groundtruth_dir(trial_dir) / REFUTATIONS_FILENAME
    if not path.is_file():
        return {}
    found = [RefutationRecord.model_validate_json(x) for x in path.read_text().splitlines()]
    return {r.item_id: r for r in found}


def _with_refutation(item: GroundTruthItem, refutation: RefutationRecord | None) -> GroundTruthItem:
    if refutation is None:
        return item
    flag = {"upheld": "upheld_by_refuter", "refuted": "refuted", "uncertain": "refuter_uncertain"}
    return item.model_copy(update={"flags": (*item.flags, flag[refutation.verdict])})


def build_items(inputs: TrialInputs) -> list[GroundTruthItem]:
    """Rewrite the trial's items.jsonl from its replays, fixes, and labels; return the items."""
    trial_dir = inputs.trial_dir
    timeline = trial_timeline(trial_dir)
    calls = trajectory_calls(trial_dir)
    items = regression_items(inputs, timeline, calls)
    regression_list = list(items)
    if labels_path(trial_dir).is_file():
        labels = TrialLabels.model_validate_json(labels_path(trial_dir).read_text())
        fixes = read_fixes(trial_dir)
        history = HistoryCache(inputs)
        minimized = read_minimized(trial_dir)
        excluded = excluded_checks(trial_dir)
        records = call_records(trial_dir)
        for number, cause in enumerate(labels.causes, start=1):
            items += cause_items(
                inputs,
                timeline,
                fixes,
                cause,
                number,
                calls,
                regression_list,
                history,
                minimized,
                excluded,
                records,
            )
    refutations = read_refutations(trial_dir)
    items = [_with_refutation(item, refutations.get(item.item_id)) for item in items]
    items_path(trial_dir).write_text("".join(item.model_dump_json() + "\n" for item in items))
    return items


def build_progress(inputs: TrialInputs) -> TrialProgress:
    """Rewrite the trial's progress.json: graded changes and first passes (classes 4 and 5)."""
    trial_dir = inputs.trial_dir
    timeline = trial_timeline(trial_dir)
    points = timeline.points
    changes = [
        GradedChange(point=p.index, seq=p.seq, tool_call_ids=p.covered_tool_call_ids)
        for previous, p in zip(points, points[1:], strict=False)
        if p.state_id != previous.state_id
    ]
    excluded = excluded_checks(trial_dir) | traits(inputs.task_name).self_test_checks
    first: list[FirstPass] = []
    unresolved: list[str] = []
    for key, verdict in sorted(timeline.verdicts.items()):
        if key in excluded:
            continue
        if verdict.verdict == "passes" and verdict.point is not None:
            if verdict.point == 0:
                continue  # passes from the initial state on: the agent did nothing for it
            point = points[verdict.point]
            first.append(
                FirstPass(
                    check=key,
                    point=point.index,
                    seq=point.seq,
                    tool_call_ids=point.covered_tool_call_ids,
                )
            )
        elif timeline.timelines and verdict.verdict in ("unstable", "incomplete"):
            final = next(t.final for t in timeline.timelines if t.key == key)
            if final == PASSED:
                unresolved.append(key)
    progress = TrialProgress(
        trial_name=trial_dir.name,
        graded_changes=tuple(changes),
        first_passes=tuple(first),
        unresolved=tuple(unresolved),
    )
    path = groundtruth_dir(trial_dir) / PROGRESS_FILENAME
    path.write_text(progress.model_dump_json(indent=2) + "\n")
    return progress
