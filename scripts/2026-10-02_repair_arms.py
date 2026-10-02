"""Repair every failed trial of a source job under four arms, as each source trial finishes.

Manual: needs Docker, credentials in .env, and a running `trajlab watch corpus/jobs ...` (repair
trials are captured too). Runs until every source trial has finished and every repair job has
exited.

Arms (state x information, 2x2); every arm runs `RepairClaudeCode`, so the instruction starts with
the same note that an earlier attempt failed:

    fresh       task image, new conversation              (retry baseline)
    state       final checkpoint of the failed trial, new conversation
    state-traj  final checkpoint, the failed trial's full native session loaded (--resume)
    traj        task image, the failed trial's full native session loaded

The final checkpoint is the failed trial's highest-seq checkpoint: its stop checkpoint when the
filesystem changed after the last hooked call (ADR-0011), else the checkpoint that holds that
state. Each (trial, arm) is its own Harbor job, `<prefix>-<task>-<arm>`, recorded by
`trajlab run` with a manifest; new manifests are committed before the next launch so the next
manifest records a clean tree.

    uv run python scripts/2026-10-02_repair_arms.py corpus/jobs/tb40-sonnet-v1 \
        --prefix tb40-repair-v1 --max-running 3
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harbor.models.trial.result import TrialResult

from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.contracts import CheckpointRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("repair")

JOBS_DIR = Path("corpus/jobs")
INPUTS_DIR = JOBS_DIR / "_repair-inputs"
MANIFESTS_DIR = Path("corpus/manifests")
HOOKS = "configs/claude-code/settings.hooks.json"
ARMS = ("fresh", "state", "state-traj", "traj")
POLL_S = 30
# Host disk guard (the Docker VM's disk image lives on the host volume).
PRUNE_BELOW_GB = 8.0
HOLD_BELOW_GB = 4.0


def free_gb() -> float:
    return shutil.disk_usage(JOBS_DIR).free / 1e9


def prune_intermediate_checkpoints(job_dirs: list[Path]) -> None:
    """Remove all but the final checkpoint image of every finished trial in these jobs.

    Repair arms need only a failed trial's final checkpoint, and the records in
    checkpoints.jsonl stay; only the intermediate images go.
    """
    for job in job_dirs:
        for trial in (p for p in job.iterdir() if p.is_dir()):
            final = final_checkpoint(trial)
            if final is None or not (trial / "result.json").exists():
                continue
            path = trial / "agent/checkpoints/checkpoints.jsonl"
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                record = CheckpointRecord.model_validate_json(line)
                if record.seq == final.seq:
                    continue
                image = f"trajlab-checkpoint:{record.trial_name}.{record.seq:04d}"
                removed = subprocess.run(["docker", "rmi", image], capture_output=True, text=True)
                if removed.returncode == 0:
                    logger.info("disk guard: removed %s", image)


def final_checkpoint(trial: Path) -> CheckpointRecord | None:
    path = trial / "agent/checkpoints/checkpoints.jsonl"
    if not path.exists():
        return None
    records = [
        CheckpointRecord.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    return max(records, key=lambda r: r.seq, default=None)


def native_session(trial: Path) -> Path:
    sessions = sorted((trial / "agent/sessions/projects").glob("*/*.jsonl"))
    if len(sessions) != 1:
        raise SystemExit(f"{trial}: expected one native session, found {sessions}")
    return sessions[0]


def job_config(trial: Path, arm: str, job_name: str, inputs: Path) -> dict[str, object]:
    source_job = json.loads((trial.parent / "config.json").read_text())
    source_agent = source_job["agents"][0]
    task_name = json.loads((trial / "config.json").read_text())["task"]["name"]
    dataset = next(
        d for d in source_job["datasets"] if task_name in d.get("task_names", [task_name])
    )
    kwargs = {**source_agent["kwargs"], "version": CLAUDE_CODE_VERSION, "config": HOOKS}
    agent: dict[str, object] = {
        "import_path": "trajlab.capture.repair:RepairClaudeCode",
        "model_name": source_agent["model_name"],
        "kwargs": kwargs,
    }
    if arm in ("state-traj", "traj"):
        session = native_session(trial)
        copy = inputs / session.name
        shutil.copyfile(session, copy)
        agent["load_trajectory"] = str(copy.resolve())
    environment: dict[str, object] = {
        "import_path": "trajlab.capture.preinstall:PreinstalledDockerEnvironment"
    }
    if arm in ("state", "state-traj"):
        record = final_checkpoint(trial)
        if record is None:
            raise SystemExit(f"{trial} has no checkpoint")
        image = f"trajlab-checkpoint:{record.trial_name}.{record.seq:04d}"
        environment = {
            "import_path": "trajlab.capture.resume:CheckpointResumeEnvironment",
            "kwargs": {"checkpoint_image": image},
        }
    return {
        "job_name": job_name,
        "jobs_dir": str(JOBS_DIR),
        "n_concurrent_trials": 1,
        "agent_timeout_multiplier": source_job.get("agent_timeout_multiplier", 1.0),
        "agents": [agent],
        "datasets": [dataset | {"task_names": [task_name]}],
        "environment": environment,
    }


def commit_new_manifests() -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    paths = [line[3:] for line in status]
    if not paths:
        return
    if any(not p.startswith(f"{MANIFESTS_DIR}/") for p in paths):
        raise SystemExit(f"tree is dirty outside {MANIFESTS_DIR}: {paths}")
    subprocess.run(["git", "add", *paths], check=True)
    names = ", ".join(Path(p).stem for p in paths)
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", f"Record the {names} manifest"], check=True
    )
    logger.info("committed %s", paths)


def finished_failed(source: Path) -> tuple[list[Path], int]:
    """Failed trials (reward < 1 after the agent ran), and how many trials are still running."""
    failed, running = [], 0
    for trial in sorted(p for p in source.iterdir() if p.is_dir()):
        if not (trial / "config.json").exists():
            continue
        result_path = trial / "result.json"
        if not result_path.exists():
            running += 1
            continue
        result = TrialResult.model_validate_json(result_path.read_text())
        rewards = (result.verifier_result.rewards or {}) if result.verifier_result else {}
        reward = rewards.get("reward", 0.0)
        if reward >= 1.0:
            continue
        if final_checkpoint(trial) is None:
            logger.warning("%s failed with no checkpoint (%s); not repaired", trial.name, reward)
            continue
        failed.append(trial)
    return failed, running


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job", type=Path)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--max-running", type=int, default=3, help="Trials at once, all jobs.")
    args = parser.parse_args()

    source: Path = args.source_job
    queued: set[tuple[str, str]] = set()
    pending: list[tuple[Path, str]] = []
    running: dict[str, subprocess.Popen[bytes]] = {}
    source_done = False
    while True:
        failed, source_running = finished_failed(source)
        source_done = source_running == 0 and (source / "result.json").exists()
        for trial in failed:
            for arm in ARMS:
                if (trial.name, arm) not in queued:
                    queued.add((trial.name, arm))
                    pending.append((trial, arm))
                    logger.info("queued %s %s", trial.name, arm)
        for name, process in list(running.items()):
            if process.poll() is not None:
                logger.info("%s exited %s", name, process.returncode)
                del running[name]
        if free_gb() < PRUNE_BELOW_GB:
            logger.warning("disk guard: %.1f GB free; pruning intermediate checkpoints", free_gb())
            prune_intermediate_checkpoints(
                [source, *sorted(p for p in JOBS_DIR.glob(f"{args.prefix}-*") if p.is_dir())]
            )
        if free_gb() < HOLD_BELOW_GB:
            logger.warning("disk guard: %.1f GB free; holding new launches", free_gb())
            time.sleep(POLL_S)
            continue
        while pending and source_running + len(running) < args.max_running:
            trial, arm = pending.pop(0)
            task = trial.name.split("__")[0]
            job_name = f"{args.prefix}-{task}-{arm}"
            if (JOBS_DIR / job_name).exists():
                logger.info("%s already exists; skipped", job_name)
                continue
            inputs = INPUTS_DIR / job_name
            inputs.mkdir(parents=True, exist_ok=True)
            config_path = inputs / "config.json"
            config_path.write_text(
                json.dumps(job_config(trial, arm, job_name, inputs), indent=4) + "\n"
            )
            commit_new_manifests()
            log = (JOBS_DIR / f"{job_name}.log").open("wb")
            running[job_name] = subprocess.Popen(
                [
                    str(Path(sys.executable).parent / "trajlab"),
                    "run",
                    str(config_path),
                    "--corpus-id",
                    job_name,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            logger.info("launched %s (%d running)", job_name, len(running))
        if source_done and not pending and not running:
            commit_new_manifests()
            logger.info("all repairs finished")
            return 0
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
