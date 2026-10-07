"""Replay: rerun a task's verifier on an artifact state through Harbor's regrade.

ADR-0013, decision 3. A state dir is a minimal trial dir (`artifacts/` with Harbor's manifest, and
the source trial's `result.json`), so Harbor's `RegradeTrial` can grade it exactly as
`harbor trial regrade` would: the task's separate verifier environment, network policy, user,
environment variables, and timeout all come from Harbor. `ReplayTrial` changes two things. An
artifact recorded as `failed` (absent at that point of the trial) is allowed, and Harbor's
uploader then skips it, as it does in a live trial whose agent never wrote the file. And seeding
keeps symlinks, as a live trial's upload and Harbor's multi-step regrade do (single-step regrade
dereferences them).

Each replay is admitted by `trajlab.groundtruth.admission` with the task's declared verifier
CPUs and memory, and records its outcome: `verdict`, `no_verdict`, or `infra`.
"""

import fcntl
import importlib.metadata
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.models.task.task import Task
from harbor.models.trial.config import (
    EnvironmentConfig,
    SourceTrialConfig,
    TaskConfig,
    TrialConfig,
    VerifierConfig,
)
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult
from harbor.tasks.client import TaskDownloadResult
from harbor.trial.regrade import RegradeTrial
from harbor.trial.trial import Trial

from trajlab.contracts.groundtruth import (
    REPLAY_RECORDS_FILENAME,
    REPLAYS_DIRNAME,
    CheckResult,
    ReplayOutcome,
    ReplayPurpose,
    ReplayRecord,
)
from trajlab.groundtruth.admission import Claim, Ledger, admitted
from trajlab.groundtruth.checks import read_checks
from trajlab.groundtruth.extract import groundtruth_dir, read_state, state_dir
from trajlab.groundtruth.traits import traits

log = logging.getLogger(__name__)

EXCEPTION_MESSAGE_CHARS = 500
# Harbor's wording for a manifest entry whose collection failed (harbor/trial/regrade.py).
_ABSENT_PROBLEM = "{source}: collection failed in the source trial"
# Text in a verifier's output that means the environment or the network failed, not the state.
INFRA_MARKERS = (
    "Temporary failure in name resolution",
    "Could not resolve host",
    "Failed to establish a new connection",
    "Connection refused",
    "Connection reset by peer",
    "Read timed out",
    "npm ERR! network",
    "npm error network",
    "ECONNRESET",
    "ETIMEDOUT",
    "EAI_AGAIN",
    "No space left on device",
    "Cannot connect to the Docker daemon",
)
FINAL_SAMPLES = 2  # replays of the final state; with the original run, three samples
MAX_FINAL_ATTEMPTS = 4  # final replays with or without a verdict; gate 2 fails after these
FIX_SAMPLES = 2  # replays of a labeled fix's state before it confirms anything
QUIET_FIX_SAMPLES = 3  # the same for tasks whose checks race wall-clock limits
DEFAULT_VERIFIER_CPUS = 1.0
DEFAULT_VERIFIER_MEMORY_MB = 2048


class ReplayTrial(RegradeTrial):
    """Harbor's regrade of a recorded state, accepting artifacts absent at that point."""

    def __init__(self, config: TrialConfig, *, absent_sources: frozenset[str], **kwargs) -> None:
        self._absent_sources = absent_sources
        super().__init__(config, **kwargs)

    def _raise_artifact_coverage_error(self, problems: list[str]) -> None:
        def absent(problem: str) -> bool:
            return any(
                problem.startswith(_ABSENT_PROBLEM.format(source=source))
                for source in self._absent_sources
            )

        super()._raise_artifact_coverage_error([p for p in problems if not absent(p)])

    def _seed_from_source(self) -> None:
        self._replay_recorded_outputs(
            step_name=None,
            declared_artifacts=[*self.task.config.artifacts, *self.config.artifacts],
            preserve_symlinks=True,
        )


def records_path(trial_dir: Path) -> Path:
    return groundtruth_dir(trial_dir) / REPLAY_RECORDS_FILENAME


def read_records(trial_dir: Path) -> list[ReplayRecord]:
    path = records_path(trial_dir)
    if not path.is_file():
        return []
    return [ReplayRecord.model_validate_json(line) for line in path.read_text().splitlines()]


