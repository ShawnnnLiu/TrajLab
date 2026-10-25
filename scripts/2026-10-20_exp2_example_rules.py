"""Run the candidate debugging rules of Exp 2 as SQL over a store and print the counts per trial.

Reads `record.duckdb` of a store built by `trajlab store build` (read-only) and prints one JSON
object to stdout: `rules`, the definition of each rule as the SQL that computes it, and
`trials`, one entry per trial with the count each rule gives, plus the most-rewritten paths.
The rules are the technical spec's three derived columns (§3.3) and the candidates the example
runs suggested (`docs/experiments/exp2/2026-10-19_example-runs.md`); step 3's `derive.py` takes
its definitions from here, and this script is not run again once that exists.

Fields per trial:
- `tool_calls`: steps that are tool calls (not the final message, not stop records);
- `mutating`: tool calls with at least one delta row; `probes`: tool calls with none;
- `edits_after_probe`: mutating calls whose previous tool call was a probe;
- `loops`: same tool and args as an earlier step of the trial, and no delta rows (the spec's
  `is_loop`); `loops_same_output`: the same with an identical output too;
- `reverts`: delta rows whose later version equals the earlier version of some earlier row on the
  same path (the spec's `is_revert`, at the row level);
- `retries`: tool calls with the same tool and args as an errored call among the 3 tool calls
  before them;
- `rejected`: tool calls Claude Code refused (`<tool_use_error>`);
- `cache_rows` / `delta_rows`: delta rows under `__pycache__`, and all delta rows;
- `idle_waits`: tool calls that took over 120 s and changed nothing (waiting on a background
  job); `idle_s`: their seconds in total;
- `scripts_added`: distinct `.py` files the agent created outside `__pycache__`;
- `prompt_path_versions`: versions (delta rows) of the paths the task instruction names, the
  deliverables; `prompt_paths` lists them;
- `churn`: the paths with the most versions (delta rows), with their step indexes.

    uv run python scripts/2026-10-20_exp2_example_rules.py corpus/stores/exp2-example-v1
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from trajlab.store import schema

logger = logging.getLogger("exp2_example_rules")

TOOL_CALLS = "tool IS NOT NULL AND tool NOT IN ('Stop', 'StopFailure')"
# An absolute path in the task instruction. Its last character is not a period, so a path that
# ends a sentence ("... to /app/plan.json.") is still the path.
PROMPT_PATH = "(/[A-Za-z0-9_./-]*[A-Za-z0-9_/-])"

RULES: dict[str, str] = {
    "tool_calls": f"SELECT trial_id, count(*) AS n FROM steps WHERE {TOOL_CALLS} GROUP BY 1",
    "mutating": f"""
        SELECT s.trial_id, count(*) AS n FROM steps s
        WHERE {TOOL_CALLS} AND EXISTS (
            SELECT 1 FROM deltas d WHERE d.trial_id = s.trial_id AND d.step_idx = s.step_idx)
        GROUP BY 1""",
    "probes": f"""
        SELECT s.trial_id, count(*) AS n FROM steps s
        WHERE {TOOL_CALLS} AND NOT EXISTS (
            SELECT 1 FROM deltas d WHERE d.trial_id = s.trial_id AND d.step_idx = s.step_idx)
        GROUP BY 1""",
    "edits_after_probe": f"""
        WITH calls AS (
            SELECT s.trial_id, s.step_idx, EXISTS (
                SELECT 1 FROM deltas d WHERE d.trial_id = s.trial_id AND d.step_idx = s.step_idx
            ) AS mutating FROM steps s WHERE {TOOL_CALLS}),
        ordered AS (
            SELECT *, lag(mutating) OVER (PARTITION BY trial_id ORDER BY step_idx) AS prev
            FROM calls)
        SELECT trial_id, count(*) AS n FROM ordered WHERE mutating AND NOT prev GROUP BY 1""",
    "loops": f"""
        SELECT s.trial_id, count(*) AS n FROM steps s
        WHERE {TOOL_CALLS}
          AND EXISTS (SELECT 1 FROM steps e WHERE e.trial_id = s.trial_id
                      AND e.step_idx < s.step_idx AND e.tool = s.tool
                      AND e.args_json = s.args_json)
          AND NOT EXISTS (
            SELECT 1 FROM deltas d WHERE d.trial_id = s.trial_id AND d.step_idx = s.step_idx)
        GROUP BY 1""",
    "loops_same_output": f"""
        SELECT s.trial_id, count(*) AS n FROM steps s
        WHERE {TOOL_CALLS}
          AND EXISTS (SELECT 1 FROM steps e WHERE e.trial_id = s.trial_id
                      AND e.step_idx < s.step_idx AND e.tool = s.tool
                      AND e.args_json = s.args_json AND e.output = s.output)
          AND NOT EXISTS (
            SELECT 1 FROM deltas d WHERE d.trial_id = s.trial_id AND d.step_idx = s.step_idx)
        GROUP BY 1""",
    "reverts": """
        SELECT d.trial_id, count(*) AS n FROM deltas d
        WHERE d.sha256_after IS NOT NULL
          AND EXISTS (SELECT 1 FROM deltas e WHERE e.trial_id = d.trial_id AND e.path = d.path
                      AND e.step_idx < d.step_idx AND e.sha256_before = d.sha256_after)
        GROUP BY 1""",
    "retries": f"""
        WITH calls AS (
            SELECT trial_id, tool, args_json, is_error,
                   row_number() OVER (PARTITION BY trial_id ORDER BY step_idx) AS call_idx
            FROM steps WHERE {TOOL_CALLS})
        SELECT s.trial_id, count(*) AS n FROM calls s
        WHERE EXISTS (SELECT 1 FROM calls e WHERE e.trial_id = s.trial_id
                      AND e.call_idx BETWEEN s.call_idx - 3 AND s.call_idx - 1
                      AND e.tool = s.tool AND e.args_json = s.args_json AND e.is_error)
        GROUP BY 1""",
    "rejected": f"""
        SELECT trial_id, count(*) AS n FROM steps
        WHERE {TOOL_CALLS} AND output LIKE '<tool_use_error>%' GROUP BY 1""",
    "cache_rows": """
        SELECT trial_id, count(*) AS n FROM deltas WHERE path LIKE '%/__pycache__/%' GROUP BY 1""",
    "delta_rows": "SELECT trial_id, count(*) AS n FROM deltas GROUP BY 1",
    "idle_waits": f"""
        SELECT s.trial_id, count(*) AS n FROM steps s
        WHERE {TOOL_CALLS} AND epoch(s.t_end - s.t_start) > 120
          AND NOT EXISTS (
            SELECT 1 FROM deltas d WHERE d.trial_id = s.trial_id AND d.step_idx = s.step_idx)
        GROUP BY 1""",
    "idle_s": f"""
        SELECT s.trial_id, round(sum(epoch(s.t_end - s.t_start)))::BIGINT AS n FROM steps s
        WHERE {TOOL_CALLS} AND epoch(s.t_end - s.t_start) > 120
          AND NOT EXISTS (
            SELECT 1 FROM deltas d WHERE d.trial_id = s.trial_id AND d.step_idx = s.step_idx)
        GROUP BY 1""",
    "scripts_added": """
        SELECT trial_id, count(DISTINCT path) AS n FROM deltas
        WHERE op = 'add' AND path LIKE '%.py' AND path NOT LIKE '%/__pycache__/%' GROUP BY 1""",
    "prompt_path_versions": f"""
        WITH named AS (
            SELECT trial_id, unnest(regexp_extract_all(prompt, '{PROMPT_PATH}')) AS path
            FROM trials)
        SELECT d.trial_id, count(*) AS n FROM deltas d
        WHERE EXISTS (SELECT 1 FROM named n WHERE n.trial_id = d.trial_id AND n.path = d.path)
        GROUP BY 1""",
}

PROMPT_PATHS = f"""
    SELECT list(DISTINCT path ORDER BY path) FROM (
        SELECT unnest(regexp_extract_all(prompt, '{PROMPT_PATH}')) AS path
        FROM trials WHERE trial_id = ?)"""

CHURN = """
    SELECT path, count(*) AS versions, list(step_idx ORDER BY step_idx) AS steps
    FROM deltas WHERE trial_id = ? AND path NOT LIKE '%/__pycache__/%'
    GROUP BY path ORDER BY versions DESC, path LIMIT ?"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("store", type=Path, help="A store dir holding record.duckdb.")
    parser.add_argument("--churn", type=int, default=5, help="Paths listed per trial.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    with schema.connect(schema.record_path(args.store), read_only=True) as con:
        trials = con.execute(
            "SELECT trial_id, trial_name, reward FROM trials ORDER BY trial_name"
        ).fetchall()
        counts: dict[str, dict[Any, int]] = {}
        for rule, sql in RULES.items():
            counts[rule] = dict(con.execute(sql).fetchall())
            logger.info("%s: %s", rule, sum(counts[rule].values()))
        report: dict[str, Any] = {
            "store": str(args.store),
            "rules": {rule: " ".join(sql.split()) for rule, sql in RULES.items()},
            "trials": [],
        }
        for trial_id, trial_name, reward in trials:
            entry: dict[str, Any] = {"trial_name": trial_name, "reward": reward}
            entry.update({rule: counts[rule].get(trial_id, 0) for rule in RULES})
            entry["prompt_paths"] = con.execute(PROMPT_PATHS, [trial_id]).fetchone()[0]
            entry["churn"] = [
                {"path": path, "versions": versions, "steps": steps}
                for path, versions, steps in con.execute(CHURN, [trial_id, args.churn]).fetchall()
            ]
            report["trials"].append(entry)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
