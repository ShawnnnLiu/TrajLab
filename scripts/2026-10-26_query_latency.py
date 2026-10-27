"""Time a fixed set of representative queries through the query tool's runner (ADR-0019).

Every query goes through `trajlab.store.query.run_query` exactly as a served request does: a
child process per query, the statement check, the cap (200 by default), and the answer's JSON
encoding; the child's start is in every number. The set is one query per table and per view,
a join of steps and deltas, a grep-like LIKE over the hunks, and the prompt-named paths' last
hunks. Each is run `--repeat` times after one untimed warm-up of the interpreter's file cache,
except the very first, whose first run is reported as the cold start.

Prints a JSON object: per query the SQL, rows, whether it was cut, and median and p95 latency in
ms over the repeats; `cold_start_ms`; and the host and DuckDB versions. With `--markdown`, the
table for a note instead. Read-only: the store is opened read-only by every child.

    uv run python scripts/2026-10-26_query_latency.py <store> [--repeat 20] [--cap 200]
        [--markdown]

The store must be a schema-2 store (`trajlab store derive` upgrades one). The 2026-10-26 numbers
are from a copy of corpus/stores/exp2-example-v2 (production-planning, 5 trials); run it on the
tb40-sonnet-v3 store with that one argument once it is built.
"""

import argparse
import json
import platform
import statistics
import sys
from pathlib import Path
from typing import Any

import duckdb

from trajlab.store.query import DEFAULT_ROW_CAP, run_query

QUERIES: dict[str, str] = {
    "trials": "SELECT trial_name, reward, cost_usd, wall_ms FROM trials ORDER BY trial_name",
    "steps": (
        "SELECT trial_id, step_idx, tool, is_error, length(output) AS out "
        "FROM steps ORDER BY trial_id, step_idx"
    ),
    "deltas": "SELECT trial_id, step_idx, path, op, bytes_after FROM deltas ORDER BY ALL",
    "step_flags": (
        "SELECT * FROM step_flags WHERE is_mutating OR has_error_marker ORDER BY trial_id, step_idx"
    ),
    "delta_flags": (
        "SELECT trial_id, step_idx, path, version, is_revert, variant_of "
        "FROM delta_flags WHERE NOT is_cache ORDER BY trial_id, step_idx, path"
    ),
    "path_versions": (
        "SELECT trial_id, path, versions, steps FROM path_versions "
        "WHERE NOT is_cache ORDER BY versions DESC, trial_id, path LIMIT 50"
    ),
    "trial_summary": "SELECT * FROM trial_summary ORDER BY trial_id",
    "join": (
        "SELECT s.trial_id, s.step_idx, s.tool, d.path, d.op FROM steps s "
        "JOIN deltas d USING (trial_id, step_idx) ORDER BY s.trial_id, s.step_idx, d.path"
    ),
    "like_hunks": (
        "SELECT trial_id, step_idx, path FROM deltas "
        "WHERE hunk LIKE '%Error%' OR hunk LIKE '%Traceback%' ORDER BY ALL"
    ),
    "last_hunks": (
        "SELECT trial_id, path, last_step, length(last_hunk) AS hunk_len FROM path_versions "
        "WHERE names_prompt_path ORDER BY trial_id, path"
    ),
}


def time_query(store: Path, sql: str, *, cap: int, repeat: int) -> dict[str, Any]:
    latencies: list[int] = []
    rows = truncated = None
    for _ in range(repeat):
        outcome = run_query(store, sql, cap=cap)
        if outcome.error is not None:
            raise SystemExit(f"{sql}\n-> {outcome.error}")
        latencies.append(outcome.latency_ms)
        rows, truncated = len(outcome.rows or []), outcome.truncated
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, round(0.95 * len(latencies)) - 1)]
    return {
        "sql": sql,
        "rows": rows,
        "truncated": truncated,
        "median_ms": statistics.median(latencies),
        "p95_ms": p95,
        "min_ms": latencies[0],
        "max_ms": latencies[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("store", type=Path)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--cap", type=int, default=DEFAULT_ROW_CAP)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    first = next(iter(QUERIES.values()))
    cold = run_query(args.store, first, cap=args.cap)
    if cold.error is not None:
        raise SystemExit(f"{args.store}: {cold.error}")
    report: dict[str, Any] = {
        "store": str(args.store),
        "repeat": args.repeat,
        "cap": args.cap,
        "cold_start_ms": cold.latency_ms,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "queries": {
            name: time_query(args.store, sql, cap=args.cap, repeat=args.repeat)
            for name, sql in QUERIES.items()
        },
    }
    if args.markdown:
        print(f"Cold start (first query, child start included): {cold.latency_ms} ms.")
        print()
        print("| Query | Rows | Cut | Median ms | p95 ms |")
        print("|---|---|---|---|---|")
        for name, q in report["queries"].items():
            cut = "yes" if q["truncated"] else "no"
            print(f"| `{name}` | {q['rows']} | {cut} | {q['median_ms']:.0f} | {q['p95_ms']} |")
    else:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
