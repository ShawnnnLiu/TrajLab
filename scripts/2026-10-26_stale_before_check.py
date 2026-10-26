"""List the diffs in a job's trials that may be against an older version of their file, and count
the calls recorded twice: the two `--extras` faults fixed on 2026-10-22, in review of PR #32
(ADR-0015 §2).

Before the fix, a file that a call changed without reading it (a call reads at most 20) kept the
version read at an earlier call, so the file's next diff and `before_sha256` started from that
version and could show the unread call's changes as its own. `trajlab trace` marks such a diff
(`stale_before` in `capture/trace.py`); this prints each one the site marks. A call retried after
a failed snapshot also got a second, empty line in `changes.jsonl`; the site reads the first, so
those are only counted. After-hashes are right in both cases.

    uv run python scripts/2026-10-26_stale_before_check.py corpus/jobs/tb40-detector-off-v1
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from trajlab.capture.trace import build_trace, trial_dirs
from trajlab.contracts import CHANGES_FILENAME, TRAJLAB_DIRNAME


def call_name(n: int | None) -> str:
    return "the stop" if n is None else f"#{n}"


def recorded_twice(changes: Path) -> int:
    """The calls with more than one line in a changes.jsonl."""
    ids: Counter[str] = Counter()
    for line in changes.read_bytes().splitlines():
        try:
            ids[json.loads(line)["tool_call_id"]] += 1
        except (ValueError, KeyError, TypeError):
            continue
    return sum(1 for count in ids.values() if count > 1)


def check(job: Path) -> tuple[int, int, int]:
    """Print each marked diff and twice-recorded call of a job's trials; return the number of
    trials with extras, of marked diffs, and of calls recorded twice."""
    trials = marked = twice = 0
    for trial in trial_dirs(job):
        changes = trial / TRAJLAB_DIRNAME / CHANGES_FILENAME
        if not changes.is_file():
            continue
        trials += 1
        found = []
        for path, versions in build_trace(trial)["files"].items():
            for version in versions:
                stale = version["stale_before"]
                if stale is not None:
                    found.append((version["event"], version["call"], path, stale))
        for _, call, path, stale in sorted(found, key=lambda f: f[0]):
            print(
                f"{trial.name}  {call_name(call)} {path}: diffed from the file after "
                f"{call_name(stale['read_at'])}; {call_name(stale['unread_at'])} changed it unread"
            )
        marked += len(found)
        count = recorded_twice(changes)
        if count:
            print(f"{trial.name}  {count} call(s) recorded twice; the site reads the first line")
        twice += count
    return trials, marked, twice


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("jobs", nargs="+", type=Path, help="job dirs, such as corpus/jobs/<job>")
    args = parser.parse_args()
    for job in args.jobs:
        if not job.is_dir():
            parser.error(f"not a directory: {job}")
    for job in args.jobs:
        trials, marked, twice = check(job)
        print(
            f"{job.name}: {trials} trials with extras; {marked} diffs that may be against an "
            f"older version; {twice} calls recorded twice"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
