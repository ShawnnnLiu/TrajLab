"""Check stores built with whole-file hunks (schema 6) against the same trials' schema 5 stores.

ADR-0023 as amended on 2026-11-04 makes every delta row's hunk a unified diff whose context is the
whole file. For each pair of stores built from the same trial dirs and checkpoint images (the
schema 6 store first, then the schema 5 one), this reads `deltas` from both `record.duckdb`
files, read-only, and checks every row:

- `same_rows`: both stores have the same (trial_id, step_idx, path) keys, and every column but
  `hunk` (and schema 5's `text_after`) is equal.
- `versions_rebuild`: the hunk's " " and "-" lines hash to `sha256_before` and its " " and "+"
  lines to `sha256_after` (`trajlab.store.reconcile.hunk_versions`), for each side that has
  content; a side whose header is /dev/null has no sha256.
- `narrow_reproduces`: `narrow(hunk, 3)`, the probe's `v3-ctx3` transform, is byte for byte the
  schema 5 store's hunk for the row.
- `later_is_text_after`: the later version is schema 5's `text_after` for the row.

It prints one JSON object per pair: the row and hunk counts, the characters of hunks in each
store and of schema 5's `text_after`, the largest whole-file hunk, and each check's failures
(empty lists when it passes).

    uv run python scripts/2026-11-04_full_context_stores.py \\
        --pair corpus/stores/mvcc-local-v6 corpus/stores/mvcc-local-v5 \\
        --pair corpus/stores/pp-local-v6 corpus/stores/pp-local-v5
"""

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import duckdb

from trajlab.store.reconcile import HunkError, hunk_versions, narrow
from trajlab.store.schema import connect, record_path

logger = logging.getLogger("full_context_stores")

CONTEXT = 3


def _rows(store: Path, *, current: bool) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Every deltas row of the store, by key, as column -> value. A `current` store is opened
    through `trajlab.store.schema.connect`, which checks it is schema 6; the other with DuckDB
    alone, read-only, since this code refuses schema 5."""
    opened = (
        connect(record_path(store), read_only=True)
        if current
        else duckdb.connect(str(record_path(store)), read_only=True)
    )
    with opened as con:
        cursor = con.execute("SELECT * FROM deltas ORDER BY trial_id, step_idx, path")
        names = [column[0] for column in cursor.description or []]
        rows = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
    return {(str(row["trial_id"]), row["step_idx"], row["path"]): row for row in rows}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compare(new_store: Path, old_store: Path) -> dict[str, Any]:
    new, old = _rows(new_store, current=True), _rows(old_store, current=False)
    problems: dict[str, list[str]] = {
        "same_rows": [],
        "versions_rebuild": [],
        "narrow_reproduces": [],
        "later_is_text_after": [],
    }
    if new.keys() != old.keys():
        problems["same_rows"].append(
            f"keys only in the new store: {sorted(new.keys() - old.keys())[:5]}; "
            f"only in the old: {sorted(old.keys() - new.keys())[:5]}"
        )
    hunks = 0
    largest = 0
    for key in sorted(new.keys() & old.keys()):
        row, before_row = new[key], old[key]
        label = f"{key[1]} {key[2]}"
        shared = set(row) - {"hunk"}
        if any(row[c] != before_row[c] for c in shared):
            problems["same_rows"].append(label)
        hunk = row["hunk"]
        if hunk is None:
            if before_row["hunk"] is not None:
                problems["same_rows"].append(f"{label}: a hunk only in the old store")
            continue
        hunks += 1
        largest = max(largest, len(hunk))
        try:
            earlier, later = hunk_versions(hunk)
            narrowed = narrow(hunk, CONTEXT)
        except HunkError as error:
            problems["versions_rebuild"].append(f"{label}: {error}")
            continue
        for text, sha in ((earlier, row["sha256_before"]), (later, row["sha256_after"])):
            if sha is not None and (text is None or _sha256(text) != sha):
                problems["versions_rebuild"].append(label)
        if narrowed != before_row["hunk"]:
            problems["narrow_reproduces"].append(label)
        if later != before_row["text_after"] and not (later == "" and row["sha256_after"] is None):
            problems["later_is_text_after"].append(label)
    return {
        "store": str(new_store),
        "against": str(old_store),
        "rows": len(new),
        "hunks": hunks,
        "hunk_chars": sum(len(r["hunk"] or "") for r in new.values()),
        "old_hunk_chars": sum(len(r["hunk"] or "") for r in old.values()),
        "old_text_after_chars": sum(len(r.get("text_after") or "") for r in old.values()),
        "largest_hunk_chars": largest,
        **problems,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pair",
        nargs=2,
        type=Path,
        action="append",
        required=True,
        metavar=("SCHEMA6_STORE", "SCHEMA5_STORE"),
    )
    args = parser.parse_args()
    for new_store, old_store in args.pair:
        print(json.dumps(compare(new_store, old_store)))


if __name__ == "__main__":
    main()
