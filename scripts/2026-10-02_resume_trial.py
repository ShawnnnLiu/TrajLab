"""Resume a captured trial from one of its checkpoints, as a new Harbor job.

Manual: needs Docker, the checkpoint image, credentials in .env, and (because the resumed run
is captured too) a running `trajlab watch corpus/jobs ...`.

The new trial starts its container from checkpoint `--seq` of the source trial
(`CheckpointResumeEnvironment`) and its conversation from the source's native session cut right
after the tool result of that checkpoint's call (`cut_session`, loaded with Harbor's
`load_trajectory`, so Claude Code runs with `--resume`). Harbor then sends the task instruction
again as the next user message: it always pipes the instruction in.

    uv run python scripts/2026-10-02_resume_trial.py corpus/jobs/<job>/<trial> --seq 3 \
        --model anthropic/claude-sonnet-5-5 --effort high --job-name resume-demo-v1
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from harbor.models.trial.config import TrialConfig

from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.resume import cut_session
from trajlab.contracts import CheckpointRecord

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("resume")

JOBS_DIR = Path("corpus/jobs")
INPUTS_DIR = JOBS_DIR / "_resume-inputs"
HOOKS = "configs/claude-code/settings.hooks.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trial_dir", type=Path)
    parser.add_argument("--seq", type=int, required=True, help="Checkpoint to resume from.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Write the config, run nothing.")
    args = parser.parse_args()

    trial: Path = args.trial_dir
    source = TrialConfig.model_validate_json((trial / "config.json").read_text())
    records = [
        CheckpointRecord.model_validate_json(line)
        for line in (trial / "agent/checkpoints/checkpoints.jsonl").read_text().splitlines()
        if line
    ]
    record = next((r for r in records if r.seq == args.seq), None)
    if record is None:
        raise SystemExit(f"{trial} has no checkpoint {args.seq}")
    if record.trigger != "tool_call":
        raise SystemExit("resume from a stop checkpoint: the agent had already finished")
    image = f"trajlab-checkpoint:{record.trial_name}.{record.seq:04d}"
    found = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
    )
    if found.returncode != 0 or found.stdout.strip() != record.checkpoint_id:
        raise SystemExit(f"{image} is missing or is not {record.checkpoint_id}")

    sessions = sorted((trial / "agent/sessions/projects").glob("*/*.jsonl"))
    if len(sessions) != 1:
        raise SystemExit(f"expected one native session, found {sessions}")
    inputs = INPUTS_DIR / args.job_name
    inputs.mkdir(parents=True, exist_ok=True)
    cut = cut_session(sessions[0].read_text().splitlines(), record.tool_call_id)
    session = inputs / sessions[0].name
    session.write_text("\n".join(cut) + "\n")
    logger.info(
        "conversation cut after %s: %d of %d session lines",
        record.tool_call_id,
        len(cut),
        len(sessions[0].read_text().splitlines()),
    )

    task = source.task
    # The source job's own dataset entry (name and dataset digest), narrowed to this task.
    job_config = json.loads((trial.parent / "config.json").read_text())
    dataset = next(
        d for d in job_config["datasets"] if task.name in d.get("task_names", [task.name])
    )
    kwargs: dict[str, object] = {"version": CLAUDE_CODE_VERSION, "config": HOOKS}
    if args.effort:
        kwargs["reasoning_effort"] = args.effort
    config = {
        "job_name": args.job_name,
        "jobs_dir": str(JOBS_DIR),
        "n_concurrent_trials": 1,
        "agent_timeout_multiplier": 2.0,
        "agents": [
            {
                "name": "claude-code",
                "model_name": args.model,
                "kwargs": kwargs,
                "load_trajectory": str(session.resolve()),
            }
        ],
        "datasets": [dataset | {"task_names": [task.name]}],
        "environment": {
            "import_path": "trajlab.capture.resume:CheckpointResumeEnvironment",
            "kwargs": {"checkpoint_image": image},
        },
    }
    config_path = inputs / "config.json"
    config_path.write_text(json.dumps(config, indent=4) + "\n")
    logger.info("wrote %s", config_path)
    if args.dry_run:
        return 0
    harbor = Path(sys.executable).parent / "harbor"
    return subprocess.run(
        [str(harbor), "run", "--config", str(config_path), "--env-file", ".env"]
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
