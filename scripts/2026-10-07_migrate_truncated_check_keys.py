"""Rename check keys that the first ground-truth parser cut at the first space (2026-10-07).

Until the fix in `trajlab.groundtruth.checks`, pytest node ids whose parameters hold spaces
(foodstuff-beta-activity: `test_value_within_tolerance[Detection limit (Bq/kg)]`) were cut to
`...[Detection`. Labels and fix records written by the labelers then carry the cut keys. This
maps each cut key to the one full key of the trial that starts with it, and rewrites
`labels.json` and `fixes.jsonl` (under the same lock the writers use). Replay records are
re-read with `trajlab gt reparse`.

    uv run python scripts/2026-10-07_migrate_truncated_check_keys.py corpus/jobs/tb40-sonnet-v2
"""

import fcntl
import json
import re
import sys
from pathlib import Path

from trajlab.groundtruth.checks import read_checks, statuses

CUT = re.compile(r'"(pytest:[^"\]]*\[[^"\]]*)"')


def full_keys(trial: Path) -> list[str]:
    return list(statuses(read_checks(trial / "verifier")))


def mapping_for(text: str, keys: list[str]) -> dict[str, str]:
    mapping = {}
    for cut in set(CUT.findall(text)):
        matches = [k for k in keys if k.startswith(cut)]
        if len(matches) != 1:
            raise SystemExit(f"cannot map {cut!r}: {matches}")
        mapping[cut] = matches[0]
    return mapping


def rewrite(path: Path, keys: list[str]) -> int:
    with path.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            text = handle.read()
            mapping = mapping_for(text, keys)
            for cut, full in mapping.items():
                text = text.replace(json.dumps(cut), json.dumps(full))
            if mapping:
                handle.seek(0)
                handle.truncate()
                handle.write(text)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return len(mapping)


def main() -> int:
    job = Path(sys.argv[1])
    for trial in sorted(p for p in job.iterdir() if (p / "groundtruth").is_dir()):
        keys = full_keys(trial)
        for name in ("labels.json", "fixes.jsonl"):
            path = trial / "groundtruth" / name
            if path.is_file():
                renamed = rewrite(path, keys)
                if renamed:
                    print(f"{trial.name} {name}: {renamed} keys renamed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
