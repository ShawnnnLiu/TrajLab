"""Manual check (needs Docker; part 1 calls the API once, on hello-world): traj-text delivery.

Harbor hands Claude Code its prompt in one environment variable, which Linux caps at 128 KiB;
`RepairClaudeCode` delivers every `traj-text` prompt through a file in the container instead
(ADR-0012, "traj-text as built"; docs/upstream-notes.md). This check runs that path for real
with a synthetic transcript of about 200 KiB, which the environment variable could not carry. Its
tool outputs hold carriage returns, non-ASCII text, and shell metacharacters, and each carries
both the text an agent saw and Harbor's longer ATIF content, so the renderer's choice shows too.

1. A repair trial of hello-world/hello-world through `trajlab run`, with the agent kwargs a
   `traj-text` repair job gets (`repair_job_config`) and the synthetic transcript. Checks: the
   trial finished without an exception; the native session's first user message is the whole
   prompt, byte for byte (the numbers also say whether it would match with carriage returns
   read as line feeds); no /tmp/trajlab-prompt-* file is in any of the trial's checkpoint
   images (each taken after `claude` started); checkpoints.jsonl has records and every .req has
   an .ack.
2. Ownership, no API: in a container of the trial's final checkpoint image run as a non-root
   user (65534), sends the same prompt through `RepairClaudeCode.exec_as_agent` with a minimal
   Docker-backed environment. Its upload is `docker cp -a`, which, like Harbor's `docker compose
   cp`, keeps the host user's uid and gid (not the agent user's) on a file in sticky /tmp.
   Checks the agent user's shell gets every byte and the file is gone; as a control, a file
   uploaded the same way, without the agent's chown, is one that user cannot remove.

    uv run python scripts/2026-10-07_traj_text_delivery_check.py [--job NAME] [--env-file PATH]
    uv run python scripts/2026-10-07_traj_text_delivery_check.py --skip-trial --image IMAGE

Run from a clean checkout with the watcher holding corpus/jobs and an env file (`.env` here by
default), as `trajlab run` needs. Part 1 writes corpus/jobs/<job>/ and
corpus/manifests/<job>.json; commit the manifest. Prints one JSON object per part; exits 1 if
any check fails.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from harbor.environments.base import ExecResult
from harbor.models.task.task import strip_canary
from harbor.models.trajectories import Trajectory
from harbor.models.trial.paths import TrialPaths

from trajlab.capture.discover import iter_trial_dirs
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.repair import (
    INSTRUCTION_VAR_PREFIX,
    REPAIR_NOTE,
    RepairClaudeCode,
    repair_instruction,
)
from trajlab.capture.repair_launcher import INPUTS_DIRNAME, TRANSCRIPT_FILENAME, repair_job_config
from trajlab.capture.transcript import render
from trajlab.checkpoint.watcher import watcher_running
from trajlab.contracts import CHECKPOINT_RECORDS_FILENAME, CHECKPOINTS_DIRNAME, CheckpointRecord

TASK = "hello-world/hello-world"
MODEL = "anthropic/claude-sonnet-5-5"
JOBS_DIR = Path("corpus/jobs")
MANIFESTS_DIR = Path("corpus/manifests")
STORAGE = "/srv/trajlab/jobs"
FIXTURE = Path("tests/fixtures/hello-world-trial/agent/trajectory.json")
TASK_CACHE = Path.home() / ".cache/harbor/tasks/packages"
TARGET_BYTES = 200 * 1024
MAX_ARG_STRLEN = 32 * 4096  # Linux: bytes per argv or envp string, NUL included
NOBODY = "65534:65534"
# Same model, effort, version, and hooks as the round 1 configs; the agent cap is hello-world's
# 120 s doubled, since a 200 KiB first turn is slower than the task itself.
SOURCE_CONFIG: dict[str, Any] = {
    "agents": [
        {
            "model_name": MODEL,
            "kwargs": {"reasoning_effort": "medium", "version": CLAUDE_CODE_VERSION},
        }
    ],
    "tasks": [{"name": TASK, "ref": "latest"}],
    "agent_timeout_multiplier": 2.0,
}
LINE = (
    "line {i}: synthetic output for the traj-text delivery check, with text a shell must not "
    "touch: café 東京 `echo $HOME` \"double\" 'single' %s \\n $(date) * ~ $((1+1))\r\n"
    "progress 10%\r50%\r100%\n"
)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def synthetic_transcript(instruction: str) -> str:
    """A made-up failed attempt rendered by the real renderer: Bash calls whose outputs hold
    carriage returns, non-ASCII text, and shell metacharacters; every 7th is long enough to be
    cut. Each result carries the text the agent saw and, as Harbor's ATIF does, a longer
    `content` with a `[stdout]` copy, which the renderer must not use."""
    data = json.loads(FIXTURE.read_text())
    steps: list[dict[str, Any]] = [{"step_id": 1, "source": "user", "message": instruction}]
    while True:
        n = len(steps)
        output = "".join(LINE.format(i=i) for i in range(70 if n % 7 == 0 else 16))
        call_id = f"toolu_synthetic_{n:04d}"
        raw = {"tool_use_id": call_id, "type": "tool_result", "content": output}
        result = {
            "source_call_id": call_id,
            "content": f"{output.strip()}\n\n[stdout]\n{output}",
            "extra": {"tool_result_metadata": {"raw_tool_result": raw}},
        }
        steps.append(
            {
                "step_id": n + 1,
                "source": "agent",
                "message": f"Step {n}: reading the log." if n % 3 == 0 else "",
                "tool_calls": [
                    {
                        "tool_call_id": call_id,
                        "function_name": "Bash",
                        "arguments": {"command": f"cat /app/log-{n}.txt"},
                    }
                ],
                "observation": {"results": [result]},
            }
        )
        data["steps"] = steps
        text, stats = render(Trajectory.model_validate(data))
        if stats.bytes >= TARGET_BYTES:
            assert "[stdout]" not in text
            return text


def task_instruction(trial_dir: Path | None) -> str:
    """The instruction Harbor passes for hello-world: its instruction.md without the canary.

    With a trial, from the task digest the trial locked; before one, from the newest cached copy.
    """
    if trial_dir is not None:
        digest = json.loads((trial_dir / "lock.json").read_text())["task"]["digest"]
        path = TASK_CACHE / TASK / digest.removeprefix("sha256:") / "instruction.md"
    else:
        cached = sorted(
            (TASK_CACHE / TASK).glob("*/instruction.md"), key=lambda p: p.stat().st_mtime
        )
        if not cached:
            sys.exit(f"{TASK} is not in {TASK_CACHE}; run it once with harbor first")
        path = cached[-1]
    return strip_canary(path.read_text())


def first_user_message(trial_dir: Path) -> str | None:
    """The first user message of the trial's one native session; None without exactly one."""
    sessions = sorted((TrialPaths(trial_dir).agent_dir / "sessions/projects").glob("*/*.jsonl"))
    if len(sessions) != 1:
        return None
    (session,) = sessions
    for line in session.read_text().splitlines():
        event = json.loads(line)
        if event.get("type") != "user":
            continue
        content = event["message"]["content"]
        if isinstance(content, str):
            return content
        return "".join(block.get("text", "") for block in content if block.get("type") == "text")
    return None


