"""Report ADR-0020's attribution over a schema-3 store: what the rule decided, per trial.

Reads `delta_attribution`, `step_jobs`, and `steps` through the agent connection (read-only, no
other file reachable) and prints, per trial with a job and in total: the rows by attribution,
the late deltas and their lags in calls, the contested rows with the landing call's command and
the launcher's, and the backgrounded calls with their job windows. The numbers for ADR-0020's
amendment of 2026-10-27 come from this script on the schema-3 rebuild of
corpus/stores/tb40-sonnet-v3.

    uv run python scripts/2026-10-27_adr0020_attribution_report.py <store> [--all-trials]
        [--contested N] [--markdown]
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from trajlab.store.schema import connect_agent

ATTRIBUTIONS = (
    "foreground",
    "background",
    "background_by_path",
    "background_untracked",
    "contested",
    "untrusted_mtime",
    "unknown",
)
CACHE = r"/__pycache__/|\.pyc$|/\.pytest_cache/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("store", type=Path)
    parser.add_argument("--all-trials", action="store_true", help="Trials without a job too.")
    parser.add_argument("--contested", type=int, default=12, help="Contested rows shown per trial.")
    parser.add_argument("--markdown", action="store_true", help="The totals as a Markdown table.")
    args = parser.parse_args()

    with connect_agent(args.store) as con:
        trials = con.execute(
            """
            SELECT t.trial_id, t.trial_name, t.reward,
                   count(*) FILTER (WHERE j.backgrounded) AS backgrounded,
                   count(*) FILTER (WHERE j.launches_job) AS launched
            FROM trials t LEFT JOIN step_jobs j ON j.trial_id = t.trial_id
            GROUP BY ALL ORDER BY t.trial_name
            """
        ).fetchall()
        totals: Counter[tuple[str, bool]] = Counter()
        lags: Counter[int] = Counter()
        for trial_id, name, reward, backgrounded, launched in trials:
            counts = Counter(
                {
                    (attribution, cache): n
                    for attribution, cache, n in con.execute(
                        f"""
                        SELECT attribution, regexp_matches(path, '{CACHE}') AS cache, count(*)
                        FROM delta_attribution WHERE trial_id = ? GROUP BY ALL
                        """,
                        [trial_id],
                    ).fetchall()
                }
            )
            totals.update(counts)
            late = con.execute(
                "SELECT step_idx - launched_by_step AS lag, count(*) FROM delta_attribution "
                "WHERE trial_id = ? AND is_late GROUP BY 1 ORDER BY 1",
                [trial_id],
            ).fetchall()
            lags.update({lag: n for lag, n in late})
            if not (backgrounded or launched or args.all_trials):
                continue
            print(
                f"\n=== {name} (reward {reward}): {backgrounded} backgrounded, {launched} launched"
            )
            for attribution in ATTRIBUTIONS:
                plain, cache = counts[(attribution, False)], counts[(attribution, True)]
                if plain or cache:
                    print(f"  {attribution:22s} {plain:6d} rows + {cache:5d} cache")
            if late:
                print("  late deltas by lag: " + ", ".join(f"{lag}: {n}" for lag, n in late))
            for step_idx, task, status, start, end, command in con.execute(
                """
                SELECT j.step_idx, j.task_id, j.job_status, s.t_start, j.job_end,
                       json_extract_string(s.args_json, '$.command')
                FROM step_jobs j JOIN steps s USING (trial_id, step_idx)
                WHERE j.trial_id = ? AND j.backgrounded ORDER BY 1
                """,
                [trial_id],
            ).fetchall():
                print(
                    f"  job at step {step_idx} {task} {status or 'no notice':9s} "
                    f"{start:%H:%M:%S} -> {end:%H:%M:%S} | {_short(command)}"
                )
            for row in con.execute(
                """
                SELECT a.step_idx, a.path, a.write_at, a.launcher_step,
                       json_extract_string(k.args_json, '$.command'), k.tool,
                       json_extract_string(l.args_json, '$.command')
                FROM delta_attribution a
                JOIN steps k ON k.trial_id = a.trial_id AND k.step_idx = a.step_idx
                JOIN steps l ON l.trial_id = a.trial_id AND l.step_idx = a.launcher_step
                WHERE a.trial_id = ? AND a.attribution = 'contested'
                  AND NOT regexp_matches(a.path, ?)
                ORDER BY a.step_idx, a.path LIMIT ?
                """,
                [trial_id, CACHE, args.contested],
            ).fetchall():
                step_idx, path, write_at, launcher, landing_cmd, tool, launcher_cmd = row
                print(
                    f"  contested {path} landed {step_idx} ({tool}: {_short(landing_cmd, 50)}) "
                    f"write {write_at:%H:%M:%S} launcher {launcher}: {_short(launcher_cmd, 60)}"
                )

    print("\n=== totals (non-cache rows / cache rows) ===")
    rows = [
        (attribution, totals[(attribution, False)], totals[(attribution, True)])
        for attribution in ATTRIBUTIONS
    ]
    if args.markdown:
        print("| Attribution | Rows | Cache rows |\n|---|---|---|")
        for attribution, plain, cache in rows:
            print(f"| `{attribution}` | {plain:,} | {cache:,} |")
    else:
        for attribution, plain, cache in rows:
            print(f"  {attribution:22s} {plain:7d} / {cache:6d}")
    print(
        "late deltas: "
        + json.dumps({str(lag): n for lag, n in sorted(lags.items())}, separators=(",", ":"))
    )


def _short(text: str | None, width: int = 90) -> str:
    return (text or "").replace("\n", " ")[:width]


if __name__ == "__main__":
    main()
