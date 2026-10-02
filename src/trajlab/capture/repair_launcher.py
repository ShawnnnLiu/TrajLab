"""Repair every failed trial of one or more source jobs under four arms (ADR-0012).

`trajlab repair` runs this. As each source trial finishes, a failed one is classified
(`classify_failure`), and if repairable, four repair jobs are queued, one per arm, each running
`attempts` repair trials. Each job is started with `trajlab run`, so it gets the same pins,
watcher check, and manifest as any corpus run.

The launcher is meant to run unattended for days, so it is restartable: a repair job whose dir
already exists is never started again. On start, and as jobs exit, a job that did not finish
(the launcher or machine died) or that has trials ended by a harness error is resumed with
`harbor jobs resume`, which reruns exactly those trials; the job's manifest is then rewritten.

Arms (state x conversation; every arm runs `RepairClaudeCode`, so the instruction starts with the
same note that an earlier attempt failed):

    fresh       task image, new conversation              (retry baseline)
    state       final checkpoint of the failed trial, new conversation
    state-traj  final checkpoint, the failed trial's full native session loaded (--resume)
    traj        task image, the failed trial's full native session loaded

A failure is repaired only if every arm can start: it has a final checkpoint whose image still
exists and exactly one native session. Otherwise no arm runs, so the arms stay paired.
"""

import json
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TimingInfo, TrialResult

from trajlab.capture.corpus import (
    ManifestError,
    build_manifest,
    repo_root,
    repo_state,
    write_manifest,
)
from trajlab.capture.discover import iter_trial_dirs, load_trial_config
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.contracts import (
    CHECKPOINT_ARMS,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    REPAIR_ARMS,
    REPAIR_SOURCE_FILENAME,
    REPAIRABLE_KINDS,
    SESSION_ARMS,
    CheckpointRecord,
    FailureKind,
    RepairArm,
    RepairSource,
)

logger = logging.getLogger(__name__)

HOOKS = "configs/claude-code/settings.hooks.json"
INPUTS_DIRNAME = "_repair-inputs"
REPAIR_AGENT = "trajlab.capture.repair:RepairClaudeCode"
PREINSTALLED_ENV = "trajlab.capture.preinstall:PreinstalledDockerEnvironment"
RESUME_ENV = "trajlab.capture.resume:CheckpointResumeEnvironment"
CHECKPOINT_REPOSITORY = "trajlab-checkpoint"
PID_FILENAME = "launcher.pid"
RESUMES_FILENAME = "resumes.json"

# Exceptions that end a trial because of the agent's own behavior: a failed attempt.
AGENT_EXCEPTIONS = frozenset(
    {
        "OutputTokenExceededError",
        "OutputLengthExceededError",
        "ContextLengthExceededError",
        "ContextWindowExceededError",
        "AgentSafetyRefusalError",
    }
)
# Exceptions that end a trial because the harness failed: not an attempt; rerun it.
INFRA_EXCEPTIONS = frozenset(
    {
        "CancelledError",
        "ApiUsageLimitError",
        "ApiRateLimitError",
        "ApiError",
        "ApiConnectionError",
        "ApiConnectionClosedError",
        "ApiInternalServerError",
        "ApiOverloadedError",
        "ApiResponseStalledError",
        "ApiKeyRejectedError",
        "ApiProviderResourceNotFoundError",
        "UnknownApiError",
        "AgentAuthenticationError",
        "AuthenticationError",
        "NotAuthenticatedError",
        "ModelNotFoundError",
        "AgentSetupTimeoutError",
        "EnvironmentStartTimeoutError",
        "VerifierTimeoutError",
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
        "DownloadVerifierDirError",
        "AddTestsDirError",
        "HealthcheckError",
        "NetworkConnectionError",
        "MemoryLimitExceededError",
        "SandboxBuildFailedError",
        "SandboxLikelyOutOfMemoryError",
        "PreinstallError",
    }
)
USAGE_LIMIT_EXCEPTION = "ApiUsageLimitError"