def preflight(job: str, env_file: Path) -> None:
    """What `trajlab run` would refuse, checked before anything is written."""
    problems = []
    for path in (JOBS_DIR / job, JOBS_DIR / INPUTS_DIRNAME / job, MANIFESTS_DIR / f"{job}.json"):
        if path.exists():
            problems.append(f"{path} exists; pass a new --job")
    if not env_file.is_file():
        problems.append(f"no env file {env_file}: the trial would run unauthenticated")
    if not watcher_running(JOBS_DIR):
        problems.append(f"no watcher holds {JOBS_DIR}; `trajlab run` refuses a hooked config")
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if status.stdout.strip():
        problems.append("the working tree has uncommitted changes; `trajlab run` refuses")
    if problems:
        sys.exit("\n".join(problems))


def run_trial(job: str, transcript: str, env_file: Path) -> dict[str, Any]:
    """Part 1. Returns the numbers and pass/fail of each check."""
    preflight(job, env_file)
    inputs = JOBS_DIR / INPUTS_DIRNAME / job
    inputs.mkdir(parents=True)
    transcript_file = inputs / TRANSCRIPT_FILENAME
    transcript_file.write_bytes(transcript.encode("utf-8"))
    config = repair_job_config(
        SOURCE_CONFIG,
        task_name=TASK,
        arm="traj-text",
        job_name=job,
        jobs_dir=JOBS_DIR,
        attempts=1,
        checkpoint_image=None,
        session_file=None,
        transcript_file=transcript_file,
    )
    config_path = inputs / "config.json"
    config_path.write_text(json.dumps(config, indent=4) + "\n")
    trajlab = Path(sys.executable).parent / "trajlab"
    command = [str(trajlab), "run", str(config_path), "--corpus-id", job, "--storage", STORAGE]
    command += ["--env-file", str(env_file)]
    returncode = subprocess.run(command, check=False).returncode
    trials = list(iter_trial_dirs(JOBS_DIR / job)) if (JOBS_DIR / job).is_dir() else []
    if len(trials) != 1:
        return {
            "numbers": {"trajlab_run_exit": returncode, "trials": len(trials)},
            "checks": {"trial_finished": False},
        }
    (trial_dir,) = trials
    try:
        return trial_report(trial_dir, transcript, returncode)
    except Exception as error:  # a broken trial still gets a report
        return {
            "numbers": {"trajlab_run_exit": returncode, "trial": trial_dir.name},
            "checks": {"trial_report": False},
            "error": f"{type(error).__name__}: {error}",
        }


