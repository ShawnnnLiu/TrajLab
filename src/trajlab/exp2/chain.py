"""A retry chain on disk: its directory, its two logs, and its store (ADR-0016 item 3, ADR-0022).

    <root>/<run>/<root trial>/<arm>/
        attempts.jsonl          one AttemptResult per attempt run, in order (the loop resumes
                                from it)
        ingests.jsonl           one IngestResult per failed attempt built into the store
        store/                  record arms: the chain's store
        bundles/attempt-<t>/    record-files: the bundle attempt t sees, mounted read-only

A chain's store holds exactly its own attempts 0 to t-1 when attempt t runs, and no sibling
failure. Attempt 0's rows are copied from the corpus store (`seed_store`), not rebuilt: they are
the rows the corpus store's build made, and copying needs no images. Each failed retry is then
built in (`ingest`: postprocess, reconcile, derive, and for record-files a fresh export,
verified), with `attempt_idx` and `parent_trial_id` set. The derived views cover every row at
once (ADR-0018 item 1), so ingest appends rows and nothing is recomputed.
"""

import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import duckdb
from pydantic import BaseModel

from trajlab.atif.postprocess import postprocess_trial
from trajlab.capture.repair_launcher import checkpoint_tag, remove_checkpoint_image
from trajlab.contracts import (
    ATTEMPTS_FILENAME,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    INGESTS_FILENAME,
    AttemptResult,
    ChainReport,
    CheckpointRecord,
    DeltaRow,
    Exp2Arm,
    IngestResult,
    JobNoticeRow,
    StepRow,
    TrialRow,
    WriteRow,
)
from trajlab.store.build import ImageProvider, build_trial, start_image, store_meta
from trajlab.store.docker_images import DockerImageProvider
from trajlab.store.export import export
from trajlab.store.schema import (
    StoreError,
    blobs_dir,
    connect,
    create_store,
    heldout_path,
    read_meta,
    record_path,
)

logger = logging.getLogger(__name__)

STORE_DIRNAME = "store"
BUNDLES_DIRNAME = "bundles"
PRUNED_FILENAME = "pruned.json"
# The record tables a trial's rows are in, each with its row model (column order).
TRIAL_TABLES: dict[str, type[BaseModel]] = {
    "trials": TrialRow,
    "steps": StepRow,
    "deltas": DeltaRow,
    "job_notices": JobNoticeRow,
    "writes": WriteRow,
}


class ChainError(ValueError):
    """The chain's store does not hold what the chain says it should."""


def chain_dir(root: Path, run: str, root_trial: str, arm: Exp2Arm) -> Path:
    return root / run / root_trial / arm


def store_dir(chain: Path) -> Path:
    return chain / STORE_DIRNAME


def bundle_dir(chain: Path, attempt_idx: int) -> Path:
    """The bundle attempt `attempt_idx` sees: the store's rows of attempts 0 to that minus 1."""
    return chain / BUNDLES_DIRNAME / f"attempt-{attempt_idx}"


def _read_log[M: BaseModel](path: Path, model: type[M]) -> list[M]:
    if not path.is_file():
        return []
    return [model.model_validate_json(line) for line in path.read_text().splitlines() if line]


