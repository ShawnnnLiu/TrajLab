"""List failed first attempts that rewrote a large text file several times: roots for a test of
whole-file hunks (ADR-0023, amendment of 2026-11-04).

Prepared on 2026-11-04 for the capture host, which holds `tb40-sonnet-v4`'s corpus store and
checkpoint images; not yet run there (the host is stopped, and starting it is the owner's call).
It opens a store's `record.duckdb` with DuckDB alone, read-only, so it reads a store of any
schema from 4 on (`deltas`, `trials`, and the derived views `delta_flags` and `trial_summary`);
it was checked on the local schema 5 and schema 6 stores of 2026-11-04.

One row per (failed first attempt, text file): a first attempt (`attempt_idx` 0) whose reward is
below 1 or null, and a file outside the cache paths whose content it changed in place at least
`--modifies` times (rows with a hunk and op `modify`; the add that created it does not count)
and that reached `--min-bytes` in some version. Each row gives the task, the trial, its reward
and tool calls, the path, the number of in-place changes and of versions, the largest and last
size in bytes, and the steps that wrote it. JSON lines on stdout.

    uv run python scripts/2026-11-04_large_rewrite_roots.py corpus/stores/tb40-sonnet-v4
    uv run python scripts/2026-11-04_large_rewrite_roots.py corpus/stores/tb40-sonnet-v4 \\
        --modifies 2 --min-bytes 5000
"""

import argparse
import json
import logging
from pathlib import Path

import duckdb

logger = logging.getLogger("large_rewrite_roots")

SQL = """
WITH text_files AS (
    SELECT d.trial_id, d.path,
           count(*) FILTER (WHERE d.op = 'modify') AS modifies,
           count(*) AS versions,
           max(d.bytes_after) AS max_bytes,
           arg_max(d.bytes_after, d.step_idx) AS last_bytes,
           list(d.step_idx ORDER BY d.step_idx) AS steps
    FROM deltas d JOIN delta_flags f USING (trial_id, step_idx, path)
    WHERE d.hunk IS NOT NULL AND NOT f.is_cache
    GROUP BY d.trial_id, d.path
)
SELECT t.task, t.trial_name, t.reward, s.tool_calls, c.path, c.modifies, c.versions,
       c.max_bytes, c.last_bytes, c.steps
FROM text_files c
JOIN trials t USING (trial_id)
JOIN trial_summary s USING (trial_id)
WHERE t.attempt_idx = 0 AND coalesce(t.reward, 0) < 1
  AND c.modifies >= $modifies AND c.max_bytes >= $min_bytes
ORDER BY t.task, t.trial_name, c.modifies DESC, c.path
"""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store", type=Path, help="A store dir holding record.duckdb.")
    parser.add_argument("--modifies", type=int, default=3, help="In-place changes, at least.")
    parser.add_argument("--min-bytes", type=int, default=10_240, help="Largest version, at least.")
    args = parser.parse_args()
    with duckdb.connect(str(args.store / "record.duckdb"), read_only=True) as con:
        cursor = con.execute(SQL, {"modifies": args.modifies, "min_bytes": args.min_bytes})
        names = [column[0] for column in cursor.description or []]
        rows = cursor.fetchall()
    for row in rows:
        print(json.dumps(dict(zip(names, row, strict=True)), default=str))
    roots = {row[1] for row in rows}
    logger.info("%d files in %d failed first attempts", len(rows), len(roots))


if __name__ == "__main__":
    main()