def trial_report(trial_dir: Path, transcript: str, returncode: int) -> dict[str, Any]:
    result_path = trial_dir / "result.json"
    result = json.loads(result_path.read_text()) if result_path.is_file() else {}
    prompt = repair_instruction(REPAIR_NOTE, task_instruction(trial_dir), transcript)
    received = first_user_message(trial_dir) or ""
    normalized = prompt.replace("\r\n", "\n").replace("\r", "\n")
    checkpoints = TrialPaths(trial_dir).agent_dir / CHECKPOINTS_DIRNAME
    records_path = checkpoints / CHECKPOINT_RECORDS_FILENAME
    records = [
        CheckpointRecord.model_validate_json(line)
        for line in (records_path.read_text().splitlines() if records_path.is_file() else [])
        if line.strip()
    ]
    requests = sorted(p.stem for p in checkpoints.glob("*.req"))
    acks = {p.stem for p in checkpoints.glob("*.ack")}
    leftovers = {}
    for record in records:
        listed = docker(
            "run", "--rm", "--entrypoint", "/bin/sh", record.checkpoint_id, "-c", "ls -1A /tmp"
        ).stdout.split()
        leftovers[record.seq] = [name for name in listed if name.startswith("trajlab-prompt-")]
    exception = result.get("exception_info")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    numbers = {
        "trajlab_run_exit": returncode,
        "trial": trial_dir.name,
        "result_json": result_path.is_file(),
        "exception": exception["exception_type"] if exception else None,
        "reward": rewards.get("reward"),
        "prompt_chars": len(prompt),
        "prompt_bytes": len(prompt.encode()),
        "prompt_carriage_returns": prompt.count("\r"),
        "env_string_bytes": len(f"{INSTRUCTION_VAR_PREFIX}{'0' * 32}=".encode())
        + len(prompt.encode())
        + 1,
        "max_arg_strlen": MAX_ARG_STRLEN,
        "first_user_message_chars": len(received),
        "first_user_message_bytes": len(received.encode()),
        "first_user_message_carriage_returns": received.count("\r"),
        "first_user_message_crlf": received.count("\r\n"),
        "prompt_crlf": prompt.count("\r\n"),
        "first_user_message_is_prompt_exactly": received == prompt,
        "first_user_message_is_prompt_with_cr_as_lf": received == normalized,
        "checkpoint_records": len(records),
        "checkpoint_images_checked": len(leftovers),
        "prompt_files_in_checkpoints": sum(len(v) for v in leftovers.values()),
        "requests": len(requests),
        "acks_for_requests": sum(name in acks for name in requests),
        "timeouts": len(list(checkpoints.glob("*.timeout"))),
        "final_checkpoint_image": max(records, key=lambda r: r.seq).checkpoint_id
        if records
        else None,
    }
    checks = {
        "trial_finished": returncode == 0 and bool(result) and exception is None,
        "prompt_over_the_argument_cap": numbers["env_string_bytes"] > MAX_ARG_STRLEN,
        "first_user_message_is_the_prompt": received == prompt,
        "no_prompt_file_in_any_checkpoint": bool(records) and not any(leftovers.values()),
        "checkpoint_records": bool(records),
        "every_req_has_an_ack": bool(requests) and set(requests) <= acks,
    }
    return {"numbers": numbers, "checks": checks}


