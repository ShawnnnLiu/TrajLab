"""A retry chain's store (ADR-0016 item 3, ADR-0022): attempt 0's rows copied from the corpus
store, each failed retry built in with `attempt_idx` and `parent_trial_id`, and the bundle the
next attempt sees exported from it."""

import json
import shutil
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from tests.conftest import FIXTURE_TRIAL, FIXTURE_TRIAL_NAME, edit_trajectory
from tests.test_store_build import TRIAL_ID, all_images, capture
from trajlab.exp2 import chain as chains
from trajlab.store import schema
from trajlab.store.build import build_trial
from trajlab.store.export import verify

RETRY_ID = UUID("7e7e7e7e-0000-4000-8000-000000000001")
RETRY2_ID = UUID("7e7e7e7e-0000-4000-8000-000000000002")


def as_attempt(
    trial_dir: Path, *, trial_id: UUID = TRIAL_ID, reward: float = 0.0, first: str | None = None
) -> Path:
    """The fixture trial as an attempt: its id, its reward, and, for a retry, the prompt it got
    as its first user step; captured (one Write call and a stop) and postprocessed."""
    path = trial_dir / "result.json"
    result = json.loads(path.read_text())
    result["id"] = str(trial_id)
    result["verifier_result"] = {"rewards": {"reward": reward}}
    path.write_text(json.dumps(result))
    if first is not None:

        def set_first(data: dict[str, Any]) -> None:
            next(s for s in data["steps"] if s["source"] == "user")["message"] = first

        edit_trajectory(trial_dir, set_first)
    return capture(trial_dir)


def copy_trial(parent: Path) -> Path:
    """The fixture trial in `parent` under its trial name, as Harbor names trial dirs."""
    return Path(shutil.copytree(FIXTURE_TRIAL, parent / FIXTURE_TRIAL_NAME))


def memory_images(trial_dir: Path, scratch: Path) -> Any:
    return nullcontext(all_images())


@pytest.fixture
def corpus_store(tmp_path: Path) -> Path:
    """A corpus store holding one failed first attempt: the fixture trial at reward 0."""
    root = as_attempt(copy_trial(tmp_path / "corpus-job"))
    store = tmp_path / "corpus-store"
    build_trial(root, store, images=all_images(), work_dir=tmp_path / "work")
    return store


def rows(store: Path, sql: str) -> list[tuple[Any, ...]]:
    with schema.connect(schema.record_path(store), read_only=True) as con:
        return con.execute(sql).fetchall()


def test_seeding_copies_the_root_failures_rows(corpus_store: Path, tmp_path: Path) -> None:
    chain_store = tmp_path / "chain" / "store"
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    for table in chains.TRIAL_TABLES:
        sql = f"SELECT * FROM {table} ORDER BY ALL"
        assert rows(chain_store, sql) == rows(corpus_store, sql), table
    assert chains.store_trials(chain_store) == (TRIAL_ID,)
    with schema.connect(schema.heldout_path(chain_store), read_only=True) as con:
        assert con.execute("SELECT trial_id, complete FROM chains").fetchall() == [(TRIAL_ID, True)]
    # Seeding again changes nothing.
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    assert rows(chain_store, "SELECT count(*) FROM steps") == rows(
        corpus_store, "SELECT count(*) FROM steps"
    )


def test_seeding_copies_the_blobs_the_rows_name(corpus_store: Path, tmp_path: Path) -> None:
    ref = f"{schema.BLOBS_DIRNAME}/{'ab' * 32}"  # as reconcile names them
    (corpus_store / ref).write_bytes(b"later version")
    with schema.connect(schema.record_path(corpus_store), read_only=False) as con:
        con.execute("UPDATE deltas SET blob_ref = ?", [ref])
    chain_store = tmp_path / "chain-store"
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    assert (chain_store / ref).read_bytes() == b"later version"


def test_seeding_refuses_a_trial_that_is_not_attempt_0(corpus_store: Path, tmp_path: Path) -> None:
    with pytest.raises(chains.ChainError, match="has no trial"):
        chains.seed_store(corpus_store, tmp_path / "s", RETRY_ID)


def test_a_failed_retry_is_ingested_with_its_parent(corpus_store: Path, tmp_path: Path) -> None:
    chain_store = tmp_path / "store"
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    retry = as_attempt(copy_trial(tmp_path / "job-a1"), trial_id=RETRY_ID, first="Retry 1.\n")
    bundle = tmp_path / "bundles" / "attempt-2"
    result = chains.ingest(
        retry,
        chain_store,
        trial_id=RETRY_ID,
        attempt_idx=1,
        parent_trial_id=TRIAL_ID,
        work_dir=tmp_path / "work",
        bundle_out=bundle,
        open_images=memory_images,
    )
    assert result.ok, result.error
    assert (result.steps, result.deltas, result.chain_complete) == (3, 1, True)
    assert chains.store_trials(chain_store) == (TRIAL_ID, RETRY_ID)
    assert rows(
        chain_store, "SELECT attempt_idx, parent_trial_id, prompt FROM trials ORDER BY 1"
    ) == [
        (0, None, 'Create a file called hello.txt with "Hello, world!" as the content.\n'),
        (1, TRIAL_ID, "Retry 1.\n"),
    ]
    # The derived views cover the retry's rows as soon as they are in.
    assert rows(chain_store, "SELECT count(DISTINCT trial_id) FROM step_flags") == [(2,)]
    assert verify(chain_store, bundle) == []
    trials = [json.loads(line) for line in (bundle / "trials.jsonl").read_text().splitlines()]
    assert sorted(t["attempt_idx"] for t in trials) == [0, 1]


