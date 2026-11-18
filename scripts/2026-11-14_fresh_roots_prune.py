"""Pick roots from a fresh-attempts job and prune the other trials' checkpoint images as it runs.

For the replication run after ADR-0025 (`tb40-passk-fresh-v2`: cargo-flight-dispatch,
freecad-impeller, freecad-spring-clip). Needs Docker; run by hand from `~/trajlab`.

A task is settled once all its attempts have a finished `result.json`. Its roots are then the
`--roots` failed attempts whose graded score is closest to the task's median over all its scored
attempts, ties to the earliest `started_at` (the rule of
`docs/experiments/exp2/2026-11-13_score-trend-chains.md`, taken three times). Every other trial
of the task has its checkpoint images removed (owner's word, 2026-11-14: "prune as you go"); its
checkpoint records stay and `agent/checkpoints/pruned.json` lists what was removed, in the shape
`trajlab.exp2.chain.prune_ingested` writes. Deleting an image cannot be undone.

    uv run python scripts/2026-11-14_fresh_roots_prune.py corpus/jobs/tb40-passk-fresh-v2 \
        --out corpus/exp2/roots2/roots.json --attempts 5 --roots 3 [--wait]

Extended on 2026-11-16 for the `roots3` run (`tb40-passk-fresh-v3`, and bun-sourcemap-leak's 20
attempts of `tb40-passk-fresh-v1`, whose images are never deleted): `--only` takes the tasks to
settle, `--no-prune` chooses roots and removes nothing, and a verifier whose pytest summary has no
"passed" count (every test failed) is scored from `verifier/ctrf.json`.

    uv run python scripts/2026-11-14_fresh_roots_prune.py corpus/jobs/tb40-passk-fresh-v1 \
        --out corpus/exp2/roots3/roots.json --attempts 20 --only bun-sourcemap-leak --no-prune
"""

import argparse
import json
import logging
import re
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from trajlab.capture.repair_launcher import checkpoint_tag, remove_checkpoint_image
from trajlab.contracts import CheckpointRecord

logger = logging.getLogger("fresh_roots_prune")
SUMMARY = re.compile(r"=+ (?:(\d+) failed, )?(\d+) passed.* in [\d.]+s")
REASON = "fresh attempt that is not a root (owner's word 2026-11-14, replication after ADR-0025)"


def graded(trial: Path) -> float | None:
    """freecad: the verifier's `combined_raw`; otherwise hidden tests passed."""
    details = trial / "verifier" / "reward_details.json"
    if details.is_file():
        found = json.loads(details.read_text())
        value = found.get("combined_raw", found.get("combined"))
        return float(value) if value is not None else None
    stdout = trial / "verifier" / "test-stdout.txt"
    if stdout.is_file() and (found := SUMMARY.search(stdout.read_text(errors="replace"))):
        return float(found.group(2))
    ctrf = trial / "verifier" / "ctrf.json"
    if ctrf.is_file():
        summary = json.loads(ctrf.read_text()).get("results", {}).get("summary", {})
        if "passed" in summary:
            return float(summary["passed"])
    return None


def finished(trial: Path) -> dict | None:
    path = trial / "result.json"
    if not path.is_file():
        return None
    result = json.loads(path.read_text())
    return result if result.get("finished_at") else None


def reward(result: dict) -> float | None:
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    return rewards.get("reward")


def prune(trial: Path) -> int:
    directory = trial / "agent" / "checkpoints"
    pruned_path = directory / "pruned.json"
    records_path = directory / "checkpoints.jsonl"
    if pruned_path.is_file() or not records_path.is_file():
        return 0
    removed = []
    for line in records_path.read_text().splitlines():
        if not line:
            continue
        record = CheckpointRecord.model_validate_json(line)
        tag = checkpoint_tag(record.checkpoint_id)
        if tag is not None and remove_checkpoint_image(tag):
            removed.append({"seq": record.seq, "checkpoint_id": record.checkpoint_id})
    out = {
        "kept_seq": None,
        "removed": removed,
        "reason": REASON,
        "at": datetime.now(UTC).isoformat(),
    }
    pruned_path.write_text(json.dumps(out, indent=2) + "\n")
    logger.info("pruned %d checkpoint images of %s", len(removed), trial.name)
    return len(removed)


def settle(
    job: Path,
    attempts: int,
    n_roots: int,
    chosen: dict[str, dict],
    only: list[str] | None = None,
    pruning: bool = True,
) -> None:
    by_task: dict[str, list[Path]] = {}
    for trial in sorted(job.glob("*__*")):
        by_task.setdefault(trial.name.rsplit("__", 1)[0], []).append(trial)
    for task, trials in by_task.items():
        if only and task not in only:
            continue
        results = {t: finished(t) for t in trials}
        if task in chosen or len(trials) < attempts or not all(results.values()):
            continue
        rows = [
            {
                "trial": t.name,
                "score": graded(t),
                "reward": reward(r),
                "started_at": r["started_at"],
                "exception": (r.get("exception_info") or {}).get("exception_type"),
            }
            for t, r in results.items()
        ]
        scored = [row for row in rows if row["score"] is not None and not row["exception"]]
        if not scored:
            logger.warning("%s: no scored attempt; nothing chosen, nothing pruned", task)
            continue
        median = statistics.median(row["score"] for row in scored)
        failed = [row for row in scored if not row["reward"]]
        failed.sort(key=lambda row: (abs(row["score"] - median), row["started_at"]))
        roots = [row["trial"] for row in failed[:n_roots]]
        chosen[task] = {"median": median, "roots": roots, "attempts": rows}
        logger.info("%s: median %s, roots %s", task, median, roots)
        for trial in trials:
            if pruning and trial.name not in roots:
                prune(trial)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--attempts", type=int, required=True, help="Attempts per task.")
    parser.add_argument("--roots", type=int, default=3)
    parser.add_argument("--tasks", type=int, default=3, help="Tasks in the job (for --wait).")
    parser.add_argument("--wait", action="store_true", help="Poll until every task is settled.")
    parser.add_argument("--only", nargs="+", help="Settle only these tasks of the job.")
    parser.add_argument("--no-prune", action="store_true", help="Choose roots; remove nothing.")
    args = parser.parse_args()
    chosen: dict[str, dict] = json.loads(args.out.read_text()) if args.out.is_file() else {}
    while True:
        settle(args.job, args.attempts, args.roots, chosen, args.only, not args.no_prune)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(chosen, indent=2) + "\n")
        settled = [task for task in chosen if not args.only or task in args.only]
        if not args.wait or len(settled) >= (len(args.only) if args.only else args.tasks):
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