class ContainerEnvironment:
    """Just enough of Harbor's BaseEnvironment for `RepairClaudeCode.exec_as_agent`: exec and
    upload in one running container. Upload is `docker cp -a`, which keeps the host file's uid,
    gid, and mode, as Harbor's `docker compose cp` (no --archive) was seen to do."""

    default_user: str | int | None = None

    def __init__(self, container: str) -> None:
        self.container = container

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        args = ["exec"] + (["-u", str(user)] if user is not None else [])
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        done = docker(*args, self.container, "bash", "-c", command, check=False)
        return ExecResult(stdout=done.stdout + done.stderr, return_code=done.returncode)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        docker("cp", "-a", str(source_path), f"{self.container}:{target_path}")


def check_ownership(image: str, prompt: str, transcript_file: Path) -> dict[str, Any]:
    """Part 2: the prompt reaches a non-root agent user whole, and its file is removed."""
    container = docker(
        "run", "-d", "--rm", "--user", NOBODY, "--entrypoint", "sleep", image, "infinity"
    ).stdout.strip()
    try:
        environment = ContainerEnvironment(container)
        agent = RepairClaudeCode(
            logs_dir=transcript_file.parent,
            model_name=MODEL,
            version=CLAUDE_CODE_VERSION,
            repair_transcript=str(transcript_file),
        )
        shell_var = f"harbor_claude_code_instruction_{uuid.uuid4().hex}"
        env_var = shell_var.upper()
        # Harbor's read of the prompt, as in ClaudeCode.run, then measure what the shell holds.
        command = (
            f'{shell_var}="${env_var}"; unset {env_var}; '
            f'printf "%s" "${shell_var}" | sha256sum; id -u; ls -1A /tmp'
        )
        found = asyncio.run(
            agent.exec_as_agent(environment, command, env={env_var: prompt})  # type: ignore[arg-type]
        )
        lines = (found.stdout or "").split("\n")
        received_sha = lines[0].split()[0]
        # Control: uploaded the same way but not chowned, the file is not the user's to remove.
        control = f"/tmp/trajlab-prompt-control-{uuid.uuid4().hex}.txt"
        docker("cp", "-a", str(transcript_file), f"{container}:{control}")
        owner = docker("exec", container, "stat", "-c", "%u:%g %a", control).stdout.strip()
        removed = docker("exec", container, "rm", "-f", control, check=False)
        still_there = docker("exec", container, "test", "-e", control, check=False).returncode == 0
    finally:
        docker("rm", "-f", container, check=False)
    numbers = {
        "image": image,
        "agent_uid": lines[1].strip(),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "received_sha256": received_sha,
        "tmp_after": [name for name in lines[2:] if name],
        "control_owner_mode": owner,
        "control_rm_exit": removed.returncode,
        "control_rm_stderr": removed.stderr.strip(),
    }
    checks = {
        "agent_user_is_not_root": numbers["agent_uid"] not in ("", "0"),
        "agent_user_got_every_byte": received_sha == numbers["prompt_sha256"],
        "prompt_file_removed": not any(
            n.startswith("trajlab-prompt-") for n in numbers["tmp_after"]
        ),
        "control_uploaded_file_not_removable": removed.returncode != 0 and still_there,
    }
    return {"numbers": numbers, "checks": checks}


def report(part: str, outcome: dict[str, Any]) -> list[str]:
    print(json.dumps({part: outcome}, indent=2), flush=True)
    return [f"{part}.{name}" for name, ok in outcome["checks"].items() if not ok]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job", default="hello-world-traj-text-delivery-v1")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--skip-trial", action="store_true", help="Run only the ownership part.")
    parser.add_argument("--image", help="Image for the ownership part; default: the trial's.")
    args = parser.parse_args()

    instruction = task_instruction(None)
    transcript = synthetic_transcript(instruction)
    print(
        json.dumps(
            {"transcript_chars": len(transcript), "transcript_bytes": len(transcript.encode())}
        ),
        flush=True,
    )
    failed: list[str] = []
    image = args.image
    if not args.skip_trial:
        trial = run_trial(args.job, transcript, args.env_file)
        failed += report("trial", trial)
        image = image or trial["numbers"].get("final_checkpoint_image")
    if image is None:
        sys.exit("no image for the ownership part: pass --image")
    prompt = repair_instruction(REPAIR_NOTE, instruction, transcript)
    with tempfile.TemporaryDirectory(prefix="trajlab-delivery-check-") as scratch:
        transcript_file = Path(scratch) / TRANSCRIPT_FILENAME
        transcript_file.write_bytes(transcript.encode("utf-8"))
        failed += report("ownership", check_ownership(image, prompt, transcript_file))
    if failed:
        print(f"FAILED: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
