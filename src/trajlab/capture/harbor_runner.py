"""Run one Harbor job from a committed config file, then record its corpus manifest.

Credentials stay in `.env`, which Harbor loads itself via `--env-file`; this module never
reads, copies, or logs them.
"""

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
) -> RunPlan:
    """Check every precondition and build the `harbor run` command. Starts nothing.

    `repo_dir` is inside this repo (normally the cwd); the config must be committed in it.
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