def trial_reward(result: TrialResult) -> float | None:
    rewards = result.verifier_result.rewards if result.verifier_result else None
    reward = (rewards or {}).get("reward")
    return float(reward) if reward is not None else None


def seconds(timing: TimingInfo | None) -> float | None:
    if timing is None or timing.started_at is None or timing.finished_at is None:
        return None
    return round((timing.finished_at - timing.started_at).total_seconds(), 1)


def classify_failure(result: TrialResult) -> FailureKind | None:
    """How a finished trial failed, or None if it passed (reward 1).

    NonZeroAgentExitCodeError is deliberately unclassified: Claude Code exits non-zero both for
    its own failures and for harness ones, so a person reads the log first.
    """
    reward = trial_reward(result)
    exception = result.exception_info.exception_type if result.exception_info else None
    if reward is not None and reward >= 1.0:
        return None
    if exception is None:
        return "ended_turn" if reward is not None else "infra_error"
    if exception == "AgentTimeoutError":
        return "timeout"
    if exception in AGENT_EXCEPTIONS:
        return "agent_error"
    if exception in INFRA_EXCEPTIONS:
        return "infra_error"
    return "unclassified"


def load_result(trial_dir: Path) -> TrialResult | None:
    path = TrialPaths(trial_dir).result_path
    if not path.is_file():
        return None
    return TrialResult.model_validate_json(path.read_text())