def append_record(trial_dir: Path, record: ReplayRecord) -> None:
    path = records_path(trial_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(record.model_dump_json() + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def classify_outcome(verifier_dir: Path, checks: list[CheckResult]) -> ReplayOutcome:
    if any(check.status is not None for check in checks):
        return "verdict"
    stdout = verifier_dir / "test-stdout.txt"
    text = stdout.read_text(errors="replace") if stdout.is_file() else ""
    if text.strip() and not any(marker in text for marker in INFRA_MARKERS):
        return "no_verdict"
    return "infra"


def replay_config(trial_dir: Path, state_id: str, replay_id: str) -> TrialConfig:
    """The TrialConfig `harbor trial regrade` would build for this state as its source."""
    trial_config = TrialConfig.model_validate_json(TrialPaths(trial_dir).config_path.read_text())
    source = state_dir(trial_dir, state_id)
    source_result = TrialResult.model_validate_json((source / "result.json").read_text())
    return TrialConfig(
        task=TaskConfig(
            name=trial_config.task.name,
            ref=trial_config.task.ref,
            source=trial_config.task.source,
        ),
        trial_name=replay_id,
        trials_dir=groundtruth_dir(trial_dir) / REPLAYS_DIRNAME,
        agent=trial_config.agent,
        # What PreinstalledDockerEnvironment starts for a separate verifier (ADR-0013).
        environment=EnvironmentConfig(),
        verifier=VerifierConfig(),
        artifacts=trial_config.artifacts,
        source_trial=SourceTrialConfig(
            action="regrade", type="local", trial_id=source_result.id, path=source
        ),
    )


def compose_project(config: TrialConfig) -> str:
    """The compose project of a replay's verifier environment, by Harbor's own naming."""
    holder = SimpleNamespace(config=config)
    session_id = Trial._separate_verifier_session_id(holder, "trial")  # type: ignore[arg-type]
    return _sanitize_docker_compose_project_name(session_id)


@dataclass
class PreparedReplay:
    trial_dir: Path
    state_id: str
    purpose: ReplayPurpose
    base_state_id: str | None
    patch_sha256: str | None
    config: TrialConfig
    task: Task
    download: TaskDownloadResult
    claim: Claim
    absent: frozenset[str]


async def prepare(
    trial_dir: Path,
    state_id: str,
    purpose: ReplayPurpose,
    *,
    base_state_id: str | None = None,
    patch_sha256: str | None = None,
) -> PreparedReplay:
    trial_dir = trial_dir.resolve()
    replay_id = f"{trial_dir.name}-{purpose[0]}{uuid.uuid4().hex[:10]}"
    config = replay_config(trial_dir, state_id, replay_id)
    task, download = await Trial._load_task(config)
    environment = task.config.verifier.environment
    claim = Claim(
        claim_id=replay_id,
        cpus=float(environment.cpus if environment and environment.cpus else DEFAULT_VERIFIER_CPUS),
        memory_mb=int(
            environment.memory_mb
            if environment and environment.memory_mb
            else DEFAULT_VERIFIER_MEMORY_MB
        ),
        quiet=traits(task.name).quiet,
        project=compose_project(config),
    )
    absent = frozenset(
        e.source for e in read_state(trial_dir, state_id).entries if e.status == "failed"
    )
    return PreparedReplay(
        trial_dir,
        state_id,
        purpose,
        base_state_id,
        patch_sha256,
        config,
        task,
        download,
        claim,
        absent,
    )


async def run_prepared(prepared: PreparedReplay, beside: list[str]) -> ReplayRecord:
    """Run an admitted replay; append and return its record. The caller releases the claim."""
    config = prepared.config
    load_1m = os.getloadavg()[0]
    started_at = datetime.now(UTC)
    trial = ReplayTrial(
        config,
        absent_sources=prepared.absent,
        _task=prepared.task,
        _task_download_result=prepared.download,
        _source_trial_dir=state_dir(prepared.trial_dir, prepared.state_id),
    )
    result = await trial.run()
    finished_at = datetime.now(UTC)
    verifier_dir = TrialPaths(config.trials_dir / config.trial_name).verifier_dir
    checks = read_checks(verifier_dir)
    rewards = result.verifier_result.rewards if result.verifier_result else None
    reward = rewards.get("reward") if rewards else None
    exception = result.exception_info
    record = ReplayRecord(
        replay_id=config.trial_name,
        trial_name=prepared.trial_dir.name,
        state_id=prepared.state_id,
        purpose=prepared.purpose,
        base_state_id=prepared.base_state_id,
        patch_sha256=prepared.patch_sha256,
        reward=float(reward) if reward is not None else None,
        checks=tuple(checks),
        exception_type=exception.exception_type if exception else None,
        exception_message=(
            exception.exception_message[:EXCEPTION_MESSAGE_CHARS] if exception else None
        ),
        harbor_version=importlib.metadata.version("harbor"),
        task_ref=config.task.ref or config.task.name or "",
        started_at=started_at,
        finished_at=finished_at,
        outcome=classify_outcome(verifier_dir, checks),
        load_1m=round(load_1m, 2),
        beside=tuple(beside),
    )
    append_record(prepared.trial_dir, record)
    log.info(
        "%s %s %s: %s, reward %s%s",
        prepared.trial_dir.name,
        prepared.purpose,
        prepared.state_id[:12],
        record.outcome,
        record.reward,
        f" ({record.exception_type})" if record.exception_type else "",
    )
    return record


async def replay(
    trial_dir: Path,
    state_id: str,
    purpose: ReplayPurpose,
    *,
    base_state_id: str | None = None,
    patch_sha256: str | None = None,
    ledger: Ledger | None = None,
) -> ReplayRecord:
    """Wait for admission, run the task's verifier on one state, and record it."""
    prepared = await prepare(
        trial_dir, state_id, purpose, base_state_id=base_state_id, patch_sha256=patch_sha256
    )
    async with admitted(prepared.claim, ledger) as beside:
        return await run_prepared(prepared, beside)


def reparse_records(trial_dir: Path) -> int:
    """Re-read every replay's checks and outcome from its verifier dir; return records changed."""
    path = records_path(trial_dir)
    if not path.is_file():
        return 0
    replays_dir = groundtruth_dir(trial_dir) / REPLAYS_DIRNAME
    with path.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            records = [
                ReplayRecord.model_validate_json(line) for line in handle.read().splitlines()
            ]
            updated = []
            for record in records:
                verifier_dir = TrialPaths(replays_dir / record.replay_id).verifier_dir
                if verifier_dir.is_dir():
                    checks = read_checks(verifier_dir)
                    record = record.model_copy(
                        update={
                            "checks": tuple(checks),
                            "outcome": classify_outcome(verifier_dir, checks),
                        }
                    )
                updated.append(record)
            changed = sum(a != b for a, b in zip(records, updated, strict=True))
            if changed:
                handle.seek(0)
                handle.truncate()
                handle.write("".join(r.model_dump_json() + "\n" for r in updated))
                handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return changed
