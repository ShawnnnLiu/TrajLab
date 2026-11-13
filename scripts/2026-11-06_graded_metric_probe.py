"""Compare probe arms on the verifier's graded score, not only on the binary reward.

Built for the night run of 2026-11-06 (freecad-platform-drawing and vf2-speedup-networkx, each
10 reps x 3 arms on two local roots, planned and run by `scripts/2026-11-05_exp2_wide_run.py`).
Both tasks grade a number against a threshold, and no attempt or almost none passes, so the
binary reward says little; the number says how far each attempt got.

Reads, for every finished attempt of the plans given (a trial dir under `--jobs-dir/<name>/`
with result.json) and for each attempt's root trial dir:

    metric      `combined` from verifier/reward_details.json (freecad-platform-drawing:
                min(geometry_similarity, cad_spec_consistency), pass at 0.9), else the
                `Geomean speedup: <x>x` line of verifier/test-stdout.txt (vf2-speedup-networkx:
                pass at 5000x); None when neither is there
    failing     names of the verifier's failing tests (verifier/ctrf.json), when it has one
    calls       tool calls in agent/trajectory.json
    agent_min   agent execution minutes from result.json

With `--record-info <jsonl>` (the output of `scripts/2026-11-06_record_info_candidates.py
plans`), each attempt also gets `record_lines`: written lines of 20 or more characters that
the attempt read from the record before writing them and that the root added.
With `--artifact <path>` (a path under the trial's `artifacts/`, Harbor's copy of the graded
files at the end, such as `app/fast_networkx/_core.c`), each attempt also gets `root_kept`: the
share of the root's distinct stripped lines of 20 or more characters in that file that the
attempt's final file also has, `same_as_root` when the two files are byte-identical, and
`root_changed_lines`: lines replaced, inserted, or deleted in a `difflib.SequenceMatcher`
alignment of the root's file with the attempt's (the larger side of each changed block).

Prints one JSON object: `attempts` (one row each, with the root's metric and `vs_root`, the
attempt's metric over the root's for a speedup and minus it for a combined score), `roots`,
and per task `arms` (count, passes, median, min and max metric, attempts above their root,
median calls and agent minutes) and `tests`.
Each test compares two groups of arms on the metric with an exact rank test stratified by
root: within each root the attempts are ranked (ties get mid-ranks), the statistic is the sum
over roots of the first group's rank sum, and its null distribution is the convolution over
roots of the rank sums of every equally sized subset (all assignments within a root equally
likely). The p-value is two-sided: the probability of a statistic at least as far from its
mean as the observed one. The same test is run on tool calls and agent minutes.
Passes get Fisher's exact test (two-sided), unstratified.
No randomness.

    uv run python scripts/2026-11-06_graded_metric_probe.py \\
        corpus/exp2/probe-jobs/_plans/night-vf.jsonl \\
        --jobs-dir corpus/exp2/probe-jobs \\
        --record-info corpus/exp2/probe-jobs/night-vf-record-info.jsonl \\
        --artifact app/fast_networkx/_core.c
"""

import argparse
import difflib
import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from math import comb, exp, log
from pathlib import Path
from typing import Any

from harbor.models.trajectories import Trajectory
from harbor.models.trial.result import TrialResult

logger = logging.getLogger("graded_metric_probe")

GEOMEAN = re.compile(r"Geomean speedup:\s*([\d.]+)x")
ARM_GROUPS = {
    "record-engine vs control": (["record-engine"], ["control"]),
    "record-files vs control": (["record-files"], ["control"]),
    "record arms vs control": (["record-engine", "record-files"], ["control"]),
    "record-engine vs record-files": (["record-engine"], ["record-files"]),
}


def trial_dir(job: Path) -> Path | None:
    for trial in sorted(job.iterdir()) if job.is_dir() else []:
        if trial.is_dir() and (trial / "result.json").is_file():
            return trial
    return None


def metric(trial: Path) -> tuple[str | None, float | None]:
    details = trial / "verifier" / "reward_details.json"
    if details.is_file():
        value = json.loads(details.read_text()).get("combined")
        if value is not None:
            return "combined", float(value)
    stdout = trial / "verifier" / "test-stdout.txt"
    if stdout.is_file():
        found = GEOMEAN.search(stdout.read_text(errors="replace"))
        if found:
            return "geomean_speedup", float(found.group(1))
    return None, None


