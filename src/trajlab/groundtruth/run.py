"""Extraction and replays over a job's trials, restartable (ADR-0013, decision 3).

Every step skips what is already on disk: a trial with `points.jsonl` is not extracted again, and
a state is not replayed again once it has the samples it needs. A replay whose outcome is `infra`
does not count and is retried, up to `INFRA_RETRIES` times.

Replays are admitted in queue order by declared resources (`trajlab.groundtruth.admission`). The
queue interleaves tasks and puts each trial's second final sample in a later wave than its first,
so samples of one state meet different load. If the head of the queue cannot start for
`HEAD_PATIENCE_S` (a quiet replay waiting for the host to calm down), nothing behind it starts
until it does. On cancellation, running replays are cancelled and, if Harbor's teardown does not
finish within `TEARDOWN_GRACE_S`, their compose projects are taken down directly.
"""

import asyncio
import logging
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from harbor.models.task.task import Task
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import TrialPaths
from harbor.trial.trial import Trial

from trajlab.contracts.groundtruth import ReplayPurpose, ReplayRecord
from trajlab.groundtruth.admission import Ledger, tear_down
from trajlab.groundtruth.extract import (
    TrialInputs,
    declared_artifacts,
    extract_timeline,
    points_path,
    read_points,
)
from trajlab.groundtruth.items import regression_repeats, trial_timeline
from trajlab.groundtruth.replay import (
    FINAL_SAMPLES,
    PreparedReplay,
    prepare,
    read_records,
    run_prepared,
)

log = logging.getLogger(__name__)

INFRA_RETRIES = 2
HEAD_PATIENCE_S = 90.0
POLL_S = 3.0
TEARDOWN_GRACE_S = 60.0
LOOKAHEAD = 24  # queue entries considered for admission on each pass


def finished_trial_dirs(job_dir: Path, names: list[str] | None = None) -> list[Path]:
    """The job's trial dirs that have a result.json, optionally only the named ones."""
    found = sorted(
        child
        for child in job_dir.iterdir()
        if child.is_dir()
        and TrialPaths(child).config_path.is_file()
        and TrialPaths(child).result_path.is_file()
    )
    if names:
        wanted = set(names)
        missing = wanted - {d.name for d in found}
        if missing:
            raise ValueError(f"no finished trial named {sorted(missing)} in {job_dir}")
        found = [d for d in found if d.name in wanted]
    return found


def trial_config(trial_dir: Path) -> TrialConfig:
    return TrialConfig.model_validate_json(TrialPaths(trial_dir).config_path.read_text())


async def _load_tasks(configs: list[TrialConfig]) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    for config in configs:
        key = f"{config.task.name}@{config.task.ref}"
        if key not in tasks:
            tasks[key], _ = await Trial._load_task(config)
    return tasks


def trial_inputs(trial_dirs: list[Path]) -> list[TrialInputs]:
    configs = [trial_config(d) for d in trial_dirs]
    tasks = asyncio.run(_load_tasks(configs))
    inputs = []
    for trial_dir, config in zip(trial_dirs, configs, strict=True):
        task = tasks[f"{config.task.name}@{config.task.ref}"]
        artifacts, convention = declared_artifacts(task, config)
        task_name = config.task.name or trial_dir.name.split("__")[0]
        inputs.append(
            TrialInputs(trial_dir.resolve(), task_name, artifacts, convention, task.paths.task_dir)
        )
    return inputs


def extract_job(trial_dirs: list[Path], *, workers: int = 4, force: bool = False) -> list[str]:
    """Extract every trial's timeline; return the names of trials that failed."""
    failed: list[str] = []

    def one(inputs: TrialInputs) -> None:
        try:
            points = extract_timeline(inputs, force=force)
            states = len({p.state_id for p in points})
            log.info("%s: %d points, %d states", inputs.trial_dir.name, len(points), states)
        except Exception:
            log.exception("%s: extraction failed", inputs.trial_dir.name)
            failed.append(inputs.trial_dir.name)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, trial_inputs(trial_dirs)))
    return sorted(failed)


