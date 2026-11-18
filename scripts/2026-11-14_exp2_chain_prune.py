"""Prune, while Exp 2 chains run, the checkpoint images of attempts no chain store will ingest.

Beside `scripts/2026-11-10_exp2_passk_chains.py run --prune-ingested`, which removes a failed
record-engine attempt's images once it is in its chain's store. That leaves two kinds on disk,
pruned by hand on 2026-11-13 and by this script from 2026-11-14 (owner's word: "prune as you
go"): every finished control attempt, and every attempt that passed. A failed record-engine
attempt is never touched here, so nothing is removed before its ingest. Needs Docker; run by
hand from `~/trajlab`. Deleting an image cannot be undone.

    uv run python scripts/2026-11-14_exp2_chain_prune.py corpus/exp2/roots2-jobs --until-idle 30
"""

import argparse
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from trajlab.capture.repair_launcher import checkpoint_tag, remove_checkpoint_image
from trajlab.contracts import CheckpointRecord

logger = logging.getLogger("exp2_chain_prune")
JOB = re.compile(r"-(control|record-engine)-a\d+(-r\d+)?$")


def reason_to_prune(job: Path, trial: Path) -> str | None:
    found = JOB.search(job.name)
    path = trial / "result.json"
    if found is None or not path.is_file():
        return None
    result = json.loads(path.read_text())
    if not result.get("finished_at"):
        return None
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    if rewards.get("reward"):
        return "passed attempt: never ingested (owner's word 2026-11-14)"
    if found.group(1) == "control":
        return "control retry: never ingested (owner's word 2026-11-14)"
    return None


def prune(trial: Path, reason: str) -> int:
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
        "reason": reason,
        "at": datetime.now(UTC).isoformat(),
    }
    pruned_path.write_text(json.dumps(out, indent=2) + "\n")
    logger.info("pruned %d checkpoint images of %s (%s)", len(removed), trial.name, reason)
    return len(removed)


def one_pass(jobs_dir: Path) -> int:
    removed = 0
    for job in sorted(jobs_dir.iterdir()):
        if not job.is_dir():
            continue
        for trial in sorted(job.glob("*__*")):
            reason = reason_to_prune(job, trial)
            if reason is not None:
                removed += prune(trial, reason)
    return removed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("jobs_dir", type=Path)
    parser.add_argument(
        "--until-idle",
        type=float,
        default=0,
        help="Keep polling until no attempt dir has changed for this many minutes; 0: one pass.",
    )
    args = parser.parse_args()
    while True:
        one_pass(args.jobs_dir)
        newest = max((p.stat().st_mtime for p in args.jobs_dir.glob("*/*")), default=0.0)
        if not args.until_idle or time.time() - newest > args.until_idle * 60:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