def failing(trial: Path) -> list[str] | None:
    ctrf = trial / "verifier" / "ctrf.json"
    if not ctrf.is_file():
        return None
    tests = json.loads(ctrf.read_text())["results"]["tests"]
    return [t["name"].split("::")[-1] for t in tests if t["status"] != "passed"]


def describe(trial: Path) -> dict[str, Any]:
    result = TrialResult.model_validate_json((trial / "result.json").read_text())
    rewards = (result.verifier_result.rewards or {}) if result.verifier_result else {}
    execution = result.agent_execution
    minutes = None
    if execution and execution.started_at and execution.finished_at:
        minutes = round((execution.finished_at - execution.started_at).total_seconds() / 60, 1)
    trajectory = Trajectory.model_validate_json((trial / "agent" / "trajectory.json").read_text())
    kind, value = metric(trial)
    return {
        "trial": trial.name,
        "reward": rewards.get("reward"),
        "exception": result.exception_info.exception_type if result.exception_info else None,
        "metric_kind": kind,
        "metric": value,
        "failing": failing(trial),
        "calls": sum(len(step.tool_calls or []) for step in trajectory.steps),
        "agent_min": minutes,
    }


MIN_LINE = 20


def artifact_lines(trial: Path, artifact: str) -> tuple[bytes, set[str]] | None:
    path = trial / "artifacts" / artifact
    if not path.is_file():
        return None
    data = path.read_bytes()
    text = data.decode(errors="replace")
    return data, {line.strip() for line in text.splitlines() if len(line.strip()) >= MIN_LINE}


def changed_lines(before: bytes, after: bytes) -> int:
    a = before.decode(errors="replace").splitlines()
    b = after.decode(errors="replace").splitlines()
    opcodes = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in opcodes if tag != "equal")


def vs_root(kind: str | None, value: float | None, root: float | None) -> float | None:
    if value is None or root is None:
        return None
    if kind == "geomean_speedup":
        return round(value / root, 3)
    return round(value - root, 4)


def mid_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def subset_sums(weights: list[int], size: int) -> Counter[int]:
    """How many subsets of `size` of `weights` have each sum."""
    table: list[Counter[int]] = [Counter() for _ in range(size + 1)]
    table[0][0] = 1
    for w in weights:
        for k in range(size, 0, -1):
            for total, n in table[k - 1].items():
                table[k][total + w] += n
    return table[size]


def stratified_rank_test(strata: list[tuple[list[float], list[float]]]) -> float | None:
    """Two-sided exact p of the first group's rank sum, ranks within each stratum."""
    distribution: Counter[int] = Counter({0: 1})
    observed = 0
    for first, second in strata:
        if not first or not second:
            continue
        doubled = [round(2 * r) for r in mid_ranks(first + second)]
        observed += sum(doubled[: len(first)])
        sums = subset_sums(doubled, len(first))
        merged: Counter[int] = Counter()
        for a, n in distribution.items():
            for b, m in sums.items():
                merged[a + b] += n * m
        distribution = merged
    total = sum(distribution.values())
    if total <= 1:
        return None
    mean = sum(s * n for s, n in distribution.items()) / total
    far = abs(observed - mean) - 1e-9
    p = sum(n for s, n in distribution.items() if abs(s - mean) >= far) / total
    return float(f"{p:.3g}")


def fisher(a: int, n1: int, c: int, n2: int) -> float:
    """Two-sided Fisher exact p of a passes in n1 against c passes in n2."""
    passed, total = a + c, n1 + n2

    def prob(x: int) -> float:
        return comb(n1, x) * comb(n2, passed - x) / comb(total, passed)

    observed = prob(a)
    lo, hi = max(0, passed - n2), min(n1, passed)
    return round(
        min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= observed * 1.000001)), 4
    )


