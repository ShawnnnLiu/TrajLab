"""Compare repair arms: completion, tests passed, agent time, and the repair's own tokens and cost.

Reads the source job (first attempts) and every repair job of its trials: `<prefix>-<trial>-<arm>`
as `trajlab repair` names them (ADR-0012), else `<prefix>-<task>-<arm>` as the pilot's
`scripts/2026-10-02_repair_arms.py` did. Prints a Markdown table, one row per trial, and writes the
rows as JSON next to the source job (`<source>/repair-report.json`).

Also writes one row per (trial, verifier check) to `<source>/repair-checks.json`, from the
verifier's structured output: pytest results (`ctrf.json`, kind `pytest`), API traces
(`trace_results.json`, kind `trace`), and CAD scores (`reward_details.json`, kind `cad`: one row
per metric with its `value`, and a `score` row that passes when the score is 1). Each repair row
carries `source_status`, the status of the same check in its source trial, so a notebook can tell
which failures a repair inherited. Trials with no verifier output have no rows.

Tokens count only the trial's own API calls: assistant messages whose `uuid` is not in the source
trial's native session (arms that load the failed trajectory carry its history in their session
file), deduplicated by API message id. Cost prices those tokens at Sonnet 5.5 list rates; Claude
Code's own `total_cost_usd` is shown alongside. `cost_usd_own` is the priced cost, or Claude Code's
reported cost for a trial whose session files cannot be read and that resumed no session (where the
reported cost is the trial's own).

    uv run python scripts/2026-10-02_repair_report.py corpus/jobs/tb40-sonnet-v1 \
        --prefix tb40-repair-v1
    uv run python scripts/2026-10-02_repair_report.py corpus/jobs/tb40-sonnet-v2 \
        --prefix tb40-repair-v2
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

JOBS_DIR = Path("corpus/jobs")
ARMS = ("fresh", "state", "state-traj", "traj")
RESUMED_ARMS = ("state-traj", "traj")
# USD per million tokens, Claude Sonnet 5.5 list prices (claude-api skill, cached 2026-09-25).
PRICE = {"input": 2.00, "cache_write_5m": 2.50, "cache_write_1h": 4.00, "cache_read": 0.20}
PRICE_OUTPUT = 10.00
SUMMARY = re.compile(r"=+ (.*) in [\d.]+s")
MESSAGE_CHARS = 300


def session_paths(trial: Path) -> list[Path]:
    return sorted((trial / "agent/sessions/projects").glob("*/*.jsonl"))


def unreadable(trial: Path) -> list[Path]:
    return [path for path in session_paths(trial) if not os.access(path, os.R_OK)]


def session_events(trial: Path) -> list[dict[str, Any]]:
    events = []
    for path in session_paths(trial):
        if not os.access(path, os.R_OK):
            print(f"warning: cannot read {path}; its tokens are not counted", file=sys.stderr)
            continue
        events += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return events


def own_usage(trial: Path, inherited: set[str]) -> dict[str, Any]:
    usage_by_message: dict[str, dict[str, Any]] = {}
    tool_calls = 0
    for event in session_events(trial):
        if event.get("uuid") in inherited or event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_calls += 1
        if message.get("id") and message.get("usage"):
            usage_by_message[message["id"]] = message["usage"]
    totals = {"input": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 0}
    for usage in usage_by_message.values():
        totals["input"] += usage.get("input_tokens", 0)
        totals["cache_read"] += usage.get("cache_read_input_tokens", 0)
        totals["output"] += usage.get("output_tokens", 0)
        split = usage.get("cache_creation") or {}
        if split:
            totals["cache_write_5m"] += split.get("ephemeral_5m_input_tokens", 0)
            totals["cache_write_1h"] += split.get("ephemeral_1h_input_tokens", 0)
        else:
            totals["cache_write_5m"] += usage.get("cache_creation_input_tokens", 0)
    cost = sum(totals[k] * PRICE[k] for k in PRICE) + totals["output"] * PRICE_OUTPUT
    return {
        "api_calls": len(usage_by_message),
        "tool_calls": tool_calls,
        **{f"tok_{k}": v for k, v in totals.items()},
        "cost_usd_priced": round(cost / 1e6, 4),
    }


def tests(trial: Path) -> str | None:
    path = trial / "verifier/test-stdout.txt"
    if not path.exists():
        return None
    text = path.read_text()
    found = SUMMARY.findall(text)
    if found:
        return found[-1]
    # Some verifiers print one `FAIL [rule] ...` line per violation instead of a pytest summary.
    violations = sum(1 for line in text.splitlines() if line.strip().startswith("FAIL "))
    return f"{violations} violations" if violations else None


def check(kind: str, name: str, status: str | None, **extra: Any) -> dict[str, Any]:
    message = extra.pop("message", None)
    return {
        "kind": kind,
        "check": name,
        "status": status,
        "value": extra.pop("value", None),
        "message": message[:MESSAGE_CHARS] if message else None,
    }


def checks(trial: Path) -> list[dict[str, Any]]:
    verifier = trial / "verifier"
    found = []
    if (verifier / "ctrf.json").exists():
        for test in json.loads((verifier / "ctrf.json").read_text())["results"]["tests"]:
            found.append(check("pytest", test["name"], test["status"], message=test.get("message")))
    if (verifier / "trace_results.json").exists():
        for trace in json.loads((verifier / "trace_results.json").read_text()):
            status = "passed" if trace["passed"] else "failed"
            found.append(check("trace", trace["trace_id"], status, message=trace.get("error")))
    if (verifier / "reward_details.json").exists():
        details = json.loads((verifier / "reward_details.json").read_text())
        status = "passed" if details["score"] == 1.0 else "failed"
        found.append(check("cad", "score", status, value=details["combined_raw"]))
        for part in ("base", "target"):
            for metric, value in (details.get(part) or {}).items():
                if isinstance(value, int | float):
                    reason = details[part].get(f"{metric}_reason")
                    found.append(
                        check("cad", f"{part}.{metric}", None, value=value, message=reason)
                    )
    return found


def check_rows(
    trial: Path, arm: str, source: str, source_status: dict[tuple[str, str], str | None]
) -> list[dict[str, Any]]:
    head = {"task": trial.name.split("__")[0], "arm": arm, "trial": trial.name, "source": source}
    return [
        {**head, **c, "source_status": source_status.get((c["kind"], c["check"]))}
        for c in checks(trial)
    ]


def seconds(phase: dict[str, Any] | None) -> float | None:
    if not phase or not phase.get("started_at") or not phase.get("finished_at"):
        return None
    start = datetime.fromisoformat(phase["started_at"])
    return round((datetime.fromisoformat(phase["finished_at"]) - start).total_seconds(), 1)


def row(trial: Path, arm: str, source: str, inherited: set[str]) -> dict[str, Any]:
    result = json.loads((trial / "result.json").read_text())
    verifier = result.get("verifier_result") or {}
    exception = result.get("exception_info") or {}
    reported = None
    log = trial / "agent/claude-code.txt"
    if log.exists():
        costs = re.findall(r'"total_cost_usd":([0-9.]+)', log.read_text())
        reported = float(costs[-1]) if costs else None
    usage = own_usage(trial, inherited)
    sessions_unreadable = len(unreadable(trial))
    own = usage["cost_usd_priced"]
    if sessions_unreadable and arm not in RESUMED_ARMS and reported is not None:
        own = reported
    return {
        "task": trial.name.split("__")[0],
        "arm": arm,
        "trial": trial.name,
        "source": source,
        "reward": (verifier.get("rewards") or {}).get("reward"),
        "tests": tests(trial),
        "exception": exception.get("exception_type"),
        "agent_s": seconds(result.get("agent_execution")),
        "trial_s": seconds(result),
        **usage,
        "cost_usd_reported": reported,
        "cost_usd_own": own,
        "sessions_unreadable": sessions_unreadable,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job", type=Path)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()

    rows = []
    check_table = []
    for source in sorted(p for p in args.source_job.iterdir() if (p / "result.json").exists()):
        task = source.name.split("__")[0]
        rows.append(row(source, "original", source.name, set()))
        source_status = {(c["kind"], c["check"]): c["status"] for c in checks(source)}
        check_table += check_rows(source, "original", source.name, source_status)
        inherited = {e["uuid"] for e in session_events(source) if e.get("uuid")}
        for arm in ARMS:
            job = JOBS_DIR / f"{args.prefix}-{source.name}-{arm}"
            if not job.exists():
                job = JOBS_DIR / f"{args.prefix}-{task}-{arm}"
            for trial in sorted(job.glob("*__*")) if job.exists() else []:
                if (trial / "result.json").exists():
                    rows.append(row(trial, arm, source.name, inherited))
                    check_table += check_rows(trial, arm, source.name, source_status)
    (args.source_job / "repair-report.json").write_text(json.dumps(rows, indent=2) + "\n")
    (args.source_job / "repair-checks.json").write_text(json.dumps(check_table, indent=2) + "\n")
    columns = [
        "task",
        "arm",
        "reward",
        "tests",
        "agent_s",
        "api_calls",
        "tool_calls",
        "tok_output",
        "tok_cache_read",
        "cost_usd_own",
        "cost_usd_reported",
    ]
    print("| " + " | ".join(columns) + " |")
    print("|" + "---|" * len(columns))
    for r in rows:
        print("| " + " | ".join("" if r[c] is None else str(r[c]) for c in columns) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
