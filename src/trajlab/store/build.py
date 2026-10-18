"""Build one trial into the Exp 2 store (ADR-0014).

A trial is built from its dir: `result.json` gives the `trials` row, the enriched trajectory
(`trajlab postprocess` writes it; this module never runs postprocess) gives the `steps` rows and
the checkpoint records, and the checkpoint images, read through an `ImageProvider`, give the
`deltas` rows (`trajlab.store.reconcile`). With no provider, the steps are built and the chain is
reported incomplete because its images were not checked.

Writes go to `record.duckdb` (trials, steps, deltas) and `heldout.duckdb` (the trial's
`ChainReport` in `chains`), each in one transaction. The chain row is written first and replaces
any earlier one; the record transaction is the commit point. A trial already in `trials` is
skipped and nothing is written, so building a trial again is safe, including after a build
that stopped between the two files; the exception is a trial built without images, which a
build with images rebuilds (its old rows are deleted first, in their own transaction, so a stop
at any point leaves the trial either whole or absent).
"""

import hashlib
import logging
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

import duckdb
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult
from pydantic import BaseModel

from trajlab.atif.load import load_trajectory, trajectory_path
from trajlab.atif.postprocess import enriched_trajectory_path
from trajlab.contracts import (
    PREINSTALL_RECORD_FILENAME,
    RESUME_RECORD_FILENAME,
    SCHEMA_VERSION,
    ChainReport,
    CheckpointRecord,
    DeltaRow,
    PreinstallRecord,
    ResumeRecord,
    StepRow,
    StoreMeta,
    TrialRow,
)
from trajlab.store.exclusions import EXCLUDED_PATTERNS
from trajlab.store.reconcile import RECONCILE_METHOD, TEXT_RULE, ImageChain, reconcile
from trajlab.store.schema import (
    blobs_dir,
    connect,
    create_store,
    heldout_path,
    insert_rows,
    record_path,
)
from trajlab.store.steps import TrialSteps, build_steps

logger = logging.getLogger(__name__)


class BuildError(ValueError):
    """The trial dir cannot be built into a store."""


class ImageProvider(Protocol):
    """Where a trial's checkpoint images are read from (the Docker adapter, or a test's)."""

    def chain(self, checkpoints: Sequence[CheckpointRecord]) -> ImageChain:
        """The start image's layers and each checkpoint's top layer; a checkpoint whose image
        is gone has no top."""
        ...


@dataclass(frozen=True)
class BuildResult:
    """A built trial's rows; for a skipped trial, the rows already in the store."""

    trial_id: UUID
    steps: tuple[StepRow, ...]
    deltas: tuple[DeltaRow, ...]
    chain: ChainReport
    skipped_existing: bool


def start_image(trial_dir: Path) -> str | None:
    """The image the trial's container started from, which every checkpoint image of the trial
    extends by one layer: the checkpoint it resumed from (`trajlab-resume.json`), else its
    derived image (`trajlab-preinstall.json`); None when neither record is there.

    The Docker provider needs it (`DockerImageProvider(start_image=...)`), so a checkpoint image
    over another image is refused instead of diffed (ADR-0014, item 5).
    """
    resume = trial_dir / RESUME_RECORD_FILENAME
    if resume.is_file():
        return ResumeRecord.model_validate_json(resume.read_text()).checkpoint_image
    preinstall = trial_dir / PREINSTALL_RECORD_FILENAME
    if preinstall.is_file():
        return PreinstallRecord.model_validate_json(preinstall.read_text()).image
    return None


def store_meta() -> StoreMeta:
    """What this code builds stores under."""
    return StoreMeta(
        schema_version=SCHEMA_VERSION,
        exclusions=EXCLUDED_PATTERNS,
        text_rule=TEXT_RULE,
        reconcile_method=RECONCILE_METHOD,
    )


def _model(result: TrialResult) -> str:
    if result.config.agent.model_name:
        return result.config.agent.model_name
    info = result.agent_info.model_info
    if info is None:
        raise BuildError(f"trial {result.trial_name} names no model")
    return f"{info.provider}/{info.name}" if info.provider else info.name


