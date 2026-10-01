"""Manual end-to-end check (needs Docker, no API): hook, watcher, and docker_commit with every-N.

Builds a jobs dir with one trial dir shaped like Harbor's, starts a Compose project named the way
Harbor names it with the trial's agent/ dir bind-mounted at /logs/agent, runs the real hook
inside the container for three file-writing calls, and runs `trajlab watch --every 2`.
Then tears the project down the way Harbor does and checks the checkpoint survives.

    uv run python scripts/2026-09-30_watcher_every_n_check.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from trajlab.capture.discover import compose_project_name
from trajlab.checkpoint.hook import hook_script
from trajlab.contracts import CheckpointRecord

REPO = Path(__file__).resolve().parent.parent
TRIAL_NAME = "every-n-check__Ab12Cd3"


def sh(*args: str, **kwargs: object) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="trajlab-every-n-"))
    jobs = work / "jobs"
    trial = jobs / "check-job" / TRIAL_NAME
    (trial / "agent").mkdir(parents=True)
    config = json.loads((REPO / "tests/fixtures/hello-world-trial/config.json").read_text())
    config["trial_name"] = TRIAL_NAME
    (trial / "config.json").write_text(json.dumps(config))
    project = compose_project_name(trial)
    compose = work / "compose.yaml"
    compose.write_text(
        "services:\n  main:\n    image: alpine:3.20\n    command: sleep 600\n"
        f"    volumes:\n      - {trial / 'agent'}:/logs/agent\n"
    )
    up = ["docker", "compose", "-p", project, "-f", str(compose)]
    watcher = None
    try:
        sh(*up, "up", "-d")
        watcher = subprocess.Popen(
            ["uv", "run", "trajlab", "watch", str(jobs), "--every", "2", "--sweep-interval", "0.5"],
            cwd=REPO,
        )
        time.sleep(3)
        container = sh(*up, "ps", "-q", "main").strip()
        for i in (1, 2, 3):
            event = json.dumps(
                {"session_id": "s", "tool_name": "Bash", "tool_use_id": f"toolu_0{i}"}
            )
            start = time.monotonic()
            sh(
                "docker", "exec", "-i", "-e", "CLAUDE_CONFIG_DIR=/logs/agent/sessions", container,
                "sh", "-c", f"echo {i} > /state-{i} && sh -c \"$0\"", hook_script(),
                input=event,
            )  # fmt: skip
            print(f"call {i}: hook released after {time.monotonic() - start:.1f} s")

        checkpoints = trial / "agent" / "checkpoints"
        records = [
            CheckpointRecord.model_validate_json(line)
            for line in (checkpoints / "checkpoints.jsonl").read_text().splitlines()
        ]
        assert [r.covered_tool_call_ids for r in records] == [("toolu_01", "toolu_02")], records
        assert json.loads((checkpoints / "toolu_01.ack").read_text())["deferred"] is True
        assert json.loads((checkpoints / "toolu_03.ack").read_text())["deferred"] is True
        assert not list(checkpoints.glob("*.timeout"))
        image = records[0].checkpoint_id
        files = sh("docker", "run", "--rm", image, "sh", "-c", "ls / | grep state- || true")
        assert files.split() == ["state-1", "state-2"], files

        sh(*up, "down", "--rmi", "local", "--volumes", "--remove-orphans")
        sh("docker", "image", "inspect", image)  # survived Harbor-style teardown
        print(f"checkpoint {image[:19]}: {records[0].capture_ms} ms, {records[0].bytes} bytes")
        print("every-N check passed")
        sh("docker", "image", "rm", "--force", image)
        return 0
    finally:
        if watcher is not None:
            watcher.terminate()
            watcher.wait(timeout=10)
        subprocess.run([*up, "down", "--volumes"], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
