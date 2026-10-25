"""Each check's status across a trial's timeline (ADR-0013, decision 4).

A point's status for a check comes from every replay of the point's state that has a verdict
(and, for the final state, the original verifier run too): `passed`, `failed`, another ctrf
status, `absent` (the check was not reported, e.g. its test module failed to import), `flaky`
(samples disagree), `unknown` (every replay of the state ended without a verdict), or None (the
state has not been replayed). A check with an unknown or unreplayed point is `incomplete`.
Only checks with a pass/fail status are tracked; CAD metric rows have none.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from trajlab.contracts.groundtruth import ReplayRecord, TimelinePoint
from trajlab.groundtruth.checks import PASSED, statuses

ABSENT = "absent"
FLAKY = "flaky"
UNKNOWN = "unknown"  # every replay of the state ended without a verdict

Verdict = Literal["regression", "never_passed", "unstable", "incomplete", "passes"]


@dataclass(frozen=True)
class CheckTimeline:
    key: str  # `<kind>:<check>`
    statuses: tuple[str | None, ...]  # one per timeline point, in order

    @property
    def final(self) -> str | None:
        return self.statuses[-1]


def state_samples(records: list[ReplayRecord]) -> dict[str, list[dict[str, str]]]:
    """Per state, the check statuses of each non-counterfactual replay with a verdict."""
    samples: dict[str, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        if record.purpose != "counterfactual" and record.outcome == "verdict":
            samples[record.state_id].append(statuses(record.checks))
    return samples


def no_verdict_states(records: list[ReplayRecord]) -> set[str]:
    """States whose test script ran without reporting checks (a timeout or a crash)."""
    return {
        r.state_id for r in records if r.purpose != "counterfactual" and r.outcome == "no_verdict"
    }


def check_timelines(
    points: list[TimelinePoint],
    records: list[ReplayRecord],
    recorded_final: dict[str, str],
) -> list[CheckTimeline]:
    samples = state_samples(records)
    unknown = no_verdict_states(records)
    final_id = points[-1].state_id
    per_state = {state_id: list(found) for state_id, found in samples.items()}
    per_state.setdefault(final_id, []).append(recorded_final)
    keys = sorted({key for found in per_state.values() for sample in found for key in sample})
    timelines = []
    for key in keys:
        row: list[str | None] = []
        for point in points:
            found = per_state.get(point.state_id) or []
            values = {sample.get(key, ABSENT) for sample in found}
            if not values:
                row.append(UNKNOWN if point.state_id in unknown else None)
            elif len(values) > 1:
                row.append(FLAKY)
            else:
                row.append(values.pop())
        timelines.append(CheckTimeline(key, tuple(row)))
    return timelines


@dataclass(frozen=True)
class Classification:
    verdict: Verdict
    # regression: the first point of the final failing run, after the last pass
    # passes: the first point from which the check passes through final
    point: int | None = None
    last_pass: int | None = None  # regression: the point before `point`


def classify(timeline: CheckTimeline) -> Classification:
    """What the timeline says about one check (decision 4)."""
    row = timeline.statuses
    if any(status is None or status == UNKNOWN for status in row):
        return Classification("incomplete")
    if row[-1] == PASSED:
        first = len(row) - 1
        while first > 0 and row[first - 1] == PASSED:
            first -= 1
        if first > 0 and row[first - 1] == FLAKY:  # the passing run may start earlier
            return Classification("unstable")
        return Classification("passes", point=first)
    passes = [index for index, status in enumerate(row) if status == PASSED]
    if not passes:
        return Classification("unstable" if FLAKY in row else "never_passed")
    last_pass = passes[-1]
    if FLAKY in row[last_pass:]:
        return Classification("unstable")
    return Classification("regression", point=last_pass + 1, last_pass=last_pass)