def test_ingesting_into_the_wrong_chain_position_is_refused(
    corpus_store: Path, tmp_path: Path
) -> None:
    chain_store = tmp_path / "store"
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    retry = as_attempt(copy_trial(tmp_path / "job-a2"), trial_id=RETRY2_ID)
    result = chains.ingest(
        retry,
        chain_store,
        trial_id=RETRY2_ID,
        attempt_idx=2,
        parent_trial_id=RETRY_ID,
        work_dir=tmp_path / "work",
        bundle_out=None,
        open_images=memory_images,
    )
    assert not result.ok
    assert result.error is not None and "attempt 2 follows" in result.error
    assert chains.store_trials(chain_store) == (TRIAL_ID,)


def test_a_failed_build_leaves_the_store_as_it_was(corpus_store: Path, tmp_path: Path) -> None:
    chain_store = tmp_path / "store"
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    retry = as_attempt(copy_trial(tmp_path / "job-a1"), trial_id=RETRY_ID)

    def broken(trial_dir: Path, scratch: Path) -> Any:
        raise OSError("docker image save: no space left on device")

    result = chains.ingest(
        retry,
        chain_store,
        trial_id=RETRY_ID,
        attempt_idx=1,
        parent_trial_id=TRIAL_ID,
        work_dir=tmp_path / "work",
        bundle_out=tmp_path / "bundle",
        open_images=broken,
    )
    assert not result.ok and result.error == "OSError: docker image save: no space left on device"
    assert chains.store_trials(chain_store) == (TRIAL_ID,)
    assert not (tmp_path / "bundle").exists()


def test_an_ingest_stopped_after_its_build_is_finished_on_the_next_try(
    corpus_store: Path, tmp_path: Path
) -> None:
    chain_store = tmp_path / "store"
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    retry = as_attempt(copy_trial(tmp_path / "job-a1"), trial_id=RETRY_ID)
    build_trial(
        retry,
        chain_store,
        images=all_images(),
        work_dir=tmp_path / "w",
        attempt_idx=1,
        parent_trial_id=TRIAL_ID,
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()  # a bundle a stopped export left, never verified
    (bundle / "stale").write_text("x")
    result = chains.ingest(
        retry,
        chain_store,
        trial_id=RETRY_ID,
        attempt_idx=1,
        parent_trial_id=TRIAL_ID,
        work_dir=tmp_path / "work",
        bundle_out=bundle,
        open_images=memory_images,
    )
    assert result.ok, result.error
    assert not (bundle / "stale").exists() and verify(chain_store, bundle) == []


def test_store_trials_refuses_a_store_that_is_not_one_chain(
    corpus_store: Path, tmp_path: Path
) -> None:
    chain_store = tmp_path / "store"
    chains.seed_store(corpus_store, chain_store, TRIAL_ID)
    with schema.connect(schema.record_path(chain_store), read_only=False) as con:
        con.execute("UPDATE trials SET attempt_idx = 1, parent_trial_id = ?", [RETRY_ID])
    with pytest.raises(chains.ChainError, match="not one chain"):
        chains.store_trials(chain_store)


def test_the_logs_append_and_read_back(tmp_path: Path) -> None:
    from tests.test_contracts_exp2 import attempt, ingest_ok

    chain = chains.chain_dir(tmp_path, "run", "x__1", "record-files")
    assert chains.read_attempts(chain) == [] and chains.read_ingests(chain) == []
    first, second = attempt(1), attempt(1, outcome="infra")
    chains.append_attempt(chain, first)
    chains.append_attempt(chain, second)
    chains.append_ingest(chain, ingest_ok(1))
    assert chains.read_attempts(chain) == [first, second]
    assert chains.read_ingests(chain) == [ingest_ok(1)]
    assert chain == tmp_path / "run" / "x__1" / "record-files"


def test_pruning_an_ingested_retry_removes_its_images_and_lists_them(trial_copy: Path) -> None:
    capture(trial_copy)
    removed: list[str] = []
    count = chains.prune_ingested(
        trial_copy,
        tag_lookup=lambda image: f"trajlab-checkpoint:{image[-1]}",
        remove_image=lambda tag: removed.append(tag) is None,
    )
    assert count == 2 and removed == ["trajlab-checkpoint:1", "trajlab-checkpoint:2"]
    pruned = json.loads((trial_copy / "agent" / "checkpoints" / "pruned.json").read_text())
    assert pruned["kept_seq"] is None and [r["seq"] for r in pruned["removed"]] == [1, 2]
    # Records stay; a second prune does nothing.
    assert (trial_copy / "agent" / "checkpoints" / "checkpoints.jsonl").is_file()
    assert (
        chains.prune_ingested(trial_copy, tag_lookup=lambda _: "t", remove_image=lambda _: True)
        == 0
    )
