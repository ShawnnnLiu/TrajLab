"""Per-check results from a verifier's structured output.

Moved from `scripts/2026-10-02_repair_report.py` (ADR-0013, decision 3): pytest results
(`ctrf.json`, kind `pytest`), API traces (`trace_results.json`, kind `trace`, vba-userform-port),
and CAD scores (`reward_details.json`, kind `cad`: a `score` row that passes when the score is 1,
and one value row per metric with no status).

ctrf drops pytest's parameter ids, so the rows of a parametrized test share one name. Such rows
are named from the short test summary in `test-stdout.txt` (`pytest -rA`), which lists full node
ids grouped by outcome, each group in run order: the k-th ctrf row of a name with status S is the
k-th S node of that name. If the two disagree, the rows are numbered `<name>#<k>` in ctrf order.
Every check name is unique within one verifier run.
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from trajlab.contracts.groundtruth import CheckKind, CheckResult

MESSAGE_CHARS = 300
PASSED = "passed"
FAILED = "failed"
# pytest's short-summary outcome words, by the ctrf status they report as.
SUMMARY_OUTCOMES = {
    "PASSED": "passed",
    "XPASS": "passed",
    "FAILED": "failed",
    "ERROR": "failed",
    "SKIPPED": "skipped",
    "XFAIL": "skipped",
}
_SUMMARY_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")


def _check(
    kind: CheckKind,
    name: str,
    status: str | None,
    *,
    value: float | None = None,
    message: str | None = None,
) -> CheckResult:
    return CheckResult(
        kind=kind,
        check=name,
        status=status,
        value=value,
        message=message[:MESSAGE_CHARS] if message else None,
    )


def _summary_nodes(stdout: Path) -> dict[str, list[str]]:
    """Node ids by ctrf status, in the summary's order, from pytest's short test summary."""
    nodes: dict[str, list[str]] = defaultdict(list)
    if not stdout.is_file():
        return nodes
    for line in stdout.read_text(errors="replace").splitlines():
        found = _SUMMARY_LINE.match(line)
        if found:
            nodes[SUMMARY_OUTCOMES[found.group(1)]].append(found.group(2))
    return nodes


def _same_test(node: str, name: str) -> bool:
    base = node.split("[", 1)[0]
    return base == name or base.endswith(f"/{name}")


def _parametrized_names(tests: list[dict], stdout: Path) -> list[str]:
    """One unique name per ctrf row (see the module docstring)."""
    names = [test["name"] for test in tests]
    repeated = {name for name, count in Counter(names).items() if count > 1}
    if not repeated:
        return names
    nodes = _summary_nodes(stdout)
    unique = list(names)
    for name in repeated:
        rows = [i for i, test in enumerate(tests) if test["name"] == name]
        pools = {
            status: [n for n in found if _same_test(n, name) and "[" in n]
            for status, found in nodes.items()
        }
        wanted = Counter(tests[i]["status"] for i in rows)
        if all(len(pools.get(status, [])) == count for status, count in wanted.items()):
            taken: Counter[str] = Counter()
            for i in rows:
                status = tests[i]["status"]
                node = pools[status][taken[status]]
                taken[status] += 1
                unique[i] = f"{name}[{node.split('[', 1)[1]}"
        else:
            for k, i in enumerate(rows, start=1):
                unique[i] = f"{name}#{k}"
    if len(set(unique)) != len(unique):  # the summary repeated a node id: fall back to ordinals
        seen: Counter[str] = Counter()
        for i, name in enumerate(names):
            if name in repeated:
                seen[name] += 1
                unique[i] = f"{name}#{seen[name]}"
    return unique


def read_checks(verifier_dir: Path) -> list[CheckResult]:
    """Every check in a verifier dir's structured output; empty if there is none."""
    found: list[CheckResult] = []
    ctrf = verifier_dir / "ctrf.json"
    if ctrf.is_file():
        tests = json.loads(ctrf.read_text())["results"]["tests"]
        names = _parametrized_names(tests, verifier_dir / "test-stdout.txt")
        for test, name in zip(tests, names, strict=True):
            found.append(_check("pytest", name, test["status"], message=test.get("message")))
    traces = verifier_dir / "trace_results.json"
    if traces.is_file():
        for trace in json.loads(traces.read_text()):
            status = PASSED if trace["passed"] else FAILED
            found.append(_check("trace", trace["trace_id"], status, message=trace.get("error")))
    details_path = verifier_dir / "reward_details.json"
    if details_path.is_file():
        details = json.loads(details_path.read_text())
        status = PASSED if details["score"] == 1.0 else FAILED
        found.append(_check("cad", "score", status, value=details["combined_raw"]))
        for part in ("base", "target"):
            for metric, value in (details.get(part) or {}).items():
                if isinstance(value, int | float):
                    reason = details[part].get(f"{metric}_reason")
                    found.append(
                        _check("cad", f"{part}.{metric}", None, value=float(value), message=reason)
                    )
    keys = [check_key(c) for c in found]
    duplicated = sorted(k for k, n in Counter(keys).items() if n > 1)
    if duplicated:
        raise ValueError(f"{verifier_dir}: checks named twice: {duplicated}")
    return found


def check_key(check: CheckResult) -> str:
    """A check's name within its trial: `<kind>:<check>`."""
    return f"{check.kind}:{check.check}"


def statuses(checks: list[CheckResult] | tuple[CheckResult, ...]) -> dict[str, str]:
    """The pass/fail status of every check that has one, by `check_key`."""
    return {check_key(c): c.status for c in checks if c.status is not None}
