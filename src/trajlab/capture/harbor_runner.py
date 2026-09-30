"""Run one Harbor job from a committed config file, then record its corpus manifest.

Credentials stay in `.env`, which Harbor loads itself via `--env-file`; this module never
reads, copies, or logs them.
"""

import json
import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig
from pydantic import ValidationError

from trajlab.capture.corpus import (
    ManifestError,
    RepoState,
    build_manifest,
    manifest_path,
    repo_root,
    repo_state,
    write_manifest,
)

logger = logging.getLogger(__name__)


class RunRefusedError(RuntimeError):
    """A precondition for a corpus run does not hold; nothing was started."""


@dataclass(frozen=True)
class RunPlan:
    """Everything decided before Harbor starts."""

    config_path: Path
    config_repo_path: str
    job_dir: Path
    corpus_id: str
    manifest_path: Path
    repo: RepoState
    command: list[str]


def claude_settings(config: JobConfig) -> list[dict[str, Any]]:
    """The Claude Code settings each agent passes via `kwargs.config` (Harbor: `--ak config=`).

    Harbor accepts a path, relative to its working directory (the same as ours), or an inline
    object. Raises RunRefusedError if a named file cannot be read.
    """
    found = []
    for agent in config.agents:
        source = agent.kwargs.get("config")
        if source is None:
            continue
        if isinstance(source, dict):
            found.append(source)
            continue
        try:
            settings = json.loads(Path(source).read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RunRefusedError(f"cannot read agent settings {source}: {error}") from error
        if not isinstance(settings, dict):
            raise RunRefusedError(f"agent settings {source} is not a JSON object")
        found.append(settings)
    return found


def enables_hooks(config: JobConfig) -> bool:
    """True if any agent's settings register a Claude Code hook, i.e. the run needs a watcher."""
    return any(settings.get("hooks") for settings in claude_settings(config))


def harbor_executable() -> Path:
    """The `harbor` script installed next to this interpreter: the pinned version."""
    return Path(sys.executable).parent / "harbor"


def plan_run(
    config_path: Path,
    *,
    repo_dir: Path,
    corpus_id: str | None,
    manifests_dir: Path,
    env_file: Path | None,
    allow_dirty: bool,
    watcher_running: Callable[[Path], bool],
) -> RunPlan:
    """Check every precondition and build the `harbor run` command. Starts nothing.

    `repo_dir` is inside this repo (normally the cwd); the config must be committed in it.
    `watcher_running(jobs_dir)` says whether a checkpoint watcher holds that jobs dir; a config
    that enables hooks is refused without one, since every tool call would wait out the hook's
    ack budget and record nothing (docs/checkpoint-protocol.md).
    """
    try:
        config = JobConfig.model_validate_json(config_path.read_text())
    except (OSError, ValidationError) as error:
        raise RunRefusedError(f"cannot load job config {config_path}: {error}") from error
    corpus_id = corpus_id or config_path.stem
    job_dir = config.jobs_dir / config.job_name
    # Harbor resumes a job whose dir already holds a result.json; a corpus run must start fresh.
    if job_dir.exists():
        raise RunRefusedError(f"{job_dir} exists; pick a new job_name in {config_path}")
    if enables_hooks(config) and not watcher_running(config.jobs_dir):
        raise RunRefusedError(
            f"{config_path} enables Claude Code hooks but no watcher holds {config.jobs_dir}; "
            f"start `uv run trajlab watch {config.jobs_dir}` first"
        )
    manifest = manifest_path(manifests_dir, corpus_id)
    if manifest.exists():
        raise RunRefusedError(f"{manifest} exists; corpus_id {corpus_id!r} is taken")
    try:
        root = repo_root(repo_dir)
        repo = repo_state(root)
    except ManifestError as error:
        raise RunRefusedError(str(error)) from error
    if not config_path.resolve().is_relative_to(root):
        raise RunRefusedError(f"{config_path} is not inside the repo at {root}")
    if repo.dirty and not allow_dirty:
        raise RunRefusedError(
            "working tree has uncommitted changes; the manifest's repo_sha would not describe "
            "the code that ran. Commit first, or pass --allow-dirty for a throwaway run."
        )
    if env_file is not None and not env_file.is_file():
        raise RunRefusedError(f"env file {env_file} not found")
    harbor = harbor_executable()
    if not harbor.is_file():
        raise RunRefusedError(f"{harbor} not found; run `uv sync`")
    command = [str(harbor), "run", "--config", str(config_path)]
    if env_file is not None:
        command += ["--env-file", str(env_file)]
    return RunPlan(
        config_path=config_path,
        config_repo_path=config_path.resolve().relative_to(root).as_posix(),
        job_dir=job_dir,
        corpus_id=corpus_id,
        manifest_path=manifest,
        repo=repo,
        command=command,
    )


def run_harbor(command: list[str]) -> int:
    """Run Harbor in the foreground with this terminal's stdio; return its exit code."""
    return subprocess.run(command, check=False).returncode


def execute(plan: RunPlan, *, storage: str | None) -> int:
    """Run Harbor in the foreground, then write the manifest. Returns Harbor's exit code.

    The manifest is written only when Harbor exits 0 (trial-level failures still exit 0); the
    repo state is the one checked before the run.
    """
    logger.info("running: %s", " ".join(plan.command))
    returncode = run_harbor(plan.command)
    if returncode != 0:
        logger.error("harbor exited %d; no manifest written for %s", returncode, plan.job_dir)
        return returncode
    try:
        manifest = build_manifest(
            [plan.job_dir],
            corpus_id=plan.corpus_id,
            repo=plan.repo,
            config_path=plan.config_repo_path,
            storage=storage,
        )
        path = write_manifest(manifest, plan.manifest_path.parent, overwrite=False)
    except ManifestError:
        logger.exception("harbor finished but no manifest was written for %s", plan.job_dir)
        return 1
    logger.info("wrote manifest %s", path)
    return 0