def final_checkpoint(trial_dir: Path) -> CheckpointRecord | None:
    """The trial's highest-seq checkpoint: its stop checkpoint if the final state was new."""
    path = TrialPaths(trial_dir).agent_dir / CHECKPOINTS_DIRNAME / CHECKPOINT_RECORDS_FILENAME
    if not path.is_file():
        return None
    records = [
        CheckpointRecord.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    return max(records, key=lambda r: r.seq, default=None)


def native_sessions(trial_dir: Path) -> list[Path]:
    return sorted((TrialPaths(trial_dir).agent_dir / "sessions/projects").glob("*/*.jsonl"))


def repair_job_name(prefix: str, trial_name: str, arm: RepairArm) -> str:
    return f"{prefix}-{trial_name}-{arm}"


def repair_job_config(
    source_job_config: dict[str, Any],
    *,
    task_name: str,
    arm: RepairArm,
    job_name: str,
    jobs_dir: Path,
    attempts: int,
    checkpoint_image: str | None,
    session_file: Path | None,
) -> dict[str, Any]:
    """The Harbor job config of one repair job: the source job's agent, model, and dataset."""
    source_agent = source_job_config["agents"][0]
    agent: dict[str, Any] = {
        "import_path": REPAIR_AGENT,
        "model_name": source_agent["model_name"],
        "kwargs": {
            **source_agent.get("kwargs", {}),
            "version": CLAUDE_CODE_VERSION,
            "config": HOOKS,
        },
    }
    if arm in SESSION_ARMS:
        if session_file is None:
            raise ValueError(f"arm {arm} needs a session file")
        agent["load_trajectory"] = str(session_file.resolve())
    environment: dict[str, Any] = {"import_path": PREINSTALLED_ENV}
    if arm in CHECKPOINT_ARMS:
        if checkpoint_image is None:
            raise ValueError(f"arm {arm} needs a checkpoint image")
        environment = {"import_path": RESUME_ENV, "kwargs": {"checkpoint_image": checkpoint_image}}
    config: dict[str, Any] = {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": attempts,
        "n_concurrent_trials": attempts,
        "agents": [agent],
        "environment": environment,
    }
    # The source job's own dataset entry (name and pinned ref), narrowed to this task.
    if source_job_config.get("datasets"):
        dataset = next(
            d
            for d in source_job_config["datasets"]
            if task_name in d.get("task_names", [task_name])
        )
        config["datasets"] = [dataset | {"task_names": [task_name]}]
    else:
        config["tasks"] = [next(t for t in source_job_config["tasks"] if t["name"] == task_name)]
    for key in ("agent_timeout_multiplier", "timeout_multiplier", "verifier_timeout_multiplier"):
        if key in source_job_config:
            config[key] = source_job_config[key]
    JobConfig.model_validate(config)
    return config


def job_result(job_dir: Path) -> JobResult | None:
    path = job_dir / "result.json"
    if not path.is_file():
        return None
    return JobResult.model_validate_json(path.read_text())


def job_finished(job_dir: Path) -> bool:
    result = job_result(job_dir)
    return result is not None and result.finished_at is not None


def finished_trials(job_dir: Path) -> list[tuple[Path, TrialResult]]:
    if not job_dir.is_dir():
        return []
    found = []
    for trial_dir in iter_trial_dirs(job_dir):
        result = load_result(trial_dir)
        if result is not None:
            found.append((trial_dir, result))
    return found


def source_reserved_slots(job_dir: Path) -> int:
    """Trial slots a source job may still occupy: none once finished."""
    if job_finished(job_dir):
        return 0
    config = json.loads((job_dir / "config.json").read_text())
    concurrent = int(config.get("n_concurrent_trials", 4))
    result = job_result(job_dir)
    if result is None:
        return concurrent
    return max(0, min(concurrent, result.n_total_trials - len(finished_trials(job_dir))))


def infra_failed(job_dir: Path) -> set[str]:
    """Exception types of this job's trials that a resume should rerun."""
    return {
        result.exception_info.exception_type
        for _, result in finished_trials(job_dir)
        if result.exception_info is not None and classify_failure(result) == "infra_error"
    }


def pid_alive(pid: int, needle: str) -> bool:
    """True if `pid` runs and its command line mentions `needle` (guards against pid reuse)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return needle.encode() in cmdline


def checkpoint_tag(image_id: str, runner: Callable[..., Any] = subprocess.run) -> str | None:
    """The `trajlab-checkpoint:` tag of a checkpoint image, or None if the image is gone.

    Repair passes the tag, not the id: Harbor's teardown removes untagged images it used
    (trajlab.capture.resume).
    """
    found = runner(
        ["docker", "image", "inspect", "--format", "{{json .RepoTags}}", image_id],
        capture_output=True,
        text=True,
    )
    if found.returncode != 0:
        return None
    tags = [t for t in json.loads(found.stdout) if t.startswith(f"{CHECKPOINT_REPOSITORY}:")]
    return tags[0] if tags else None


@dataclass
class RepairJob:
    name: str
    source: RepairSource
    config_path: Path


@dataclass
class Launcher:
    """One poll loop over source jobs and their repair jobs. `step()` does one pass."""

    source_jobs: list[Path]
    prefix: str
    attempts: int
    max_running: int
    jobs_dir: Path
    manifests_dir: Path
    env_file: Path | None
    hold_below_gb: float = 50.0
    usage_backoff_s: float = 1800.0
    max_resumes: int = 3
    watcher_running: Callable[[Path], bool] = lambda _: True
    tag_lookup: Callable[[str], str | None] = checkpoint_tag
    clock: Callable[[], float] = time.time
    pending: list[RepairJob] = field(default_factory=list)
    queued_trials: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)
    running: dict[str, subprocess.Popen[bytes] | int] = field(default_factory=dict)
    resume_due: dict[str, float] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    paused_until: float = 0.0

    @property
    def inputs_root(self) -> Path:
        return self.jobs_dir / INPUTS_DIRNAME

    # -- planning -----------------------------------------------------------------------------

    def plan_failure(self, trial_dir: Path, result: TrialResult) -> list[RepairJob] | str:
        """The four repair jobs for one failed trial, or why it is not repaired."""
        kind = classify_failure(result)
        if kind is None:
            return "passed"
        if kind not in REPAIRABLE_KINDS:
            exception = result.exception_info.exception_type if result.exception_info else None
            return f"{kind} ({exception})"
        record = final_checkpoint(trial_dir)
        if record is None:
            return "no checkpoint"
        tag = self.tag_lookup(record.checkpoint_id)
        if tag is None:
            return f"checkpoint image {record.checkpoint_id} is gone"
        sessions = native_sessions(trial_dir)
        if len(sessions) != 1:
            return f"expected one native session, found {len(sessions)}"
        source_config = json.loads((trial_dir.parent / "config.json").read_text())
        task_name = load_trial_config(trial_dir).task.name
        jobs = []
        for arm in REPAIR_ARMS:
            name = repair_job_name(self.prefix, result.trial_name, arm)
            inputs = self.inputs_root / name
            session_file = inputs / sessions[0].name if arm in SESSION_ARMS else None
            source = RepairSource(
                source_job=trial_dir.parent.name,
                source_trial=result.trial_name,
                task_name=task_name,
                failure_kind=kind,
                exception_type=(
                    result.exception_info.exception_type if result.exception_info else None
                ),
                source_reward=trial_reward(result),
                source_agent_s=seconds(result.agent_execution),
                arm=arm,
                attempts=self.attempts,
                checkpoint_seq=record.seq if arm in CHECKPOINT_ARMS else None,
                checkpoint_image=tag if arm in CHECKPOINT_ARMS else None,
                checkpoint_image_id=record.checkpoint_id if arm in CHECKPOINT_ARMS else None,
                session_file=str(session_file) if session_file else None,
                recorded_at=datetime.now(UTC),
            )
            config = repair_job_config(
                source_config,
                task_name=task_name,
                arm=arm,
                job_name=name,
                jobs_dir=self.jobs_dir,
                attempts=self.attempts,
                checkpoint_image=source.checkpoint_image,
                session_file=session_file,
            )
            if not (self.jobs_dir / name).exists():
                inputs.mkdir(parents=True, exist_ok=True)
                if session_file is not None:
                    shutil.copyfile(sessions[0], session_file)
                (inputs / REPAIR_SOURCE_FILENAME).write_text(source.model_dump_json(indent=2))
                (inputs / "config.json").write_text(json.dumps(config, indent=4) + "\n")
            jobs.append(RepairJob(name, source, inputs / "config.json"))
        return jobs

    def discover(self) -> None:
        """Queue repairs for source trials that finished since the last pass."""
        for source_job in self.source_jobs:
            for trial_dir, result in finished_trials(source_job):
                if result.trial_name in self.queued_trials or result.trial_name in self.skipped:
                    continue
                if (
                    result.exception_info is not None
                    and result.exception_info.exception_type == USAGE_LIMIT_EXCEPTION
                ):
                    self.pause("source trial hit the usage limit")
                planned = self.plan_failure(trial_dir, result)
                if isinstance(planned, str):
                    self.skipped[result.trial_name] = planned
                    if planned != "passed":
                        logger.warning("%s not repaired: %s", result.trial_name, planned)
                    continue
                self.queued_trials.add(result.trial_name)
                for job in planned:
                    self.adopt_or_queue(job)

    def adopt_or_queue(self, job: RepairJob) -> None:
        job_dir = self.jobs_dir / job.name
        if not job_dir.exists():
            self.pending.append(job)
            logger.info("queued %s", job.name)
            return
        if job.name in self.running or job.name in self.resume_due:
            return
        pid_path = self.inputs_root / job.name / PID_FILENAME
        if pid_path.is_file():
            pid = int(pid_path.read_text())
            if pid_alive(pid, job.name):
                self.running[job.name] = pid
                logger.info("adopted running %s (pid %d)", job.name, pid)
                return
        self.check_finished(job.name)

    # -- running ------------------------------------------------------------------------------

    def pause(self, reason: str) -> None:
        until = self.clock() + self.usage_backoff_s
        if until > self.paused_until:
            self.paused_until = until
            logger.warning("pausing launches for %.0f s: %s", self.usage_backoff_s, reason)

    def resumes(self, name: str) -> int:
        path = self.inputs_root / name / RESUMES_FILENAME
        return json.loads(path.read_text())["count"] if path.is_file() else 0

    def check_finished(self, name: str) -> None:
        """After a job's process ended: resume it if it is unfinished or hit infra errors."""
        job_dir = self.jobs_dir / name
        failed = infra_failed(job_dir)
        if job_finished(job_dir) and not failed:
            return
        if self.resumes(name) >= self.max_resumes:
            logger.error("%s still incomplete or has %s after resumes; leaving it", name, failed)
            return
        delay = self.usage_backoff_s if USAGE_LIMIT_EXCEPTION in failed else 60.0
        if USAGE_LIMIT_EXCEPTION in failed:
            self.pause(f"{name} hit the usage limit")
        self.resume_due[name] = self.clock() + delay
        logger.warning("%s: resume scheduled in %.0f s (%s)", name, delay, failed or "unfinished")

    def reap(self) -> None:
        for name, process in list(self.running.items()):
            if isinstance(process, int):
                if pid_alive(process, name):
                    continue
                code = None
            else:
                code = process.poll()
                if code is None:
                    continue
            del self.running[name]
            (self.inputs_root / name / PID_FILENAME).unlink(missing_ok=True)
            logger.info("%s exited %s", name, code)
            job_dir = self.jobs_dir / name
            if not job_dir.exists():
                # `trajlab run` refused before Harbor started (its log says why); try again.
                logger.error("%s did not start; see %s.log", name, job_dir)
                self.requeue(name)
                continue
            if self.resumes(name) and job_finished(job_dir):
                self.write_manifest(name)
            self.check_finished(name)

    def requeue(self, name: str) -> None:
        refusals = self.refusals.get(name, 0) + 1
        self.refusals[name] = refusals
        if refusals > self.max_resumes:
            logger.error("%s refused %d times; leaving it", name, refusals)
            return
        source = RepairSource.model_validate_json(
            (self.inputs_root / name / REPAIR_SOURCE_FILENAME).read_text()
        )
        self.pending.append(RepairJob(name, source, self.inputs_root / name / "config.json"))

    def running_trials(self) -> int:
        used = sum(source_reserved_slots(job) for job in self.source_jobs)
        for name in self.running:
            used += max(0, self.attempts - len(finished_trials(self.jobs_dir / name)))
        return used

    def hold_reason(self) -> str | None:
        free = shutil.disk_usage(self.jobs_dir).free / 1e9
        if free < self.hold_below_gb:
            return f"{free:.0f} GB free, below {self.hold_below_gb:.0f} GB"
        if self.clock() < self.paused_until:
            return "usage-limit pause"
        if not self.watcher_running(self.jobs_dir):
            return f"no watcher holds {self.jobs_dir}"
        dirty = commit_new_manifests(self.manifests_dir)
        if dirty:
            return f"working tree has changes outside {self.manifests_dir}: {dirty}"
        return None

    def launch(self, name: str, command: list[str]) -> None:
        log = (self.jobs_dir / f"{name}.log").open("ab")
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        (self.inputs_root / name / PID_FILENAME).write_text(str(process.pid))
        self.running[name] = process

    def launch_ready(self) -> None:
        reason = self.hold_reason()
        if reason:
            if self.pending or self.resume_due:
                logger.warning("holding launches: %s", reason)
            return
        now = self.clock()
        for name, due in sorted(self.resume_due.items(), key=lambda item: item[1]):
            if due > now or self.running_trials() + self.attempts > self.max_running:
                continue
            del self.resume_due[name]
            count = self.resumes(name) + 1
            (self.inputs_root / name / RESUMES_FILENAME).write_text(json.dumps({"count": count}))
            command = [str(Path(sys.executable).parent / "harbor"), "jobs", "resume"]
            command += ["-p", str(self.jobs_dir / name)]
            for exception in sorted(infra_failed(self.jobs_dir / name) | {"CancelledError"}):
                command += ["-f", exception]
            if self.env_file is not None:
                command += ["--env-file", str(self.env_file)]
            self.launch(name, command)
            logger.info("resuming %s (resume %d)", name, count)
        while self.pending and self.running_trials() + self.attempts <= self.max_running:
            job = self.pending.pop(0)
            command = [str(Path(sys.executable).parent / "trajlab"), "run", str(job.config_path)]
            command += ["--corpus-id", job.name, "--manifests-dir", str(self.manifests_dir)]
            if self.env_file is not None:
                command += ["--env-file", str(self.env_file)]
            self.launch(job.name, command)
            logger.info("launched %s (%d trials running)", job.name, self.running_trials())

    def write_manifest(self, name: str) -> None:
        """Rewrite a resumed job's manifest: its trials changed since `trajlab run` wrote it."""
        try:
            root = repo_root(Path.cwd())
            config = (self.inputs_root / name / "config.json").resolve().relative_to(root)
            manifest = build_manifest(
                [self.jobs_dir / name],
                corpus_id=name,
                repo=repo_state(root),
                config_path=config.as_posix(),
                storage=None,
            )
            write_manifest(manifest, self.manifests_dir, overwrite=True)
        except ManifestError:
            logger.exception("no manifest written for resumed %s", name)

    def done(self) -> bool:
        return (
            all(job_finished(job) for job in self.source_jobs)
            and not self.pending
            and not self.running
            and not self.resume_due
        )

    def step(self) -> None:
        self.reap()
        self.discover()
        self.launch_ready()
        self.write_status()

    def write_status(self) -> None:
        status = {
            "updated_at": datetime.now(UTC).isoformat(),
            "running_trials": self.running_trials(),
            "running_jobs": sorted(self.running),
            "pending_jobs": [job.name for job in self.pending],
            "resume_due": self.resume_due,
            "paused_until": self.paused_until or None,
            "queued_failures": sorted(self.queued_trials),
            "not_repaired": {k: v for k, v in self.skipped.items() if v != "passed"},
        }
        (self.inputs_root / f"{self.prefix}.status.json").write_text(json.dumps(status, indent=2))

    def run(self, poll_s: float = 30.0) -> None:
        self.inputs_root.mkdir(parents=True, exist_ok=True)
        while True:
            self.step()
            if self.done():
                commit_new_manifests(self.manifests_dir)
                logger.info("all repairs finished; not repaired: %s", self.skipped)
                return
            time.sleep(poll_s)


def commit_new_manifests(manifests_dir: Path) -> list[str]:
    """Commit new or changed manifests so the next `trajlab run` sees a clean tree.

    Returns the dirty paths outside the manifests dir instead of committing anything if there
    are any: `trajlab run` would refuse, and they are not the launcher's to commit.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    paths = [line[3:] for line in status]
    prefix = f"{manifests_dir.as_posix().rstrip('/')}/"
    outside = [p for p in paths if not p.startswith(prefix)]
    if outside or not paths:
        return outside
    subprocess.run(["git", "add", *paths], check=True)
    names = ", ".join(Path(p).stem for p in paths)
    message = f"Record the {names} manifest{'s' if len(paths) > 1 else ''}"
    subprocess.run(["git", "commit", "-q", "--no-verify", "-m", message], check=True)
    logger.info("committed %s", paths)
    return []


def check_sources(source_jobs: Iterable[Path]) -> list[str]:
    """Problems that make these source jobs unusable for repair."""
    problems = []
    for job in source_jobs:
        if not (job / "config.json").is_file():
            problems.append(f"{job}: no config.json (start the source job first)")
    return problems
