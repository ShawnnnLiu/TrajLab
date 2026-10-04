"""Remove all but the newest checkpoint image of each running repair trial, every few minutes.

`trajlab repair --prune` keeps only each repair trial's final checkpoint image, but only once the
trial's job has finished. Some repairs commit a large container diff at every tool call (863 MB per
checkpoint on a layout-config-recreation `state` repair of `tb40-repair-v3`), which fills the disk
before the job ends. This removes the same images earlier: in a running trial, every checkpoint
image older than its newest record. The newest always stays, so the final one survives, and the
end state is the one `--prune` leaves (ADR-0012, amendment of 2026-10-04).

Checkpoint records are never touched. Each removed image is appended to
`_repair-inputs/<job>/pruned-early.json` (outside the trial dir, which the agent can see at
/logs/agent); the job's end-of-run `pruned.json` then lists only the images left to remove. Change
gating compares file listings held by the watcher, not images, so capture is unaffected.

Exits once the launcher's status shows no running or pending job.

    uv run python scripts/2026-10-04_prune_running_repairs.py corpus/jobs --prefix tb40-repair-v3
"""

import argparse
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from harbor.models.trial.paths import TrialPaths

from trajlab.capture.discover import iter_trial_dirs
from trajlab.capture.repair_launcher import (
    INPUTS_DIRNAME,
    checkpoint_tag,
    load_result,
    remove_checkpoint_image,
)
from trajlab.contracts import CHECKPOINT_RECORDS_FILENAME, CHECKPOINTS_DIRNAME, CheckpointRecord

logger = logging.getLogger("prune_running_repairs")
PRUNED_EARLY_FILENAME = "pruned-early.json"


def running(trial_dir: Path) -> bool:
    result = load_result(trial_dir)
    return result is None or result.finished_at is None


def prune_trial(trial_dir: Path, log_path: Path) -> int:
    records_path = (
        TrialPaths(trial_dir).agent_dir / CHECKPOINTS_DIRNAME / CHECKPOINT_RECORDS_FILENAME
    )
    if not records_path.is_file():
        return 0
    records = [
        CheckpointRecord.model_validate_json(line)
        for line in records_path.read_text().splitlines()
        if line.strip()
    ]
    if len(records) < 2:
        return 0
    newest = max(r.seq for r in records)
    log = json.loads(log_path.read_text()) if log_path.is_file() else {}
    done = log.setdefault(trial_dir.name, [])
    removed = 0
    for record in records:
        if record.seq == newest:
            continue
        tag = checkpoint_tag(record.checkpoint_id)
        if tag is None or not remove_checkpoint_image(tag):
            continue
        done.append(
            {
                "seq": record.seq,
                "checkpoint_id": record.checkpoint_id,
                "newest_seq": newest,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        removed += 1
    if removed:
        log_path.write_text(json.dumps(log, indent=2) + "\n")
    return removed


def one_pass(jobs_dir: Path, prefix: str) -> None:
    for job_dir in sorted(jobs_dir.glob(f"{prefix}-*")):
        if not job_dir.is_dir():
            continue
        log_path = jobs_dir / INPUTS_DIRNAME / job_dir.name / PRUNED_EARLY_FILENAME
        for trial_dir in iter_trial_dirs(job_dir):
            if running(trial_dir) and (n := prune_trial(trial_dir, log_path)):
                logger.info("removed %d checkpoint images of %s", n, trial_dir.name)


def launcher_done(jobs_dir: Path, prefix: str) -> bool:
    status = json.loads((jobs_dir / INPUTS_DIRNAME / f"{prefix}.status.json").read_text())
    return not status["running_jobs"] and not status["pending_jobs"]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("jobs_dir", type=Path)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--every", type=float, default=600.0, help="Seconds between passes.")
    args = parser.parse_args()
    while True:
        one_pass(args.jobs_dir, args.prefix)
        if launcher_done(args.jobs_dir, args.prefix):
            logger.info("launcher has no running or pending job; exiting")
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()