def by_root(
    a: list[dict[str, Any]], b: list[dict[str, Any]], key: str
) -> list[tuple[list[float], list[float]]]:
    """The two groups' values of `key`, split by root, for the stratified rank test."""
    return [
        (
            [r[key] for r in a if r["root"] == root and r[key] is not None],
            [r[key] for r in b if r["root"] == root and r[key] is not None],
        )
        for root in sorted({r["root"] for r in a + b})
    ]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [r["metric"] for r in rows if r["metric"] is not None]
    above = [r for r in rows if r["vs_root"] is not None]
    threshold = 1.0 if rows and rows[0]["metric_kind"] == "geomean_speedup" else 0.0
    out: dict[str, Any] = {
        "attempts": len(rows),
        "passes": sum(int(r["reward"] or 0) for r in rows),
        "metric_median": round(statistics.median(values), 4) if values else None,
        "metric_min": min(values, default=None),
        "metric_max": max(values, default=None),
        "above_root": sum(r["vs_root"] > threshold for r in above),
        "median_calls": statistics.median(r["calls"] for r in rows),
        "median_agent_min": statistics.median(
            r["agent_min"] for r in rows if r["agent_min"] is not None
        ),
    }
    if rows and rows[0]["metric_kind"] == "geomean_speedup" and values:
        out["metric_geomean"] = round(exp(statistics.fmean(log(v) for v in values)), 1)
    if any("record_lines" in r for r in rows):
        out["reused_root_lines"] = sum((r.get("record_lines") or 0) > 0 for r in rows)
    kept = [r["root_kept"] for r in rows if r.get("root_kept") is not None]
    if kept:
        out["median_root_kept"] = round(statistics.median(kept), 3)
        out["kept_half_of_root"] = sum(k >= 0.5 for k in kept)
        out["same_as_root"] = sum(bool(r.get("same_as_root")) for r in rows)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("plans", nargs="+", type=Path)
    parser.add_argument("--jobs-dir", type=Path, default=Path("corpus/exp2/probe-jobs"))
    parser.add_argument("--record-info", type=Path, action="append", default=[])
    parser.add_argument("--artifact")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    reused: dict[str, int] = {}
    for path in args.record_info:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "attempt":
                reused[row["trial"]] = row.get("record_lines", 0)

    roots: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    for plan in args.plans:
        for line in plan.read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            trial = trial_dir(args.jobs_dir / item["name"])
            if trial is None:
                logger.info("%s: not finished, skipped", item["name"])
                continue
            root_path = Path(item["root"])
            if root_path.name not in roots:
                roots[root_path.name] = describe(root_path)
            root = roots[root_path.name]
            row = {"name": item["name"], "task": item["task"], "arm": item["arm"]}
            row |= {"root": root_path.name, **describe(trial)}
            row["root_metric"] = root["metric"]
            row["vs_root"] = vs_root(row["metric_kind"], row["metric"], root["metric"])
            if trial.name in reused:
                row["record_lines"] = reused[trial.name]
            if args.artifact:
                mine = artifact_lines(trial, args.artifact)
                theirs = artifact_lines(root_path, args.artifact)
                if mine and theirs and theirs[1]:
                    row["root_kept"] = round(len(mine[1] & theirs[1]) / len(theirs[1]), 3)
                    row["same_as_root"] = mine[0] == theirs[0]
                    row["root_changed_lines"] = changed_lines(theirs[0], mine[0])
            attempts.append(row)

    counted = [r for r in attempts if r["exception"] is None]
    out: dict[str, Any] = {
        "excluded": [f"{r['name']} ({r['exception']})" for r in attempts if r["exception"]],
        "roots": list(roots.values()),
        "attempts": attempts,
        "tasks": {},
    }
    for task in sorted({r["task"] for r in counted}):
        rows = [r for r in counted if r["task"] == task]
        by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_arm[r["arm"]].append(r)
        tests = []
        for label, (first, second) in ARM_GROUPS.items():
            a = [r for r in rows if r["arm"] in first and r["metric"] is not None]
            b = [r for r in rows if r["arm"] in second and r["metric"] is not None]
            if not a or not b:
                continue
            pa, pb = sum(int(r["reward"] or 0) for r in a), sum(int(r["reward"] or 0) for r in b)
            tests.append(
                {
                    "test": label,
                    "n": [len(a), len(b)],
                    "metric_rank_p": stratified_rank_test(by_root(a, b, "metric")),
                    "calls_rank_p": stratified_rank_test(by_root(a, b, "calls")),
                    "agent_min_rank_p": stratified_rank_test(by_root(a, b, "agent_min")),
                    "passes": [pa, pb],
                    "passes_fisher_p": fisher(pa, len(a), pb, len(b)),
                }
            )
        out["tasks"][task] = {
            "arms": {arm: summarize(rs) for arm, rs in sorted(by_arm.items())},
            "tests": tests,
        }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