def trial_row(
    result: TrialResult,
    prompt: str,
    *,
    attempt_idx: int = 0,
    parent_trial_id: UUID | None = None,
) -> TrialRow:
    """The `trials` row of a finished trial, from Harbor's `result.json`."""
    agent = result.agent_result
    rewards = result.verifier_result.rewards if result.verifier_result else None
    reward = rewards.get("reward") if rewards else None
    timing = result.agent_execution
    wall_ms = (
        (timing.finished_at - timing.started_at) // timedelta(milliseconds=1)
        if timing is not None and timing.started_at and timing.finished_at
        else None
    )
    return TrialRow(
        trial_id=result.id,
        trial_name=result.trial_name,
        task=result.task_name,
        attempt_idx=attempt_idx,
        parent_trial_id=parent_trial_id,
        prompt=prompt,
        model=_model(result),
        harness=result.agent_info.name,
        harness_version=result.agent_info.version,
        reward=None if reward is None else float(reward),
        tokens_in=agent.n_input_tokens if agent else None,
        tokens_out=agent.n_output_tokens if agent else None,
        cost_usd=agent.cost_usd if agent else None,
        wall_ms=wall_ms,
    )


def _calls(n: int) -> str:
    return f"{n} hooked call{'' if n == 1 else 's'}"


def chain_report(trial_id: UUID, steps: TrialSteps, chain: ImageChain | None) -> ChainReport:
    """Whether the trial's checkpoint chain is complete: every hooked call answered and every
    checkpoint's image read. `chain` is None when the images were not checked."""
    missing = (
        tuple(record.seq for record in steps.checkpoints if record.seq not in chain.tops)
        if chain is not None
        else ()
    )
    reasons: list[str] = []
    if steps.timed_out_calls:
        reasons.append(f"{_calls(len(steps.timed_out_calls))} timed out")
    if steps.unanswered_calls:
        reasons.append(f"{_calls(len(steps.unanswered_calls))} without a call record")
    if steps.interrupted_calls:
        reasons.append(
            f"{_calls(len(steps.interrupted_calls))} stopped while running (no result), "
            "so its changes may be in no checkpoint"
        )
    if chain is None:
        reasons.append("images were not checked")
    reasons.extend(f"checkpoint {seq} has no image" for seq in missing)
    return ChainReport(
        trial_id=trial_id,
        complete=not reasons,
        hooked_calls=len(steps.hooked_calls),
        answered_calls=len(steps.answered_calls),
        timed_out_calls=steps.timed_out_calls,
        unanswered_calls=steps.unanswered_calls,
        interrupted_calls=steps.interrupted_calls,
        rejected_calls=steps.rejected_calls,
        checkpoints=len(steps.checkpoints),
        images_checked=chain is not None,
        missing_images=missing,
        final_state_recorded=steps.final_state_recorded,
        reasons=tuple(reasons),
    )


@contextmanager
def _transaction(con: duckdb.DuckDBPyConnection) -> Iterator[None]:
    con.begin()
    try:
        yield
    except BaseException:
        con.rollback()
        raise
    con.commit()


def _select[M: BaseModel](
    con: duckdb.DuckDBPyConnection, table: str, model: type[M], trial_id: UUID, order: str
) -> tuple[M, ...]:
    """A trial's rows of `table` as `model`s."""
    names = list(model.model_fields)
    rows = con.execute(
        f"SELECT {', '.join(names)} FROM {table} WHERE trial_id = ? ORDER BY {order}",
        [trial_id],
    ).fetchall()
    return tuple(model.model_validate(dict(zip(names, row, strict=True))) for row in rows)


def _stored(store_dir: Path, trial_id: UUID) -> BuildResult | None:
    """The trial's rows if `trials` has it, else None."""
    with connect(record_path(store_dir), read_only=True) as con:
        found = con.execute("SELECT count(*) FROM trials WHERE trial_id = ?", [trial_id])
        if found.fetchone() == (0,):
            return None
        steps = _select(con, "steps", StepRow, trial_id, "step_idx")
        deltas = _select(con, "deltas", DeltaRow, trial_id, "step_idx, path")
    with connect(heldout_path(store_dir), read_only=True) as con:
        chains = _select(con, "chains", ChainReport, trial_id, "trial_id")
    if len(chains) != 1:
        raise BuildError(f"{store_dir}: trial {trial_id} is in trials but has no chain row")
    return BuildResult(
        trial_id=trial_id, steps=steps, deltas=deltas, chain=chains[0], skipped_existing=True
    )


