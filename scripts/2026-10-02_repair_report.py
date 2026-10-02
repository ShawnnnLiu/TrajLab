"""Compare repair arms: completion, tests passed, agent time, and the repair's own tokens and cost.

Reads the source job (first attempts) and every `<prefix>-<task>-<arm>` repair job written by
`scripts/2026-10-02_repair_arms.py`. Prints a Markdown table, one row per trial, and writes the
rows as JSON next to the source job (`<source>/repair-report.json`).

Tokens count only the trial's own API calls: assistant messages whose `uuid` is not in the source
trial's native session (arms that load the failed trajectory carry its history in their session
file), deduplicated by API message id. Cost prices those tokens at Sonnet 5.5 list rates; Claude
Code's own `total_cost_usd` is shown alongside.

    uv run python scripts/2026-10-02_repair_report.py corpus/jobs/tb40-sonnet-v1 \
        --prefix tb40-repair-v1
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

JOBS_DIR = Path("corpus/jobs")
ARMS = ("fresh", "state", "state-traj", "traj")
# USD per million tokens, Claude Sonnet 5.5 list prices (claude-api skill, cached 2026-09-25).
PRICE = {"input": 2.00, "cache_write_5m": 2.50, "cache_write_1h": 4.00, "cache_read": 0.20}
PRICE_OUTPUT = 10.00
SUMMARY = re.compile(r"=+ (.*) in [\d.]+s")


def session_events(trial: Path) -> list[dict[str, Any]]:
    events = []
    for path in sorted((trial / "agent/sessions/projects").glob("*/*.jsonl")):
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


def seconds(phase: dict[str, Any] | None) -> float | None:
    if not phase or not phase.get("started_at") or not phase.get("finished_at"):
        return None
    start = datetime.fromisoformat(phase["started_at"])
    return round((datetime.fromisoformat(phase["finished_at"]) - start).total_seconds(), 1)


def row(trial: Path, arm: str, inherited: set[str]) -> dict[str, Any]:
    result = json.loads((trial / "result.json").read_text())
    verifier = result.get("verifier_result") or {}
    exception = result.get("exception_info") or {}
    reported = None
    log = trial / "agent/claude-code.txt"
    if log.exists():
        costs = re.findall(r'"total_cost_usd":([0-9.]+)', log.read_text())
        reported = float(costs[-1]) if costs else None
    return {
        "task": trial.name.split("__")[0],
        "arm": arm,
        "trial": trial.name,
        "reward": (verifier.get("rewards") or {}).get("reward"),
        "tests": tests(trial),
        "exception": exception.get("exception_type"),
        "agent_s": seconds(result.get("agent_execution")),
        "trial_s": seconds(result),
        **own_usage(trial, inherited),
        "cost_usd_reported": reported,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job", type=Path)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()

    rows = []
    for source in sorted(p for p in args.source_job.iterdir() if (p / "result.json").exists()):
        task = source.name.split("__")[0]
        rows.append(row(source, "original", set()))
        inherited = {e["uuid"] for e in session_events(source) if e.get("uuid")}
        for arm in ARMS:
            job = JOBS_DIR / f"{args.prefix}-{task}-{arm}"
            for trial in sorted(job.glob("*__*")) if job.exists() else []:
                if (trial / "result.json").exists():
                    rows.append(row(trial, arm, inherited))
    (args.source_job / "repair-report.json").write_text(json.dumps(rows, indent=2) + "\n")
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
        "cost_usd_priced",
        "cost_usd_reported",
    ]
    print("| " + " | ".join(columns) + " |")
    print("|" + "---|" * len(columns))
    for r in rows:
        print("| " + " | ".join("" if r[c] is None else str(r[c]) for c in columns) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
