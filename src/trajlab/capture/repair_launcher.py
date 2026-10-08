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

Another arm can be added to a round later (`arms`, e.g. only `traj-text`): it runs on that
round's recorded draw, never draws, and plans only its own jobs, so the round's existing jobs
are not touched. A failure then needs only what the given arms start from; `traj-text` needs
the failed trial's `agent/trajectory.json`, which is rendered as text at planning
(`trajlab.capture.transcript`).
"""

import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult
from harbor.models.trajectories import Trajectory
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TimingInfo, TrialResult
from pydantic import ValidationError

from trajlab.capture.corpus import (
    ManifestError,
    build_manifest,
    repo_root,
    repo_state,
    write_manifest,
)
from trajlab.capture.discover import iter_trial_dirs, load_trial_config
from trajlab.capture.harbor_runner import repo_relative
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.transcript import render
from trajlab.contracts import (
    CHECKPOINT_ARMS,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    REPAIR_ARMS,
    REPAIR_SOURCE_FILENAME,
    REPAIRABLE_KINDS,
    SESSION_ARMS,
    TRANSCRIPT_ARMS,
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
PRUNED_FILENAME = "pruned.json"
RESUMES_FILENAME = "resumes.json"
TRANSCRIPT_FILENAME = "transcript.txt"
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

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


def session_compacted(trial_dir: Path) -> bool:
    """True if a native session of the trial records a compaction (a `compact_boundary` event)."""
    for path in native_sessions(trial_dir):
        for line in path.read_text().splitlines():
            if COMPACT_BOUNDARY_SUBTYPE not in line:
                continue
            event = json.loads(line)
            if event.get("type") == "system" and event.get("subtype") == COMPACT_BOUNDARY_SUBTYPE:
                return True
    return False


def load_trajectory(trial_dir: Path) -> Trajectory | str:
    """The trial's ATIF trajectory, or why it cannot be read."""
    path = TrialPaths(trial_dir).agent_dir / "trajectory.json"
    if not path.is_file():
        return "no agent/trajectory.json"
    try:
        return Trajectory.model_validate_json(path.read_text())
    except ValidationError as error:
        return f"agent/trajectory.json does not load: {error.error_count()} errors"


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
    transcript_file: Path | None = None,
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
    if arm in TRANSCRIPT_ARMS:
        if transcript_file is None:
            raise ValueError(f"arm {arm} needs a transcript file")
        agent["kwargs"]["repair_transcript"] = str(transcript_file.resolve())
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


def job_complete(job_dir: Path) -> bool:
    """Finished with a result for each of its trials. `harbor jobs resume` deletes the trials it
    reruns before it rewrites result.json, so a resume that dies in between leaves an old,
    finished result.json and fewer trials."""
    result = job_result(job_dir)
    return (
        result is not None
        and result.finished_at is not None
        and len(finished_trials(job_dir)) >= result.n_total_trials
    )


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


