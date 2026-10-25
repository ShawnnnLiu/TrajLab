"""Manual end-to-end check (needs Docker, no API): a call whose `docker commit` fails keeps its
changes when the sweep retries it.

Starts a Compose project shaped like a Harbor trial from a pre-installed image (or any image with
GNU find) and runs `trajlab watch --gate change --every 1` with a `docker` shim first on its PATH.
The shim fails the second `docker commit` it sees, once, as a full disk or a busy daemon would,
and hands every other command to the real docker. The real hook fires after each of three calls:
a baseline write, a write whose checkpoint commit fails and is retried by the sweep, and one more
write. The check passes if every call is acked with a checkpoint and each call's
`changed_paths` lists exactly the file it wrote.

    uv run python scripts/2026-10-25_retried_commit_check.py [image]
"""

import argparse
import json
import os
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
TRIAL_NAME = "retried-commit-check__Rc4Xy01"
FAIL_COMMIT = 2  # the second commit is the second call's checkpoint

# (command, the changed_paths its ack must list)
CALLS = [
    ("mkdir -p /app && echo seed > /app/seed.txt", None),  # baseline: no previous listing
    ("echo retried > /app/retried.txt", ("+/app/retried.txt",)),
    ("echo after > /app/after.txt", ("+/app/after.txt",)),
]

SHIM = """#!/bin/sh
if [ "$1" = commit ]; then
  n=$(( $(cat '{counter}' 2>/dev/null || echo 0) + 1 ))
  echo "$n" > '{counter}'
  if [ "$n" = {fail} ]; then
    echo "injected failure of commit $n" >&2
    exit 1
  fi
fi
exec '{docker}' "$@"
"""


def sh(*args: str, **kwargs: object) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


def main(image: str) -> int:
    work = Path(tempfile.mkdtemp(prefix="trajlab-retried-commit-"))
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
    shim_dir = work / "bin"
    shim_dir.mkdir()
    counter = work / "commits"
    docker = shutil.which("docker")
    assert docker is not None
    (shim_dir / "docker").write_text(SHIM.format(counter=counter, fail=FAIL_COMMIT, docker=docker))
    (shim_dir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim_dir}:{os.environ['PATH']}"}
    up = ["docker", "compose", "-p", project, "-f", str(compose)]
    watcher = None
    failures = 0
    try:
        sh(*up, "up", "-d")
        watcher = subprocess.Popen(
            ["uv", "run", "trajlab", "watch", str(jobs), "--every", "1", "--gate", "change",
             "--sweep-interval", "0.5"],
            cwd=REPO,
            env=env,
        )  # fmt: skip
        time.sleep(3)
        container = sh(*up, "ps", "-q", "main").strip()
        checkpoints = trial / "agent" / "checkpoints"
        for i, (command, expected) in enumerate(CALLS, start=1):
            tool_use_id = f"toolu_{i:02d}"
            event = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_use_id": tool_use_id})
            started = time.monotonic()
            sh(
                "docker", "exec", "-i", "-e", "CLAUDE_CONFIG_DIR=/logs/agent/sessions", container,
                "sh", "-c", f'{command}; sh -c "$0"', hook_script(),
                input=event,
            )  # fmt: skip
            waited = time.monotonic() - started
            ack = checkpoints / f"{tool_use_id}.ack"
            if not ack.is_file():
                failures += 1
                print(f"BAD {i} no ack after {waited:.1f} s")
                continue
            call = CallRecord.model_validate_json(ack.read_text())
            paths_ok = expected is None or call.changed_paths == expected
            ok = call.outcome == "checkpoint" and paths_ok
            failures += not ok
            print(
                f"{'ok ' if ok else 'BAD'} {i} {call.outcome:10s} {call.change:9s} "
                f"seq {call.checkpoint_seq} in {waited:.1f} s: changed_paths "
                f"{list(call.changed_paths)} (total {call.changed_paths_total}), "
                f"expected {list(expected) if expected is not None else 'any'}"
            )
        commits = int(counter.read_text()) if counter.is_file() else 0
        records = [
            CheckpointRecord.model_validate_json(line)
            for line in (checkpoints / "checkpoints.jsonl").read_text().splitlines()
        ]
        ok = commits == len(CALLS) + 1 and len(records) == len(CALLS)
        failures += not ok
        print(
            f"{'ok ' if ok else 'BAD'} {commits} commits ({FAIL_COMMIT} failed), "
            f"{len(records)} checkpoints for {len(CALLS)} calls"
        )
        for record in records:
            sh("docker", "image", "rm", "--force", record.checkpoint_id)
    finally:
        if watcher is not None:
            watcher.terminate()
            watcher.wait(timeout=10)
        subprocess.run([*up, "down", "--volumes"], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    print("retried-commit check passed" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?")
    args = parser.parse_args()
    default = sh("docker", "images", "trajlab-preinstalled", "--format", "{{.Repository}}:{{.Tag}}")
    sys.exit(main(args.image or default.split()[0]))