@dataclass(frozen=True)
class PlannedReplay:
    trial_dir: Path
    state_id: str
    purpose: ReplayPurpose
    wave: int  # 0: first final sample; 1: timeline states; 2: further samples


def _counts(records: list[ReplayRecord]) -> tuple[Counter[str], Counter[str], Counter[str]]:
    """Per state: samples (verdict or no_verdict), final samples, and infra failures."""
    samples: Counter[str] = Counter()
    finals: Counter[str] = Counter()
    infra: Counter[str] = Counter()
    for record in records:
        if record.purpose == "counterfactual":
            continue
        if record.outcome == "infra" or (record.exception_type and not record.checks):
            infra[record.state_id] += 1
            continue
        samples[record.state_id] += 1
        if record.purpose == "final":
            finals[record.state_id] += 1
    return samples, finals, infra


def plan_replays(trial_dir: Path, *, timeline: bool, repeats: bool) -> list[PlannedReplay]:
    """What is still missing for one trial."""
    if not points_path(trial_dir).is_file():
        return []
    points = read_points(trial_dir)
    samples, finals, infra = _counts(read_records(trial_dir))
    final_id = points[-1].state_id
    planned: list[PlannedReplay] = []

    def gave_up(state_id: str) -> bool:
        return not samples[state_id] and infra[state_id] > INFRA_RETRIES

    if not gave_up(final_id):
        for n in range(finals[final_id], FINAL_SAMPLES):
            planned.append(PlannedReplay(trial_dir, final_id, "final", 0 if n == 0 else 2))
    if timeline:
        for state_id in dict.fromkeys(p.state_id for p in points):
            if state_id != final_id and not samples[state_id] and not gave_up(state_id):
                planned.append(PlannedReplay(trial_dir, state_id, "timeline", 1))
    if repeats:
        for state_id, missing in regression_repeats(trial_timeline(trial_dir)).items():
            planned += [PlannedReplay(trial_dir, state_id, "repeat", 2)] * missing
    return planned


def interleave(planned: list[PlannedReplay]) -> list[PlannedReplay]:
    """By wave, then round-robin over tasks, so one task's replays do not run all at once."""
    rank: list[int] = []
    seen: Counter[tuple[int, str]] = Counter()
    for item in planned:
        task = item.trial_dir.name.split("__")[0]
        rank.append(seen[(item.wave, task)])
        seen[(item.wave, task)] += 1
    order = sorted(
        range(len(planned)),
        key=lambda i: (planned[i].wave, rank[i], planned[i].trial_dir.name),
    )
    return [planned[i] for i in order]


