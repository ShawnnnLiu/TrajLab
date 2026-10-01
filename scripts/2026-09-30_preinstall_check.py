"""Manual end-to-end check (needs Docker, no API): the pre-installed agent environment.

For one task from Harbor's local task cache, drives `PreinstalledDockerEnvironment` the way a
Harbor trial does, without running the agent:

1. builds the derived image (or reuses it), then asks again and expects a cache hit;
2. checks the derived image keeps the task image's CMD and carries provenance labels;
3. starts a real trial container through Harbor's start path and checks it runs the derived image;
4. runs Harbor's own `ClaudeCode.setup` against the trial container and checks it installs
   nothing: the container's writable layer stays small;
5. tears down the way Harbor does (`compose down --rmi local`) and checks the derived image
   survives.

    uv run python scripts/2026-09-30_preinstall_check.py hello-world/hello-world
    uv run python scripts/2026-09-30_preinstall_check.py path/to/task-dir
    DOCKER_DEFAULT_PLATFORM=linux/amd64 uv run python scripts/2026-09-30_preinstall_check.py \\
        terminal-bench/regex-chess
"""

import asyncio
import json
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.models.task.config import TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.trial.paths import TrialPaths

from trajlab.capture.discover import compose_project_name
from trajlab.capture.preinstall import PreinstalledDockerEnvironment
from trajlab.contracts import PREINSTALL_RECORD_FILENAME, PreinstallRecord

VERSION = "2.1.278"
CACHE = Path.home() / ".cache/harbor/tasks/packages"
# The install is about 1.3 GB; anything near that means Harbor installed at trial time.
MAX_TRIAL_LAYER_BYTES = 50_000_000


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def task_dir(name: str) -> Path:
    """A task in Harbor's cache by name, or a task directory given by path."""
    if Path(name).is_dir():
        return Path(name).resolve()
    candidates = sorted((CACHE / name).glob("*/task.toml"))
    if not candidates:
        sys.exit(f"{name} is not in {CACHE}; run it once with harbor first")
    return candidates[-1].parent


def writable_layer(container: str) -> int:
    return int(docker("container", "inspect", "--size", "--format", "{{.SizeRw}}", container))


async def check(name: str) -> None:
    task = task_dir(name)
    task_config = TaskConfig.model_validate_toml(TaskPaths(task).config_path.read_text())
    # Cached tasks live in <name>/<digest>/; a task given by path is named by its directory.
    short_name = task.name if Path(name).is_dir() else task.parent.name
    trial_name = f"{short_name}__Pre1nst"
    work = Path(tempfile.mkdtemp(prefix="trajlab-preinstall-"))
    paths = TrialPaths(work / trial_name)
    paths.mkdir()
    paths.config_path.write_text(
        json.dumps(
            {
                "task": {"name": name, "ref": "latest"},
                "trial_name": trial_name,
                "agent": {"name": "claude-code", "kwargs": {"version": VERSION}},
            }
        )
    )

    def environment() -> PreinstalledDockerEnvironment:
        return PreinstalledDockerEnvironment(
            environment_dir=TaskPaths(task).environment_dir,
            environment_name=short_name,
            session_id=f"{trial_name}__env",
            trial_paths=paths,
            task_env_config=task_config.environment,
        )

    print(f"== {name}")
    start = time.monotonic()
    record = await environment().prepare_image(force_build=False)
    print(f"1. {record.image} cache_hit={record.cache_hit} in {time.monotonic() - start:.0f} s")
    again = await environment().prepare_image(force_build=False)
    assert again.cache_hit and again.image_id == record.image_id, again
    print("   second request: cache hit, same image id")

    task_cmd = docker("image", "inspect", "--format", "{{json .Config.Cmd}}", record.task_image)
    derived_cmd = docker("image", "inspect", "--format", "{{json .Config.Cmd}}", record.image)
    assert task_cmd == derived_cmd, (task_cmd, derived_cmd)
    labels = json.loads(
        docker("image", "inspect", "--format", "{{json .Config.Labels}}", record.image)
    )
    assert labels["trajlab.preinstall.agent_version"] == VERSION, labels
    print(f"2. CMD kept ({task_cmd.strip()}), provenance labels present")

    env = environment()
    await env.start(force_build=False)
    try:
        written = PreinstallRecord.model_validate_json(
            (paths.trial_dir / PREINSTALL_RECORD_FILENAME).read_text()
        )
        assert written.image == record.image and written.cache_hit
        project = compose_project_name(paths.trial_dir)
        container = docker(
            "ps", "--filter", f"label=com.docker.compose.project={project}",
            "--filter", "label=com.docker.compose.service=main", "--format", "{{.ID}}",
        ).strip()  # fmt: skip
        image = docker("container", "inspect", "--format", "{{.Config.Image}}", container).strip()
        assert image == record.image, image
        print(f"3. trial container {container} runs {image}")

        before = writable_layer(container)
        agent = ClaudeCode(logs_dir=paths.agent_dir, version=VERSION)
        start = time.monotonic()
        with env.with_default_user(task_config.agent.user):
            await agent.setup(env)
        after = writable_layer(container)
        print(
            f"4. Harbor setup took {time.monotonic() - start:.1f} s; "
            f"writable layer {before} -> {after} bytes"
        )
        assert after < MAX_TRIAL_LAYER_BYTES, f"Harbor installed at trial time: {after} bytes"
    finally:
        await env.stop(delete=True)
    docker("image", "inspect", record.image)
    print("5. derived image survived compose down --rmi local")
    print(f"preinstall check passed for {name}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for task_name in sys.argv[1:] or ["hello-world/hello-world"]:
        asyncio.run(check(task_name))