def _append(path: Path, row: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(row.model_dump_json() + "\n")
        log.flush()
        os.fsync(log.fileno())


def read_attempts(chain: Path) -> list[AttemptResult]:
    return _read_log(chain / ATTEMPTS_FILENAME, AttemptResult)


def append_attempt(chain: Path, result: AttemptResult) -> None:
    _append(chain / ATTEMPTS_FILENAME, result)


def read_ingests(chain: Path) -> list[IngestResult]:
    return _read_log(chain / INGESTS_FILENAME, IngestResult)


def append_ingest(chain: Path, result: IngestResult) -> None:
    _append(chain / INGESTS_FILENAME, result)


def _literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@contextmanager
def _transaction(con: duckdb.DuckDBPyConnection) -> Iterator[None]:
    con.begin()
    try:
        yield
    except BaseException:
        con.rollback()
        raise
    con.commit()


def _columns(model: type[BaseModel]) -> str:
    return ", ".join(model.model_fields)


def _has_trial(store: Path, trial_id: UUID) -> bool:
    with connect(record_path(store), read_only=True) as con:
        found = con.execute("SELECT count(*) FROM trials WHERE trial_id = ?", [trial_id])
        return found.fetchone() != (0,)


def _link_or_copy(source: Path, target: Path) -> None:
    if target.exists():
        return
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        os.link(source, tmp)
    except OSError:
        shutil.copyfile(source, tmp)
    os.replace(tmp, target)


def seed_store(corpus_store: Path, chain_store: Path, root_trial_id: UUID) -> None:
    """Create the chain's store holding its root failure's rows, copied from the corpus store:
    the trial's rows of every record table, its `chains` row, and the blobs its deltas name.

    Safe to run again: a store that already holds the trial is left as it is. The record
    transaction is the commit point, as in `trajlab.store.build`, so a seed stopped midway is
    redone whole. Raises `ChainError` if the corpus store has no such attempt 0, and
    `StoreError` if the corpus store was built under other settings than this code's (its rows
    would not be the rows this code builds for the retries beside them).
    """
    meta = store_meta()
    with connect(record_path(corpus_store), read_only=True) as con:
        if read_meta(con) != meta:
            raise StoreError(f"{corpus_store} was built under {read_meta(con)}, not {meta}")
        found = con.execute(
            "SELECT attempt_idx FROM trials WHERE trial_id = ?", [root_trial_id]
        ).fetchone()
        refs = [
            ref
            for (ref,) in con.execute(
                "SELECT DISTINCT blob_ref FROM deltas WHERE trial_id = ? AND blob_ref IS NOT NULL",
                [root_trial_id],
            ).fetchall()
        ]
    if found is None:
        raise ChainError(f"{corpus_store} has no trial {root_trial_id}")
    if found != (0,):
        raise ChainError(f"trial {root_trial_id} is attempt {found[0]} in {corpus_store}, not 0")
    create_store(chain_store, meta)
    if _has_trial(chain_store, root_trial_id):
        return
    blobs_dir(chain_store).mkdir(parents=True, exist_ok=True)
    for ref in refs:
        _link_or_copy(blobs_dir(corpus_store) / ref, blobs_dir(chain_store) / ref)
    with connect(heldout_path(chain_store), read_only=False) as con, _transaction(con):
        con.execute(f"ATTACH {_literal(heldout_path(corpus_store))} AS corpus (READ_ONLY)")
        con.execute("DELETE FROM chains WHERE trial_id = ?", [root_trial_id])
        columns = _columns(ChainReport)
        con.execute(
            f"INSERT INTO chains ({columns}) SELECT {columns} FROM corpus.chains "
            "WHERE trial_id = ?",
            [root_trial_id],
        )
    with connect(record_path(chain_store), read_only=False) as con:
        con.execute(f"ATTACH {_literal(record_path(corpus_store))} AS corpus (READ_ONLY)")
        with _transaction(con):
            for table, model in TRIAL_TABLES.items():
                columns = _columns(model)
                con.execute(
                    f"INSERT INTO {table} ({columns}) SELECT {columns} FROM corpus.{table} "
                    "WHERE trial_id = ?",
                    [root_trial_id],
                )
        con.execute("DETACH corpus")
    logger.info("seeded %s with trial %s from %s", chain_store, root_trial_id, corpus_store)


def store_trials(chain_store: Path) -> tuple[UUID, ...]:
    """The chain store's trials in attempt order, checked to be one chain: attempts 0 to k-1,
    each retry naming the attempt before it. Raises `ChainError` otherwise."""
    with connect(record_path(chain_store), read_only=True) as con:
        rows = con.execute(
            "SELECT trial_id, attempt_idx, parent_trial_id FROM trials ORDER BY attempt_idx"
        ).fetchall()
    previous: UUID | None = None
    for position, (trial_id, attempt_idx, parent) in enumerate(rows):
        if attempt_idx != position or parent != previous:
            raise ChainError(
                f"{chain_store} is not one chain: attempt {attempt_idx} (expected {position}) "
                f"with parent {parent} (expected {previous})"
            )
        previous = trial_id
    return tuple(trial_id for trial_id, _, _ in rows)


OpenImages = Callable[[Path, Path], AbstractContextManager[ImageProvider]]
"""Opens the image provider of a trial dir, given a scratch dir: the Docker one in production
(`docker_images`), in-memory layers in tests."""


def docker_images(trial_dir: Path, scratch: Path) -> AbstractContextManager[ImageProvider]:
    image = start_image(trial_dir)
    if image is None:
        raise ChainError(
            f"{trial_dir}: neither trajlab-preinstall.json nor trajlab-resume.json names its "
            "start image"
        )
    return DockerImageProvider(scratch, start_image=image)


def _check_before(before: tuple[UUID, ...], trial_id: UUID, attempt_idx: int, parent: UUID) -> None:
    """The store holds attempts 0 to `attempt_idx` - 1, the last `parent`; or, for an ingest
    stopped after its build, this attempt too."""
    held = before[:-1] if before and before[-1] == trial_id else before
    if len(held) != attempt_idx or held[-1] != parent:
        raise ChainError(
            f"the store holds {len(held)} attempts ending with {held[-1] if held else None}; "
            f"attempt {attempt_idx} follows {parent}"
        )


def ingest(
    trial_dir: Path,
    chain_store: Path,
    *,
    trial_id: UUID,
    attempt_idx: int,
    parent_trial_id: UUID,
    work_dir: Path,
    bundle_out: Path | None,
    open_images: OpenImages = docker_images,
) -> IngestResult:
    """Build one failed attempt into its chain's store and, for record-files, export the bundle
    the next attempt sees (technical spec §5.2). Never raises for the attempt's own problems:
    a failure is the result's `error`, and the chain stops on it, so no attempt runs with a
    partial store.

    The store must hold exactly attempts 0 to `attempt_idx` - 1, the last `parent_trial_id`
    (or, after an ingest stopped once its build was done, this attempt too, which the build
    then skips). A bundle left by an earlier, stopped ingest is replaced, since it was never
    verified against the rows now there.
    """
    started_at = datetime.now(UTC)
    clock = time.monotonic()

    def failed(error: Exception) -> IngestResult:
        logger.error("ingest of %s into %s failed: %s", trial_dir, chain_store, error)
        return IngestResult(
            attempt_idx=attempt_idx,
            trial_id=trial_id,
            trial_name=trial_dir.name,
            ok=False,
            error=f"{type(error).__name__}: {error}",
            started_at=started_at,
            seconds=round(time.monotonic() - clock, 1),
        )

    try:
        _check_before(store_trials(chain_store), trial_id, attempt_idx, parent_trial_id)
        postprocess_trial(trial_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=f"{trial_dir.name}.", dir=work_dir))
        try:
            with open_images(trial_dir, scratch) as images:
                build = build_trial(
                    trial_dir,
                    chain_store,
                    images=images,
                    work_dir=scratch,
                    attempt_idx=attempt_idx,
                    parent_trial_id=parent_trial_id,
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if build.trial_id != trial_id:
            raise ChainError(f"{trial_dir} built as trial {build.trial_id}, not {trial_id}")
        after = store_trials(chain_store)
        if len(after) != attempt_idx + 1 or after[-1] != trial_id:
            raise ChainError(f"the store holds {len(after)} attempts after attempt {attempt_idx}")
        if bundle_out is not None:
            if bundle_out.exists():
                shutil.rmtree(bundle_out)
            export(chain_store, bundle_out)
    except (ValueError, OSError, duckdb.Error) as error:
        return failed(error)
    return IngestResult(
        attempt_idx=attempt_idx,
        trial_id=trial_id,
        trial_name=trial_dir.name,
        ok=True,
        steps=len(build.steps),
        deltas=len(build.deltas),
        chain_complete=build.chain.complete,
        chain_reasons=build.chain.reasons,
        bundle=None if bundle_out is None else str(bundle_out),
        started_at=started_at,
        seconds=round(time.monotonic() - clock, 1),
    )


def export_bundle(chain_store: Path, out: Path) -> Path:
    """The bundle of the chain's store at `out`, written and verified if it is not there."""
    if not out.exists():
        export(chain_store, out)
    return out


def _records(trial_dir: Path) -> Sequence[CheckpointRecord]:
    path = trial_dir / "agent" / CHECKPOINTS_DIRNAME / CHECKPOINT_RECORDS_FILENAME
    if not path.is_file():
        return ()
    return [
        CheckpointRecord.model_validate_json(line) for line in path.read_text().splitlines() if line
    ]


def prune_ingested(
    trial_dir: Path,
    *,
    tag_lookup: Callable[[str], str | None] = checkpoint_tag,
    remove_image: Callable[[str], bool] = remove_checkpoint_image,
) -> int:
    """Remove every checkpoint image of a retry whose rows are in its chain's store; opt-in
    (`trajlab exp2 run --prune-ingested`, ADR-0022). Never called on a root failure. Every
    checkpoint record stays, and `agent/checkpoints/pruned.json` lists what was removed, in the
    shape Exp 1's prune writes, with `kept_seq` null. Returns the number removed."""
    directory = trial_dir / "agent" / CHECKPOINTS_DIRNAME
    pruned_path = directory / PRUNED_FILENAME
    if pruned_path.is_file():
        return 0
    removed = []
    for record in _records(trial_dir):
        tag = tag_lookup(record.checkpoint_id)
        if tag is not None and remove_image(tag):
            removed.append({"seq": record.seq, "checkpoint_id": record.checkpoint_id})
    if directory.is_dir():
        pruned_path.write_text(
            json.dumps(
                {
                    "kept_seq": None,
                    "removed": removed,
                    "reason": "ingested into its Exp 2 chain store (trajlab exp2 run "
                    "--prune-ingested)",
                    "at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
            + "\n"
        )
    logger.info("pruned %d checkpoint images of %s", len(removed), trial_dir.name)
    return len(removed)