def _deltas(
    steps: TrialSteps,
    images: ImageProvider | None,
    *,
    trial_id: UUID,
    trial_name: str,
    store_dir: Path,
    work_dir: Path,
) -> tuple[ImageChain | None, tuple[DeltaRow, ...]]:
    if images is None:
        return None, ()
    if not steps.checkpoints:
        # Nothing to diff: every view is the start image's.
        return ImageChain(start_layers=(), tops={}), ()
    chain = images.chain(steps.checkpoints)
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"{trial_name}.", dir=work_dir))
    try:
        result = reconcile(
            chain,
            steps.checkpoints,
            steps.owner_step,
            trial_id=trial_id,
            blobs_dir=blobs_dir(store_dir),
            work_dir=scratch,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result.skipped_seqs:
        logger.warning("%s: no delta for checkpoints %s", trial_name, list(result.skipped_seqs))
    return chain, result.deltas


def _check_source(trial_dir: Path, steps: TrialSteps) -> None:
    """The enriched trajectory must have been built from this trial's trajectory.json."""
    source = trajectory_path(trial_dir)
    if steps.source_sha256 != hashlib.sha256(source.read_bytes()).hexdigest():
        raise BuildError(
            f"{enriched_trajectory_path(trial_dir)} was not built from {source}; "
            f"run `trajlab postprocess {trial_dir}` again"
        )


def build_trial(
    trial_dir: Path,
    store_dir: Path,
    *,
    images: ImageProvider | None,
    work_dir: Path,
    attempt_idx: int = 0,
    parent_trial_id: UUID | None = None,
) -> BuildResult:
    """Build one finished, postprocessed trial into the store at `store_dir`, creating it if
    needed. Binary file versions go to the store's blobs dir; `work_dir` holds scratch files,
    removed before returning. Skips a trial already in the store (`skipped_existing`).

    Raises `BuildError` without `result.json` or the enriched trajectory, or with an enriched
    trajectory of another trajectory.json; `StepsError` if its records do not fit together;
    `StoreError` if the store was built under other settings; and lets reconcile's errors
    through. The trial dir is read and checked before the store is touched, and on any error
    nothing of this trial is in `trials`.
    """
    result_path = TrialPaths(trial_dir).result_path
    if not result_path.is_file():
        raise BuildError(f"{trial_dir}: no result.json; only a finished trial can be built")
    enriched_path = enriched_trajectory_path(trial_dir)
    if not enriched_path.is_file():
        raise BuildError(f"{enriched_path} is missing; run `trajlab postprocess {trial_dir}` first")
    result = TrialResult.model_validate_json(result_path.read_text())
    steps = build_steps(load_trajectory(enriched_path), trial_id=result.id)
    _check_source(trial_dir, steps)
    trial = trial_row(
        result, steps.prompt, attempt_idx=attempt_idx, parent_trial_id=parent_trial_id
    )

    create_store(store_dir, store_meta())
    stored = _stored(store_dir, result.id)
    if stored is not None:
        if stored.chain.images_checked or images is None:
            logger.info("%s: trial %s is already in %s; skipped", trial_dir, result.id, store_dir)
            return stored
        logger.info("%s: rebuilding trial %s, built before without images", trial_dir, result.id)
        with connect(record_path(store_dir), read_only=False) as con, _transaction(con):
            for table in ("deltas", "steps", "trials"):
                con.execute(f"DELETE FROM {table} WHERE trial_id = ?", [result.id])

    chain, deltas = _deltas(
        steps,
        images,
        trial_id=result.id,
        trial_name=result.trial_name,
        store_dir=store_dir,
        work_dir=work_dir,
    )
    report = chain_report(result.id, steps, chain)

    with connect(heldout_path(store_dir), read_only=False) as con, _transaction(con):
        con.execute("DELETE FROM chains WHERE trial_id = ?", [result.id])
        insert_rows(con, "chains", [report])
    with connect(record_path(store_dir), read_only=False) as con, _transaction(con):
        insert_rows(con, "trials", [trial])
        insert_rows(con, "steps", steps.rows)
        insert_rows(con, "deltas", deltas)
    logger.info(
        "%s: %d steps, %d deltas, chain %s",
        trial_dir,
        len(steps.rows),
        len(deltas),
        "complete" if report.complete else f"incomplete ({'; '.join(report.reasons)})",
    )
    return BuildResult(
        trial_id=result.id,
        steps=steps.rows,
        deltas=deltas,
        chain=report,
        skipped_existing=False,
    )