async def replay_job(
    trial_dirs: list[Path], *, timeline: bool, repeats: bool = False
) -> tuple[int, int]:
    """Run every planned replay under admission; return (verdicts, other outcomes)."""
    planned = interleave(
        [p for d in trial_dirs for p in plan_replays(d, timeline=timeline, repeats=repeats)]
    )
    log.info("%d replays planned over %d trials", len(planned), len(trial_dirs))
    ledger = Ledger()
    queue: list[tuple[PlannedReplay, PreparedReplay | None]] = [(p, None) for p in planned]
    running: dict[asyncio.Task[ReplayRecord], PreparedReplay] = {}
    outcomes: Counter[str] = Counter()
    head_blocked_since: float | None = None
    try:
        while queue or running:
            index = 0
            while index < min(len(queue), LOOKAHEAD):
                item, prepared = queue[index]
                if prepared is None:
                    try:
                        prepared = await prepare(item.trial_dir, item.state_id, item.purpose)
                    except Exception:
                        log.exception("%s %s: cannot prepare", item.trial_dir.name, item.state_id)
                        queue.pop(index)
                        outcomes["error"] += 1
                        continue
                    queue[index] = (item, prepared)
                beside = ledger.try_admit(prepared.claim)
                if beside is None:
                    if index == 0:
                        head_blocked_since = head_blocked_since or time.monotonic()
                        if time.monotonic() - head_blocked_since > HEAD_PATIENCE_S:
                            break  # hold capacity back until the head fits
                    index += 1
                    continue
                if index == 0:
                    head_blocked_since = None
                queue.pop(index)
                running[asyncio.create_task(run_prepared(prepared, beside))] = prepared
            if not running:
                await asyncio.sleep(POLL_S)
                continue
            done, _ = await asyncio.wait(
                running, timeout=POLL_S, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                prepared = running.pop(task)
                ledger.release(prepared.claim.claim_id)
                try:
                    outcomes[task.result().outcome] += 1
                except Exception:
                    log.exception("%s: replay failed", prepared.claim.claim_id)
                    outcomes["error"] += 1
                total = sum(outcomes.values())
                if total % 10 == 0 or (not queue and not running):
                    log.info("replays: %d/%d done %s", total, len(planned), dict(outcomes))
    finally:
        if running:
            for task in running:
                task.cancel()
            await asyncio.wait(running, timeout=TEARDOWN_GRACE_S)
            for prepared in running.values():
                tear_down(prepared.claim)
                ledger.release(prepared.claim.claim_id)
    return outcomes["verdict"], sum(outcomes.values()) - outcomes["verdict"]


def planned_counts(trial_dirs: list[Path], *, timeline: bool, repeats: bool) -> dict[str, int]:
    found: dict[str, int] = defaultdict(int)
    for trial_dir in trial_dirs:
        for item in plan_replays(trial_dir, timeline=timeline, repeats=repeats):
            found[item.purpose] += 1
    return dict(found)


def plan_fix_confirmations(trial_dir: Path) -> list[tuple[str, str, str]]:
    """Missing replays of labeled fixes' states: (state_id, base_state_id, patch_sha256) each.

    A fix confirms a cause only if every replay of its state agrees (ADR-0013, decision 5), so
    each labeled fix's state gets `FIX_SAMPLES` (`QUIET_FIX_SAMPLES` for quiet tasks) replays.
    """
    from trajlab.contracts.groundtruth import TrialLabels
    from trajlab.groundtruth.counterfactual import read_fixes
    from trajlab.groundtruth.items import labels_path
    from trajlab.groundtruth.traits import traits

    if not labels_path(trial_dir).is_file():
        return []
    labels = TrialLabels.model_validate_json(labels_path(trial_dir).read_text())
    fixes = {f.fix_id: f for f in read_fixes(trial_dir)}
    task_name = trial_config(trial_dir).task.name or ""
    wanted = QUIET_FIX_SAMPLES if traits(task_name).quiet else FIX_SAMPLES
    counts = Counter(
        r.state_id
        for r in read_records(trial_dir)
        if r.purpose == "counterfactual" and r.outcome == "verdict"
    )
    planned = []
    for cause in labels.causes:
        fix = fixes.get(cause.fix_id or "")
        if fix is None or fix.state_id is None:
            continue
        for _ in range(wanted - counts[fix.state_id]):
            planned.append((fix.state_id, fix.base_state_id, fix.patch_sha256))
        counts[fix.state_id] = max(counts[fix.state_id], wanted)
    return planned


FIX_SAMPLES = 2
QUIET_FIX_SAMPLES = 3


async def confirm_fixes(trial_dirs: list[Path]) -> int:
    from trajlab.groundtruth.replay import replay

    jobs = [(d, *item) for d in trial_dirs for item in plan_fix_confirmations(d)]
    log.info("%d fix confirmation replays planned", len(jobs))

    async def one(trial_dir: Path, state_id: str, base: str, patch: str) -> None:
        try:
            await replay(
                trial_dir, state_id, "counterfactual", base_state_id=base, patch_sha256=patch
            )
        except Exception:
            log.exception("%s %s: confirmation failed", trial_dir.name, state_id[:12])

    await asyncio.gather(*(one(*job) for job in jobs))
    return len(jobs)