def remove_checkpoint_image(tag: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    removed = runner(["docker", "rmi", tag], capture_output=True, text=True)
    if removed.returncode != 0:
        logger.warning("could not remove %s: %s", tag, removed.stderr.strip())
    return removed.returncode == 0


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
    storage: str | None = None
    per_task: int | None = None
    prune: bool = True
    arms: tuple[RepairArm, ...] = REPAIR_ARMS
    dry_run: bool = False
    watcher_running: Callable[[Path], bool] = lambda _: True
    tag_lookup: Callable[[str], str | None] = checkpoint_tag
    remove_image: Callable[[str], bool] = lambda tag: remove_checkpoint_image(tag)
    clock: Callable[[], float] = time.time
    pending: list[RepairJob] = field(default_factory=list)
    queued_trials: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)
    running: dict[str, subprocess.Popen[bytes] | int] = field(default_factory=dict)
    resume_due: dict[str, float] = field(default_factory=dict)
    # Trials a running resume reruns: their old dirs count as finished until Harbor removes them.
    rerunning: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    paused_until: float = 0.0
    selection: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def inputs_root(self) -> Path:
        return self.jobs_dir / INPUTS_DIRNAME

    # -- planning -----------------------------------------------------------------------------

    def blocker(self, trial_dir: Path, result: TrialResult) -> str | None:
        """Why this finished trial cannot be repaired under every arm, or None if it can."""
        kind = classify_failure(result)
        if kind is None:
            return "passed"
        if kind not in REPAIRABLE_KINDS:
            exception = result.exception_info.exception_type if result.exception_info else None
            return f"{kind} ({exception})"
        arms = set(self.arms)
        if arms & CHECKPOINT_ARMS:
            record = final_checkpoint(trial_dir)
            if record is None:
                return "no checkpoint"
            if self.tag_lookup(record.checkpoint_id) is None:
                return f"checkpoint image {record.checkpoint_id} is gone"
        if arms & SESSION_ARMS:
            sessions = native_sessions(trial_dir)
            if len(sessions) != 1:
                return f"expected one native session, found {len(sessions)}"
        if arms & TRANSCRIPT_ARMS:
            trajectory = load_trajectory(trial_dir)
            if isinstance(trajectory, str):
                return trajectory
        return None

    def plan_failure(self, trial_dir: Path, result: TrialResult) -> list[RepairJob] | str:
        """One repair job per arm for one failed trial, or why it is not repaired."""
        reason = self.blocker(trial_dir, result)
        if reason is not None:
            return reason
        kind = classify_failure(result)
        assert kind is not None
        arms = set(self.arms)
        record = final_checkpoint(trial_dir) if arms & CHECKPOINT_ARMS else None
        tag = self.tag_lookup(record.checkpoint_id) if record is not None else None
        sessions = native_sessions(trial_dir) if arms & SESSION_ARMS else []
        transcript: str | None = None
        transcript_fields: dict[str, Any] = {}
        if arms & TRANSCRIPT_ARMS:
            trajectory = load_trajectory(trial_dir)
            assert not isinstance(trajectory, str)
            transcript, stats = render(trajectory)
            transcript_fields = {
                "transcript_chars": stats.chars,
                "transcript_bytes": stats.bytes,
                "transcript_outputs_total": stats.outputs_total,
                "transcript_outputs_cut": stats.outputs_cut,
                "transcript_rule": stats.rule,
                "source_compacted": session_compacted(trial_dir),
            }
        source_config = json.loads((trial_dir.parent / "config.json").read_text())
        task_name = load_trial_config(trial_dir).task.name
        jobs = []
        for arm in self.arms:
            name = repair_job_name(self.prefix, result.trial_name, arm)
            inputs = self.inputs_root / name
            session_file = inputs / sessions[0].name if arm in SESSION_ARMS else None
            transcript_file = inputs / TRANSCRIPT_FILENAME if arm in TRANSCRIPT_ARMS else None
            checkpoint = record if arm in CHECKPOINT_ARMS else None
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
                checkpoint_seq=checkpoint.seq if checkpoint else None,
                checkpoint_image=tag if checkpoint else None,
                checkpoint_image_id=checkpoint.checkpoint_id if checkpoint else None,
                session_file=str(session_file) if session_file else None,
                transcript_file=str(transcript_file) if transcript_file else None,
                **(transcript_fields if arm in TRANSCRIPT_ARMS else {}),
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
                transcript_file=transcript_file,
            )
            # A dry run plans and renders but writes nothing.
            if not self.dry_run and not (self.jobs_dir / name).exists():
                inputs.mkdir(parents=True, exist_ok=True)
                if session_file is not None:
                    shutil.copyfile(sessions[0], session_file)
                if transcript_file is not None:
                    assert transcript is not None
                    transcript_file.write_bytes(transcript.encode("utf-8"))
                (inputs / REPAIR_SOURCE_FILENAME).write_text(source.model_dump_json(indent=2))
                (inputs / "config.json").write_text(json.dumps(config, indent=4) + "\n")
            jobs.append(RepairJob(name, source, inputs / "config.json"))
        return jobs

    @property
    def selection_path(self) -> Path:
        return self.inputs_root / f"{self.prefix}.selection.json"

    @property
    def reuses_draw(self) -> bool:
        """True if only failures already drawn are repaired: arms added to a round later."""
        return self.arms != REPAIR_ARMS

    def save_selection(self) -> None:
        if not self.dry_run:
            self.selection_path.write_text(json.dumps(self.selection, indent=2) + "\n")

    def discover(self) -> None:
        """Queue repairs for source trials that finished since the last pass."""
        if self.per_task is not None:
            self.discover_per_task(self.per_task)
            return
        for source_job in self.source_jobs:
            for trial_dir, result in finished_trials(source_job):
                if result.trial_name in self.queued_trials or result.trial_name in self.skipped:
                    continue
                self.check_usage_limit(result)
                self.queue_failure(trial_dir, result)

    def check_usage_limit(self, result: TrialResult) -> None:
        if (
            not self.dry_run
            and result.exception_info is not None
            and result.exception_info.exception_type == USAGE_LIMIT_EXCEPTION
        ):
            self.pause("source trial hit the usage limit")

    def discover_per_task(self, per_task: int) -> None:
        """Repair `per_task` failures of each task, drawn at random once all its attempts end.

        Waiting for every attempt keeps the draw from favoring early failures. A task with a
        harness-failed attempt waits for its rerun, unless the source job has finished. The draw
        is seeded by prefix and task and written to `<prefix>.selection.json`; a recorded draw is
        reused on restart. With arms other than the default, the round's recorded draw is read
        and never written: no task is drawn anew.
        """
        if not self.selection and self.selection_path.is_file():
            self.selection = json.loads(self.selection_path.read_text())
        for source_job in self.source_jobs:
            attempts = int(
                json.loads((source_job / "config.json").read_text()).get("n_attempts", 1)
            )
            source_done = job_finished(source_job)
            by_task: dict[str, list[tuple[Path, TrialResult]]] = {}
            for trial_dir, result in finished_trials(source_job):
                self.check_usage_limit(result)
                by_task.setdefault(load_trial_config(trial_dir).task.name, []).append(
                    (trial_dir, result)
                )
            for task, trials in sorted(by_task.items()):
                key = f"{source_job.name}:{task}"
                if key not in self.selection and self.reuses_draw:
                    continue
                if key not in self.selection:
                    unsettled = any(
                        classify_failure(r) in ("infra_error", "unclassified") for _, r in trials
                    )
                    if not source_done and (len(trials) < attempts or unsettled):
                        continue
                    blockers = {r.trial_name: self.blocker(d, r) for d, r in trials}
                    candidates = sorted(name for name, why in blockers.items() if why is None)
                    seed = f"{self.prefix}:{task}"
                    chosen = sorted(
                        random.Random(seed).sample(candidates, min(per_task, len(candidates)))
                    )
                    self.selection[key] = {
                        "task": task,
                        "seed": seed,
                        "candidates": candidates,
                        "chosen": chosen,
                        "not_candidates": {k: v for k, v in blockers.items() if v is not None},
                    }
                    self.save_selection()
                    logger.info("%s: repairing %s of %s", task, chosen, candidates)
                chosen = set(self.selection[key]["chosen"])
                for trial_dir, result in trials:
                    name = result.trial_name
                    if name in self.queued_trials or name in self.skipped:
                        continue
                    if name in chosen:
                        self.queue_failure(trial_dir, result)
                    else:
                        reason = self.selection[key]["not_candidates"].get(name, "not drawn")
                        self.skipped[name] = reason

    def queue_failure(self, trial_dir: Path, result: TrialResult) -> None:
        planned = self.plan_failure(trial_dir, result)
        if isinstance(planned, str):
            self.skipped[result.trial_name] = planned
            if planned != "passed":
                logger.warning("%s not repaired: %s", result.trial_name, planned)
            return
        self.queued_trials.add(result.trial_name)
        for job in planned:
            if self.dry_run:
                self.pending.append(job)
            else:
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
        if job_complete(job_dir) and not failed:
            if self.prune:
                self.prune_job(job_dir)
            return
        if self.resumes(name) >= self.max_resumes:
            logger.error("%s still incomplete or has %s after resumes; leaving it", name, failed)
            return
        delay = self.usage_backoff_s if USAGE_LIMIT_EXCEPTION in failed else 60.0
        if USAGE_LIMIT_EXCEPTION in failed:
            self.pause(f"{name} hit the usage limit")
        self.resume_due[name] = self.clock() + delay
        logger.warning("%s: resume scheduled in %.0f s (%s)", name, delay, failed or "unfinished")

    def prune_job(self, job_dir: Path) -> None:
        """Remove every checkpoint image of a finished repair job except each trial's final one.

        Repairs are scored on outcome, so only their final state is kept as an image; every
        checkpoint record stays, and `agent/checkpoints/pruned.json` lists the removed ones.
        """
        for trial_dir in iter_trial_dirs(job_dir):
            directory = TrialPaths(trial_dir).agent_dir / CHECKPOINTS_DIRNAME
            pruned_path = directory / PRUNED_FILENAME
            final = final_checkpoint(trial_dir)
            if final is None or pruned_path.is_file():
                continue
            removed = []
            for line in (directory / CHECKPOINT_RECORDS_FILENAME).read_text().splitlines():
                if not line.strip():
                    continue
                record = CheckpointRecord.model_validate_json(line)
                if record.seq == final.seq:
                    continue
                tag = self.tag_lookup(record.checkpoint_id)
                if tag is not None and self.remove_image(tag):
                    removed.append({"seq": record.seq, "checkpoint_id": record.checkpoint_id})
            pruned_path.write_text(
                json.dumps(
                    {
                        "kept_seq": final.seq,
                        "removed": removed,
                        "at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                )
                + "\n"
            )
            logger.info("pruned %d checkpoint images of %s", len(removed), trial_dir.name)

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
            self.rerunning.pop(name, None)
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
            unfinished = max(0, self.attempts - len(finished_trials(self.jobs_dir / name)))
            used += max(unfinished, self.rerunning.get(name, 0))
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

    def launch(self, name: str, command: list[str], env: dict[str, str] | None = None) -> None:
        log = (self.jobs_dir / f"{name}.log").open("ab")
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
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
            rerun = infra_failed(self.jobs_dir / name) | {"CancelledError"}
            for exception in sorted(rerun):
                command += ["-f", exception]
            # Harbor reruns every trial but those that finished with another outcome.
            kept = [
                result
                for _, result in finished_trials(self.jobs_dir / name)
                if result.exception_info is None
                or result.exception_info.exception_type not in rerun
            ]
            self.launch(name, command, env=resume_env(self.env_file))
            self.rerunning[name] = max(0, self.attempts - len(kept))
            logger.info("resuming %s (resume %d)", name, count)
        while self.pending and self.running_trials() + self.attempts <= self.max_running:
            job = self.pending.pop(0)
            command = [str(Path(sys.executable).parent / "trajlab"), "run", str(job.config_path)]
            command += ["--corpus-id", job.name, "--manifests-dir", str(self.manifests_dir)]
            if self.storage is not None:
                command += ["--storage", self.storage]
            if self.env_file is not None:
                command += ["--env-file", str(self.env_file)]
            self.launch(job.name, command)
            logger.info("launched %s (%d trials running)", job.name, self.running_trials())

    def write_manifest(self, name: str) -> None:
        """Rewrite a resumed job's manifest: its trials changed since `trajlab run` wrote it."""
        try:
            root = repo_root(Path.cwd())
            config = repo_relative(self.inputs_root / name / "config.json", root)
            manifest = build_manifest(
                [self.jobs_dir / name],
                corpus_id=name,
                repo=repo_state(root),
                config_path=config,
                storage=self.storage,
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
            "selected": {v["task"]: v["chosen"] for v in self.selection.values()},
        }
        self.status_path.write_text(json.dumps(status, indent=2))

    @property
    def status_path(self) -> Path:
        """`<prefix>.status.json`, or `<prefix>.<arms>.status.json` for arms other than the
        default, so a launcher adding arms to a round leaves the round's own status file alone."""
        arms = "" if self.arms == REPAIR_ARMS else "." + "+".join(self.arms)
        return self.inputs_root / f"{self.prefix}{arms}.status.json"

    def run(self, poll_s: float = 30.0) -> None:
        self.inputs_root.mkdir(parents=True, exist_ok=True)
        while True:
            self.step()
            if self.done():
                commit_new_manifests(self.manifests_dir)
                logger.info("all repairs finished; not repaired: %s", self.skipped)
                return
            time.sleep(poll_s)


def resume_env(env_file: Path | None) -> dict[str, str] | None:
    """The environment for `harbor jobs resume`, which has no `--env-file` option: this
    process's environment with the env file's values over it, as `harbor run --env-file` loads
    the file (python-dotenv, override). None, inheriting this process's, without a file."""
    if env_file is None:
        return None
    values = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    return {**os.environ, **values}


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
