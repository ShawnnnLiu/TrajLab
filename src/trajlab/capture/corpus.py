"""Build and write corpus manifests from finished Harbor job dirs.

A manifest records the inputs every trial in a corpus shares. Those are read from each job's
`lock.json`, which holds what Harbor resolved and ran, not what the config asked for. Jobs
that disagree on any shared input are refused: they are not one corpus.
"""

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig
from harbor.models.job.lock import JobLock, TrialLock
from harbor.models.job.result import JobResult
from pydantic import ValidationError

from trajlab.capture.discover import TrialNotFinishedError, iter_trial_dirs, trial_record
from trajlab.contracts import CorpusJob, CorpusManifest, CorpusTask

# Harbor's Job hardcodes these names (harbor/job.py, _job_config_path etc.).
JOB_CONFIG_FILENAME = "config.json"
JOB_LOCK_FILENAME = "lock.json"
JOB_RESULT_FILENAME = "result.json"


class ManifestError(ValueError):
    """The job dirs cannot be described by one corpus manifest."""


@dataclass(frozen=True)
class RepoState:
    """This repo's git HEAD and whether the working tree differs from it."""

    sha: str
    dirty: bool


def _git(repo_dir: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as error:
        raise ManifestError(f"git {' '.join(args)} failed in {repo_dir}: {error.stderr}") from error
    return completed.stdout.strip()


def repo_root(repo_dir: Path) -> Path:
    """The top level of the git repo containing `repo_dir`."""
    return Path(_git(repo_dir, "rev-parse", "--show-toplevel"))


def repo_state(repo_dir: Path) -> RepoState:
    """Read git HEAD and dirtiness of the repo containing `repo_dir`."""
    return RepoState(
        sha=_git(repo_dir, "rev-parse", "HEAD"),
        dirty=bool(_git(repo_dir, "status", "--porcelain")),
    )


@dataclass(frozen=True)
class _Job:
    dir: Path
    config: JobConfig
    config_raw: dict[str, Any]
    lock: JobLock
    result: JobResult


def _load_job(job_dir: Path) -> _Job:
    names = (JOB_CONFIG_FILENAME, JOB_LOCK_FILENAME, JOB_RESULT_FILENAME)
    missing = [name for name in names if not (job_dir / name).is_file()]
    if missing:
        raise ManifestError(f"{job_dir}: not a Harbor job dir, missing {', '.join(missing)}")
    config_text = (job_dir / JOB_CONFIG_FILENAME).read_text()
    current = JOB_CONFIG_FILENAME
    try:
        config = JobConfig.model_validate_json(config_text)
        current = JOB_LOCK_FILENAME
        lock = JobLock.model_validate_json((job_dir / JOB_LOCK_FILENAME).read_text())
        current = JOB_RESULT_FILENAME
        result = JobResult.model_validate_json((job_dir / JOB_RESULT_FILENAME).read_text())
    except ValidationError as error:
        raise ManifestError(f"{job_dir / current}: {error}") from error
    return _Job(
        dir=job_dir, config=config, config_raw=json.loads(config_text), lock=lock, result=result
    )


def _shared_inputs(trial: TrialLock) -> dict[str, Any]:
    """The per-trial inputs that must be identical across a corpus."""
    environment = trial.environment
    return {
        "agent_name": trial.agent.name or trial.agent.import_path,
        "model_name": trial.agent.model_name,
        "agent_kwargs": trial.agent.kwargs,
        "environment_type": (
            environment.type.value if environment.type is not None else environment.import_path
        ),
        "timeout_multiplier": trial.timeout_multiplier,
        "agent_timeout_multiplier": trial.agent_timeout_multiplier,
    }


def _single_value(field: str, values: dict[str, Any]) -> Any:
    """Return the one value every source agrees on, or raise naming who holds which value."""
    by_value: dict[str, list[str]] = {}
    for source, value in values.items():
        by_value.setdefault(json.dumps(value, sort_keys=True, default=str), []).append(source)
    if len(by_value) != 1:
        detail = "; ".join(f"{value} in {', '.join(srcs)}" for value, srcs in by_value.items())
        raise ManifestError(f"jobs disagree on {field}: {detail}")
    return next(iter(values.values()))


def _corpus_job(job: _Job) -> CorpusJob:
    trials = []
    for trial_dir in iter_trial_dirs(job.dir):
        try:
            record = trial_record(trial_dir)
        except TrialNotFinishedError as error:
            raise ManifestError(str(error)) from error
        except ValidationError as error:
            raise ManifestError(f"{trial_dir}: {error}") from error
        if record.job_id != job.result.id:
            raise ManifestError(
                f"{trial_dir}: job_id {record.job_id} != {job.result.id} in {job.dir}/result.json"
            )
        trials.append(record)
    if not trials:
        raise ManifestError(f"{job.dir}: no trial dirs")
    return CorpusJob(
        job_name=job.dir.name, job_id=job.result.id, job_config=job.config_raw, trials=trials
    )


def _tasks(jobs: list[_Job]) -> list[CorpusTask]:
    digests: dict[str, set[str]] = {}
    for job in jobs:
        for trial in job.lock.trials:
            digests.setdefault(trial.task.name, set()).add(trial.task.digest)
    conflicting = {name: sorted(d) for name, d in digests.items() if len(d) > 1}
    if conflicting:
        raise ManifestError(f"tasks resolved to more than one digest: {conflicting}")
    return [CorpusTask(name=name, digest=d.pop()) for name, d in sorted(digests.items())]


def build_manifest(
    job_dirs: list[Path],
    *,
    corpus_id: str,
    repo: RepoState,
    config_path: str | None = None,
    storage: str | None = None,
    created_at: datetime | None = None,
) -> CorpusManifest:
    """Describe finished job dirs as one corpus. Raises `ManifestError` if they are not one."""
    if not job_dirs:
        raise ManifestError("no job dirs given")
    jobs = [_load_job(job_dir) for job_dir in job_dirs]

    per_trial: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if len(job.config.agents) != 1:
            raise ManifestError(f"{job.dir}: expected one agent, found {len(job.config.agents)}")
        if not job.lock.trials:
            raise ManifestError(f"{job.dir}: lock.json lists no trials")
        for index, trial in enumerate(job.lock.trials):
            per_trial[f"{job.dir.name}/lock.json trials[{index}]"] = _shared_inputs(trial)
    shared = {
        field: _single_value(field, {src: inputs[field] for src, inputs in per_trial.items()})
        for field in next(iter(per_trial.values()))
    }

    harbor_version = _single_value(
        "harbor_version",
        {job.dir.name: job.lock.harbor.version or version("harbor") for job in jobs},
    )
    n_attempts = _single_value("n_attempts", {job.dir.name: job.config.n_attempts for job in jobs})

    return CorpusManifest(
        corpus_id=corpus_id,
        created_at=created_at or datetime.now(UTC),
        harbor_version=harbor_version,
        repo_sha=repo.sha,
        repo_dirty=repo.dirty,
        config_path=config_path,
        n_attempts=n_attempts,
        tasks=_tasks(jobs),
        jobs=[_corpus_job(job) for job in jobs],
        storage=storage,
        **shared,
    )


def manifest_path(manifests_dir: Path, corpus_id: str) -> Path:
    return manifests_dir / f"{corpus_id}.json"


def write_manifest(manifest: CorpusManifest, manifests_dir: Path, *, overwrite: bool) -> Path:
    """Write `<manifests_dir>/<corpus_id>.json`. Refuses to replace one unless `overwrite`."""
    path = manifest_path(manifests_dir, manifest.corpus_id)
    if path.exists() and not overwrite:
        raise ManifestError(f"{path} exists; a corpus_id names one corpus")
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n")
    return path
