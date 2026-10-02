"""Manual end-to-end check (needs Docker, no API): change-gated checkpoints on real overlayfs.

Starts a Compose project shaped like a Harbor trial from a pre-installed image (or any image
with GNU find), runs `trajlab watch --gate change --every 1`, and fires the real hook after
each of a fixed list of shell commands. Each command is chosen to probe one detector rule
(ADR-0010); the script checks the watcher's verdict for every call.

    uv run python scripts/2026-09-30_change_gate_check.py [image]

With `--audit DIR` it runs under `--gate audit` instead (every call checkpointed, verdicts still
recorded) and keeps the jobs dir and images in DIR for `2026-09-30_audit_detector.py`.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from trajlab.capture.discover import compose_project_name
from trajlab.checkpoint.hook import hook_script
from trajlab.contracts import CallRecord, CheckpointRecord

REPO = Path(__file__).resolve().parent.parent
TRIAL_NAME = "change-gate-check__Gt7Ab12"

# (command, expected outcome, why)
CALLS = [
    ("mkdir -p /app && echo seed > /app/seed.txt", "checkpoint", "baseline: first measured call"),
    ("ls -la /app; cat /app/seed.txt; grep -r seed /app", "unchanged", "reads only"),
    ("echo data > /app/new.txt", "checkpoint", "creates a file"),
    ("echo data > /app/new.txt", "checkpoint", "rewrites identical bytes: change time moves"),
    ("touch -d 2000-01-01 /app/new.txt", "checkpoint", "back-dates mtime: change time still moves"),
    ("chmod 600 /app/new.txt", "checkpoint", "metadata only"),
    ("rm /app/new.txt", "checkpoint", "deletes a file"),
    ("echo x > /app/t && rm /app/t", "unchanged", "created and deleted within one call"),
    ("mkdir -p /tmp/claude-0/-app && echo o > /tmp/claude-0/-app/out", "unchanged", "harness path"),
    (
        "python3 -c 'print(1)' 2>/dev/null || true",
        "unchanged",
        "runs a program that writes nothing",
    ),
    ("mkdir /app/empty", "checkpoint", "creates an empty directory"),
]


def sh(*args: str, **kwargs: object) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


def main(image: str, audit_dir: Path | None) -> int:
    gate = "audit" if audit_dir else "change"
    work = audit_dir or Path(tempfile.mkdtemp(prefix="trajlab-change-gate-"))
    jobs = work / "jobs"
    trial = jobs / "check-job" / TRIAL_NAME
    (trial / "agent").mkdir(parents=True)
    config = json.loads((REPO / "tests/fixtures/hello-world-trial/config.json").read_text())
    config["trial_name"] = TRIAL_NAME
    (trial / "config.json").write_text(json.dumps(config))
    project = compose_project_name(trial)
    compose = work / "compose.yaml"
    compose.write_text(
        f"services:\n  main:\n    image: {image}\n    command: [sh, -c, 'sleep infinity']\n"
        f"    volumes:\n      - {trial / 'agent'}:/logs/agent\n"
    )
    up = ["docker", "compose", "-p", project, "-f", str(compose)]
    watcher = None
    failures = 0
    try:
        sh(*up, "up", "-d")
        watcher = subprocess.Popen(
            ["uv", "run", "trajlab", "watch", str(jobs), "--every", "1", "--gate", gate,
             "--sweep-interval", "0.5"],
            cwd=REPO,
        )  # fmt: skip
        time.sleep(3)
        container = sh(*up, "ps", "-q", "main").strip()
        checkpoints = trial / "agent" / "checkpoints"
        for i, (command, expected, why) in enumerate(CALLS, start=1):
            tool_use_id = f"toolu_{i:02d}"
            event = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_use_id": tool_use_id})
            sh(
                "docker", "exec", "-i", "-e", "CLAUDE_CONFIG_DIR=/logs/agent/sessions", container,
                "sh", "-c", f'{command}; sh -c "$0"', hook_script(),
                input=event,
            )  # fmt: skip
            call = CallRecord.model_validate_json((checkpoints / f"{tool_use_id}.ack").read_text())
            ok = call.outcome == (expected if gate == "change" else "checkpoint")
            failures += not ok
            print(
                f"{'ok ' if ok else 'BAD'} {i:2d} {call.outcome:10s} {call.change:9s} "
                f"{call.detect_ms} ms  {why}: {list(call.changed_paths)[:4]}"
            )
        records = [
            CheckpointRecord.model_validate_json(line)
            for line in (checkpoints / "checkpoints.jsonl").read_text().splitlines()
        ]
        expected_checkpoints = (
            sum(outcome == "checkpoint" for _, outcome, _ in CALLS)
            if gate == "change"
            else len(CALLS)
        )
        print(
            f"{len(records)} checkpoints for {len(CALLS)} calls (expected {expected_checkpoints})"
        )
        failures += len(records) != expected_checkpoints
        if audit_dir is None:
            for record in records:
                sh("docker", "image", "rm", "--force", record.checkpoint_id)
    finally:
        if watcher is not None:
            watcher.terminate()
            watcher.wait(timeout=10)
        subprocess.run([*up, "down", "--volumes"], capture_output=True)
        if audit_dir is None:
            shutil.rmtree(work, ignore_errors=True)
    print("change-gate check passed" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?")
    parser.add_argument("--audit", type=Path, help="run under --gate audit and keep results here")
    args = parser.parse_args()
    default = sh("docker", "images", "trajlab-preinstalled", "--format", "{{.Repository}}:{{.Tag}}")
    sys.exit(main(args.image or default.split()[0], args.audit))
