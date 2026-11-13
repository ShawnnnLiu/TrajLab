"""Building trials into the store: the acceptance check of Exp 2 build-order step 1.

Images come from an in-memory `ImageProvider` whose layers are tars built here, shaped like the
layers Docker 29 (containerd store) writes for `docker commit`: gzip-compressed, member names
without a leading `/`, parent directories present with their children, and each checkpoint's
top layer holding the container's whole writable layer.
"""

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID

import pytest
from harbor.models.trial.result import TrialResult

from tests.conftest import (
    FIXTURE_TOOL_CALL_ID,
    edit_trajectory,
    make_call,
    make_checkpoint,
    write_capture,
)
from trajlab.atif.postprocess import postprocess_trial
from trajlab.contracts import (
    SCHEMA_VERSION,
    ChainReport,
    CheckpointRecord,
    DeltaRow,
    StoreMeta,
    TrialRow,
)
from trajlab.store import build, schema
from trajlab.store.build import BuildError, build_trial, chain_report, store_meta, trial_row
from trajlab.store.derive import VIEWS, derived_id
from trajlab.store.docker_images import DockerImagesError
from trajlab.store.exclusions import EXCLUDED_PATTERNS
from trajlab.store.reconcile import (
    RECONCILE_METHOD,
    TEXT_RULE,
    ImageChain,
    LayerSource,
    ReconcileError,
)
from trajlab.store.steps import TrialSteps

TRIAL_ID = UUID("e1eee115-a864-4c21-896b-5b60c8741d97")
PARENT_ID = UUID("0b5e1d0a-0000-4000-8000-00000000c0de")
STOP_ID = "stop_1790000000_42"
PROMPT = 'Create a file called hello.txt with "Hello, world!" as the content.\n'
HELLO = b"Hello, world!\n"
HUNK = "--- /dev/null\n+++ b/app/hello.txt\n@@ -0,0 +1 @@\n+Hello, world!\n"
BASE_MTIME = 1_700_000_000
TOP_MTIME = 1_790_000_000  # copy-ups move directory times; times are never compared


# -- in-memory layers --------------------------------------------------------------------------


class MemoryLayer:
    """A `LayerSource` over tar bytes."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self) -> BinaryIO:
        return io.BytesIO(self.data)


Member = tuple[str, str, int, bytes]


def d(name: str, mode: int = 0o755) -> Member:
    return ("d", name, mode, b"")


def f(name: str, data: bytes, mode: int = 0o644) -> Member:
    return ("f", name, mode, data)


def layer(*members: Member, mtime: int) -> MemoryLayer:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for kind, name, mode, data in members:
            info = tarfile.TarInfo(name)
            info.mode, info.uid, info.gid, info.mtime = mode, 0, 0, mtime
            if kind == "d":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return MemoryLayer(gzip.compress(buffer.getvalue()))  # Docker's layer blobs are gzip


# The hello-world task image, as far as this trial touches it: Ubuntu with WORKDIR /app.
BASE = layer(
    d("app"),
    d("etc"),
    f("etc/os-release", b'PRETTY_NAME="Ubuntu 24.04.3 LTS"\n'),
    f("etc/hostname", b"localhost\n"),
    d("root", 0o700),
    f("root/.bashrc", b"# ~/.bashrc\n"),
    d("tmp", 0o1777),
    mtime=BASE_MTIME,
)


def top_after_write(mtime: int) -> MemoryLayer:
    """The container's writable layer after the Write call: the agent's file, and what Claude
    Code and Harbor wrote around it."""
    return layer(
        d("app"),
        f("app/hello.txt", HELLO),
        d("root", 0o700),
        d("root/.cache"),
        d("root/.cache/claude"),
        f("root/.cache/claude/staging.json", b"{}"),
        d("tmp", 0o1777),
        d("tmp/claude-0", 0o700),
        d("tmp/claude-0/-app"),
        d("tmp/claude-0/-app/tasks"),
        f("tmp/claude-0/-app/tasks/b1.output", b"\n"),
        d("tmp/claude-code-settings"),
        f("tmp/claude-code-settings/settings.json", b'{"hooks": {}}'),
        mtime=mtime,
    )


class MemoryImages:
    """An `ImageProvider` over in-memory layers, keyed by checkpoint image id."""

    def __init__(self, tops: dict[str, LayerSource]) -> None:
        self.tops = tops
        self.requests: list[tuple[CheckpointRecord, ...]] = []

    def chain(self, checkpoints: Sequence[CheckpointRecord]) -> ImageChain:
        self.requests.append(tuple(checkpoints))
        return ImageChain(
            start_layers=(BASE,),
            tops={
                r.seq: self.tops[r.checkpoint_id]
                for r in checkpoints
                if r.checkpoint_id in self.tops
            },
        )


def image(seq: int) -> str:
    return f"sha256:{seq:064x}"  # make_checkpoint's checkpoint_id


def all_images() -> MemoryImages:
    # The stop changed nothing the agent did, so its top layer holds the same entries.
    return MemoryImages(
        {image(1): top_after_write(TOP_MTIME), image(2): top_after_write(TOP_MTIME + 60)}
    )


STOP_CHECKPOINT = make_checkpoint(2, STOP_ID, trigger="stop", tool_name="Stop")


def capture(trial_dir: Path) -> Path:
    """The Write call checkpointed as seq 1, then a stop checkpointed as seq 2; postprocessed."""
    stop_call = make_call(2, STOP_ID, trigger="stop", tool_name="Stop", change="changed")
    write_capture(trial_dir, [make_checkpoint(1), STOP_CHECKPOINT], [make_call(1), stop_call])
    postprocess_trial(trial_dir)
    return trial_dir


def count(store: Path, table: str) -> int:
    path = (
        schema.record_path(store) if table in schema.RECORD_TABLES else schema.heldout_path(store)
    )
    with schema.connect(path, read_only=True) as con:
        (n,) = con.execute(f"SELECT count(*) FROM {table}").fetchone()  # type: ignore[misc]
    return n


# -- the acceptance check ----------------------------------------------------------------------


def test_fixture_trial_builds_to_the_hand_count(trial_copy: Path, tmp_path: Path) -> None:
    capture(trial_copy)
    images = all_images()
    store = tmp_path / "store"

    result = build_trial(trial_copy, store, images=images, work_dir=tmp_path / "work")

    # Hand count, by the rules in trajlab.store.reconcile's docstring:
    #   seq 1 (base -> top 1): /app is a copy-up with the base's kind, mode, and owner: no row.
    #     /app/hello.txt is new: 1 row, add, text, owned by step 1 (the Write call). /root and
    #     /tmp are copy-ups: no row. /root/.cache is a new directory holding only excluded
    #     paths: no row. /root/.cache/claude, /tmp/claude-0, /tmp/claude-code-settings, and
    #     everything under them are excluded harness paths: no row.
    #   seq 2 (top 1 -> top 2): the same entries, only times differ: no row.
    # deltas = 1; steps = 3 (Write call, final message, stop); the chain is complete.
    assert result.skipped_existing is False
    assert result.trial_id == TRIAL_ID
    assert len(result.steps) == 3
    assert result.deltas == (
        DeltaRow(
            trial_id=TRIAL_ID,
            step_idx=1,
            path="/app/hello.txt",
            op="add",
            kind_after="file",
            is_text=True,
            hunk=HUNK,
            sha256_after=hashlib.sha256(HELLO).hexdigest(),
            bytes_after=len(HELLO),
            mode_after="0644",
            owner_after="0:0",
            mtime_after=datetime.fromtimestamp(TOP_MTIME, tz=UTC),
        ),
    )
    assert result.job_notices == ()  # the fixture's session has no background job
    assert result.chain == ChainReport(
        trial_id=TRIAL_ID,
        complete=True,
        hooked_calls=1,
        answered_calls=1,
        checkpoints=2,
        images_checked=True,
        final_state_recorded=True,
    )
    assert images.requests == [(make_checkpoint(1), STOP_CHECKPOINT)]

    with schema.connect(schema.record_path(store), read_only=True) as con:
        assert schema.read_meta(con) == store_meta()
        assert con.execute(
            "SELECT trial_id, trial_name, task, attempt_idx, parent_trial_id, prompt, model, "
            "harness, harness_version, reward, tokens_in, tokens_out, cost_usd, wall_ms "
            "FROM trials"
        ).fetchall() == [
            (
                TRIAL_ID,
                "hello-world__K3GBok3",
                "hello-world/hello-world",
                0,
                None,
                PROMPT,
                "anthropic/claude-sonnet-5",
                "claude-code",
                "2.1.278",
                1.0,
                54361,
                115,
                0.0320233,
                4090,
            )
        ]
        assert con.execute(
            "SELECT step_idx, atif_step_id, source, tool_call_id, tool, message, "
            "checkpoint_seq, checkpoint_id FROM steps ORDER BY step_idx"
        ).fetchall() == [
            (
                1,
                2,
                "agent",
                FIXTURE_TOOL_CALL_ID,
                "Write",
                "I'll create the file now.",
                1,
                image(1),
            ),
            (
                2,
                3,
                "agent",
                None,
                None,
                'Created `/app/hello.txt` with the content "Hello, world!".',
                1,
                image(1),
            ),
            (3, None, "system", STOP_ID, "Stop", None, 2, image(2)),
        ]
        assert con.execute(
            "SELECT trial_id, step_idx, path, op, kind_before, kind_after, is_text, hunk, "
            "blob_ref, bytes_before, bytes_after FROM deltas"
        ).fetchall() == [
            (TRIAL_ID, 1, "/app/hello.txt", "add", None, "file", True, HUNK, None, None, 14)
        ]
        # The delta joins back to the call that made it through the step row.
        assert con.execute(
            "SELECT s.tool_call_id FROM deltas d JOIN steps s USING (trial_id, step_idx)"
        ).fetchall() == [(FIXTURE_TOOL_CALL_ID,)]
    with schema.connect(schema.heldout_path(store), read_only=True) as con:
        assert schema.read_meta(con) == store_meta()
        assert con.execute("SELECT * FROM chains").fetchall() == [
            (TRIAL_ID, True, 1, 1, [], [], [], [], 2, True, [], True, [])
        ]
    assert not any(schema.blobs_dir(store).iterdir())  # the one version is text
    assert not any((tmp_path / "work").iterdir())  # scratch is removed

    # Building it again writes nothing and returns the rows the store holds.
    again = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "work")

    assert again.skipped_existing is True
    assert (again.steps, again.deltas, again.chain) == (result.steps, result.deltas, result.chain)
    assert [count(store, table) for table in ("trials", "steps", "deltas", "chains")] == [
        1,
        3,
        1,
        1,
    ]


def test_a_missing_image_leaves_the_earlier_deltas_and_flags_the_chain(
    trial_copy: Path, tmp_path: Path
) -> None:
    capture(trial_copy)
    images = MemoryImages({image(1): top_after_write(TOP_MTIME)})

    result = build_trial(trial_copy, tmp_path / "store", images=images, work_dir=tmp_path / "w")

    assert [(delta.step_idx, delta.path, delta.op) for delta in result.deltas] == [
        (1, "/app/hello.txt", "add")
    ]
    assert result.chain.complete is False
    assert result.chain.images_checked is True
    assert result.chain.missing_images == (2,)
    assert result.chain.reasons == ("checkpoint 2 has no image",)
    assert count(tmp_path / "store", "deltas") == 1


# -- without images ----------------------------------------------------------------------------


def test_without_images_steps_are_built_and_the_chain_is_unchecked(
    trial_copy: Path, tmp_path: Path
) -> None:
    capture(trial_copy)
    store = tmp_path / "store"

    result = build_trial(trial_copy, store, images=None, work_dir=tmp_path / "work")

    assert [(row.step_idx, row.tool, row.checkpoint_seq) for row in result.steps] == [
        (1, "Write", 1),
        (2, None, 1),
        (3, "Stop", 2),
    ]
    assert result.deltas == ()
    assert result.chain == ChainReport(
        trial_id=TRIAL_ID,
        complete=False,
        hooked_calls=1,
        answered_calls=1,
        checkpoints=2,
        images_checked=False,
        final_state_recorded=True,
        reasons=("images were not checked",),
    )
    assert [count(store, table) for table in ("trials", "steps", "deltas", "chains")] == [
        1,
        3,
        0,
        1,
    ]
    assert not (tmp_path / "work").exists()


def test_building_again_is_skipped(trial_copy: Path, tmp_path: Path) -> None:
    capture(trial_copy)
    store = tmp_path / "store"
    first = build_trial(trial_copy, store, images=None, work_dir=tmp_path / "work")

    again = build_trial(trial_copy, store, images=None, work_dir=tmp_path / "work")

    assert again.skipped_existing is True
    # The stored rows read back equal to the built ones, timestamps included.
    assert (again.steps, again.deltas, again.chain) == (first.steps, first.deltas, first.chain)
    assert [count(store, table) for table in ("trials", "steps", "deltas", "chains")] == [
        1,
        3,
        0,
        1,
    ]


def test_a_build_stopped_after_the_chain_row_can_be_rerun(trial_copy: Path, tmp_path: Path) -> None:
    # The chain row is written before the record transaction; a build that stopped between
    # them left a chain row and no trial. Rebuilding replaces the chain row.
    capture(trial_copy)
    store = tmp_path / "store"
    schema.create_store(store, store_meta())
    stale = ChainReport(
        trial_id=TRIAL_ID,
        complete=False,
        hooked_calls=0,
        answered_calls=0,
        checkpoints=0,
        images_checked=False,
        final_state_recorded=False,
        reasons=("stale",),
    )
    with schema.connect(schema.heldout_path(store), read_only=False) as con:
        schema.insert_rows(con, "chains", [stale])

    result = build_trial(trial_copy, store, images=None, work_dir=tmp_path / "work")

    assert result.skipped_existing is False
    with schema.connect(schema.heldout_path(store), read_only=True) as con:
        assert con.execute("SELECT checkpoints, reasons FROM chains").fetchall() == [
            (2, ["images were not checked"])
        ]


def test_a_stock_trial_needs_no_images(trial_copy: Path, tmp_path: Path) -> None:
    postprocess_trial(trial_copy)  # no capture at all
    images = all_images()

    result = build_trial(trial_copy, tmp_path / "store", images=images, work_dir=tmp_path / "w")

    assert images.requests == []
    assert result.deltas == ()
    assert result.chain.images_checked is True
    assert result.chain.checkpoints == 0
    assert result.chain.final_state_recorded is False
    assert result.chain.unanswered_calls == (FIXTURE_TOOL_CALL_ID,)
    assert result.chain.reasons == ("1 hooked call without a call record",)


def test_a_timed_out_call_makes_the_chain_incomplete(trial_copy: Path, tmp_path: Path) -> None:
    write_capture(trial_copy, timeouts=[FIXTURE_TOOL_CALL_ID])
    postprocess_trial(trial_copy)

    result = build_trial(trial_copy, tmp_path / "store", images=None, work_dir=tmp_path / "w")

    assert result.chain.timed_out_calls == (FIXTURE_TOOL_CALL_ID,)
    assert result.chain.unanswered_calls == ()
    assert result.chain.answered_calls == 0
    assert result.chain.reasons == ("1 hooked call timed out", "images were not checked")
    assert result.steps[0].checkpoint_seq is None


# -- refusals ----------------------------------------------------------------------------------


def test_a_trial_without_result_json_is_refused(trial_copy: Path, tmp_path: Path) -> None:
    capture(trial_copy)
    (trial_copy / "result.json").unlink()

    with pytest.raises(BuildError, match="no result.json"):
        build_trial(trial_copy, tmp_path / "store", images=None, work_dir=tmp_path / "w")
    assert not (tmp_path / "store").exists()


def test_a_trial_without_the_enriched_trajectory_is_refused(
    trial_copy: Path, tmp_path: Path
) -> None:
    write_capture(trial_copy, [make_checkpoint(1)], [make_call(1)])  # not postprocessed

    with pytest.raises(
        BuildError, match=r"trajectory\.enriched\.json is missing.*trajlab postprocess"
    ):
        build_trial(trial_copy, tmp_path / "store", images=None, work_dir=tmp_path / "w")
    assert not (tmp_path / "store").exists()
    assert not (trial_copy / "agent" / "trajectory.enriched.json").exists()


def test_an_enriched_trajectory_of_another_trajectory_is_refused(
    trial_copy: Path, tmp_path: Path
) -> None:
    capture(trial_copy)
    edit_trajectory(trial_copy, lambda data: None)  # same content, other bytes

    with pytest.raises(BuildError, match="was not built from.*trajlab postprocess"):
        build_trial(trial_copy, tmp_path / "store", images=None, work_dir=tmp_path / "w")
    assert not (tmp_path / "store").exists()


def test_a_failed_reconcile_writes_nothing(
    trial_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture(trial_copy)
    seen: list[Path] = []

    def fail(*args: Any, work_dir: Path, **kwargs: Any) -> Any:
        seen.append(work_dir)
        (work_dir / "partial.txt").write_text("x")
        raise ReconcileError("a checkpoint with no owning step")

    monkeypatch.setattr(build, "reconcile", fail)

    with pytest.raises(ReconcileError):
        build_trial(trial_copy, tmp_path / "store", images=all_images(), work_dir=tmp_path / "w")
    assert [count(tmp_path / "store", t) for t in ("trials", "steps", "chains")] == [0, 0, 0]
    assert seen and seen[0].parent == tmp_path / "w"
    assert not any((tmp_path / "w").iterdir())


def test_a_store_built_under_other_settings_is_refused(trial_copy: Path, tmp_path: Path) -> None:
    capture(trial_copy)
    store = tmp_path / "store"
    schema.create_store(store, store_meta().model_copy(update={"text_rule": "ascii only"}))

    with pytest.raises(schema.StoreError):
        build_trial(trial_copy, store, images=None, work_dir=tmp_path / "w")


def test_the_two_files_hold_separate_tables(trial_copy: Path, tmp_path: Path) -> None:
    capture(trial_copy)
    store = tmp_path / "store"
    build_trial(trial_copy, store, images=None, work_dir=tmp_path / "w")

    with schema.connect(schema.record_path(store), read_only=True) as con:
        record = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    with schema.connect(schema.heldout_path(store), read_only=True) as con:
        heldout = {row[0] for row in con.execute("SHOW TABLES").fetchall()}

    assert record.isdisjoint({"checks", "faults", "blame", "chains"})
    assert heldout.isdisjoint({"trials", "steps", "deltas", "job_notices", "writes"})
    assert record == {
        "trials",
        "steps",
        "deltas",
        "job_notices",
        "writes",
        "store_meta",
        *(v.name for v in VIEWS),
    }


# -- parts -------------------------------------------------------------------------------------


def test_store_meta() -> None:
    assert store_meta() == StoreMeta(
        schema_version=SCHEMA_VERSION,
        exclusions=EXCLUDED_PATTERNS,
        text_rule=TEXT_RULE,
        reconcile_method=RECONCILE_METHOD,
        derived=derived_id(),
    )


@pytest.fixture
def result(fixture_trial: Path) -> Any:
    return TrialResult.model_validate_json((fixture_trial / "result.json").read_text())


def test_trial_row_from_the_fixture(result: Any) -> None:
    assert trial_row(result, PROMPT) == TrialRow(
        trial_id=TRIAL_ID,
        trial_name="hello-world__K3GBok3",
        task="hello-world/hello-world",
        attempt_idx=0,
        prompt=PROMPT,
        model="anthropic/claude-sonnet-5",
        harness="claude-code",
        harness_version="2.1.278",
        reward=1.0,
        tokens_in=54361,
        tokens_out=115,
        cost_usd=0.0320233,
        wall_ms=4090,  # 00:49:14.546032 to 00:49:18.636546
    )


def test_trial_row_of_a_retry(result: Any) -> None:
    row = trial_row(result, PROMPT, attempt_idx=2, parent_trial_id=PARENT_ID)
    assert (row.attempt_idx, row.parent_trial_id) == (2, PARENT_ID)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("anthropic", "anthropic/claude-sonnet-5"), (None, "claude-sonnet-5")],
)
def test_trial_row_model_falls_back_to_agent_info(
    result: Any, provider: str | None, expected: str
) -> None:
    result.config.agent.model_name = None
    result.agent_info.model_info.provider = provider

    assert trial_row(result, PROMPT).model == expected


def test_trial_row_without_a_model_is_refused(result: Any) -> None:
    result.config.agent.model_name = None
    result.agent_info.model_info = None

    with pytest.raises(BuildError, match="names no model"):
        trial_row(result, PROMPT)


def test_trial_row_of_a_trial_that_did_not_finish(result: Any) -> None:
    result.verifier_result = None
    result.agent_result = None
    result.agent_execution.finished_at = None

    row = trial_row(result, PROMPT)

    assert (row.reward, row.tokens_in, row.tokens_out, row.cost_usd, row.wall_ms) == (None,) * 5


def test_trial_row_reward_is_the_reward_key(result: Any) -> None:
    result.verifier_result.rewards = {"accuracy": 0.25}
    assert trial_row(result, PROMPT).reward is None
    result.verifier_result.rewards = {"reward": 0}
    assert trial_row(result, PROMPT).reward == 0.0


def test_chain_report_reasons() -> None:
    steps = TrialSteps(
        source_sha256="0" * 64,
        prompt="",
        rows=(),
        owner_step={},
        checkpoints=(make_checkpoint(1), make_checkpoint(2, "toolu_b"), make_checkpoint(3, "c")),
        hooked_calls=("toolu_a", "toolu_b", "c", "d", "e"),
        answered_calls=("toolu_a", "toolu_b", "c"),
        timed_out_calls=("d", "e"),
        unanswered_calls=("f",),
        interrupted_calls=("g",),
        rejected_calls=("h",),
        stop_calls=(),
        final_state_recorded=False,
    )
    chain = ImageChain(start_layers=(BASE,), tops={1: BASE})

    report = chain_report(TRIAL_ID, steps, chain)

    assert report.complete is False
    assert (report.hooked_calls, report.answered_calls, report.checkpoints) == (5, 3, 3)
    assert report.missing_images == (2, 3)
    assert report.final_state_recorded is False
    assert report.reasons == (
        "2 hooked calls timed out",
        "1 hooked call without a call record",
        "1 hooked call stopped while running (no result), so its changes may be in no checkpoint",
        "checkpoint 2 has no image",
        "checkpoint 3 has no image",
    )
    assert chain_report(TRIAL_ID, steps, None).missing_images == ()


def test_timestamps_read_back_from_duckdb_in_utc(trial_copy: Path, tmp_path: Path) -> None:
    # DuckDB returns TIMESTAMPTZ values to Python only with pytz installed, in the session's
    # zone; `schema.connect` sets UTC, so an agent's query prints the same times on any host.
    built = build_trial(
        capture(trial_copy), tmp_path / "store", images=all_images(), work_dir=tmp_path / "w"
    )
    with schema.connect(schema.record_path(tmp_path / "store"), read_only=True) as con:
        read = con.execute("SELECT t_start, t_end FROM steps ORDER BY step_idx").fetchall()
        zone = con.execute("SELECT current_setting('TimeZone')").fetchone()
    assert zone == ("UTC",)
    assert read == [(row.t_start, row.t_end) for row in built.steps]
    assert all(t is None or t.utcoffset().total_seconds() == 0 for pair in read for t in pair)


def test_start_image_prefers_the_resumed_checkpoint(fixture_trial: Path, tmp_path: Path) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    assert build.start_image(trial) is None
    preinstall = {
        "task_image": "task:1",
        "task_image_id": "sha256:" + "1" * 64,
        "task_content_key": "k",
        "image": "trajlab-preinstalled:abc",
        "image_id": "sha256:" + "2" * 64,
        "agent_name": "claude-code",
        "agent_version": "2.1.278",
        "agent_sha256": "3" * 64,
        "harbor_version": "0.23.0",
        "recipe_version": 2,
        "cache_hit": True,
        "recorded_at": "2026-10-02T00:00:00Z",
    }
    (trial / "trajlab-preinstall.json").write_text(json.dumps(preinstall))
    assert build.start_image(trial) == "trajlab-preinstalled:abc"
    resume = {
        "checkpoint_image": "trajlab-checkpoint:task__x.0003",
        "checkpoint_image_id": "sha256:" + "4" * 64,
        "derived_image": "trajlab-preinstalled:abc",
        "recorded_at": "2026-10-02T00:00:00Z",
    }
    (trial / "trajlab-resume.json").write_text(json.dumps(resume))
    # A resumed trial's checkpoints extend the checkpoint it started from, not the derived image.
    assert build.start_image(trial) == "trajlab-checkpoint:task__x.0003"


# -- review findings, 2026-10-18 ----------------------------------------------------------------


@pytest.mark.parametrize("failing", ["chains", "trials", "steps", "deltas"])
def test_a_build_that_fails_while_writing_leaves_the_trial_buildable(
    trial_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    capture(trial_copy)
    store = tmp_path / "store"
    real = build.insert_rows

    def insert(con: Any, table: str, rows: Sequence[Any]) -> None:
        if table == failing:
            raise RuntimeError(f"disk full writing {table}")
        real(con, table, rows)

    monkeypatch.setattr(build, "insert_rows", insert)
    with pytest.raises(RuntimeError, match="disk full"):
        build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    # The record transaction is the commit point: nothing of the trial is in record.duckdb.
    assert [count(store, table) for table in ("trials", "steps", "deltas")] == [0, 0, 0]

    monkeypatch.setattr(build, "insert_rows", real)
    result = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    assert result.skipped_existing is False and result.chain.complete
    assert [count(store, table) for table in ("trials", "steps", "deltas", "chains")] == [
        1,
        3,
        1,
        1,
    ]


def test_a_trial_built_without_images_is_rebuilt_with_them(
    trial_copy: Path, tmp_path: Path
) -> None:
    capture(trial_copy)
    store = tmp_path / "store"
    first = build_trial(trial_copy, store, images=None, work_dir=tmp_path / "w")
    assert first.chain.images_checked is False and first.deltas == ()
    # Building again without images is still skipped.
    assert build_trial(trial_copy, store, images=None, work_dir=tmp_path / "w").skipped_existing

    second = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    assert second.skipped_existing is False
    assert second.chain.complete and len(second.deltas) == 1
    assert [count(store, table) for table in ("trials", "steps", "deltas", "chains")] == [
        1,
        3,
        1,
        1,
    ]
    # And now it is reconciled, so a third build is skipped.
    assert build_trial(
        trial_copy, store, images=all_images(), work_dir=tmp_path / "w"
    ).skipped_existing


def test_a_rebuild_that_cannot_read_the_images_keeps_the_steps(
    trial_copy: Path, tmp_path: Path
) -> None:
    capture(trial_copy)
    store = tmp_path / "store"
    build_trial(trial_copy, store, images=None, work_dir=tmp_path / "w")

    class Unreadable:
        def chain(self, checkpoints: Sequence[CheckpointRecord]) -> ImageChain:
            raise DockerImagesError("Cannot connect to the Docker daemon")

    with pytest.raises(DockerImagesError):
        build_trial(trial_copy, store, images=Unreadable(), work_dir=tmp_path / "w")
    assert [count(store, table) for table in ("trials", "steps", "deltas", "chains")] == [
        1,
        3,
        0,
        1,
    ]
    rebuilt = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    assert rebuilt.skipped_existing is False and rebuilt.chain.complete


# -- background-job notices (ADR-0020) ----------------------------------------------------------


def test_job_notices_are_built_from_the_session_log_and_replaced_on_a_rebuild(
    trial_copy: Path, tmp_path: Path
) -> None:
    from tests.test_atif_jobs import append, event, notice_text
    from trajlab.contracts import JobNoticeRow

    capture(trial_copy)
    append(trial_copy, event("enqueue", notice_text("b4cnj2dq1"), "2026-09-22T00:50:17Z"))
    expected = JobNoticeRow(
        trial_id=TRIAL_ID,
        task_id="b4cnj2dq1",
        tool_call_id=FIXTURE_TOOL_CALL_ID,
        status="completed",
        noticed_at=datetime(2026, 9, 22, 0, 50, 17, tzinfo=UTC),
    )
    store = tmp_path / "store"

    without = build_trial(trial_copy, store, images=None, work_dir=tmp_path / "w")
    assert without.job_notices == (expected,)
    assert count(store, "job_notices") == 1

    rebuilt = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    assert rebuilt.skipped_existing is False
    assert rebuilt.job_notices == (expected,)
    assert count(store, "job_notices") == 1

    skipped = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    assert skipped.skipped_existing is True
    assert skipped.job_notices == (expected,)
    with schema.connect(schema.record_path(store), read_only=True) as con:
        assert con.execute("SELECT backgrounded, job_end FROM step_jobs").fetchall() == [
            (False, None)  # the Write call's output is no task id, notice or not
        ]


# -- recorded writes (ADR-0021) -----------------------------------------------------------------


def test_recorded_writes_are_joined_to_the_deltas_and_replaced_on_a_rebuild(
    trial_copy: Path, tmp_path: Path
) -> None:
    from trajlab.contracts import WriteEvent, WriteRow
    from trajlab.store.writes import writes_path

    capture(trial_copy)
    plain = build_trial(trial_copy, tmp_path / "plain", images=all_images(), work_dir=tmp_path)
    assert plain.writes == ()  # no recorder, no rows
    write_step = plain.steps[0]
    assert write_step.tool == "Write" and write_step.t_start is not None
    when = write_step.t_start + (write_step.t_end - write_step.t_start) / 2  # type: ignore[operator]
    own = WriteEvent(at=when, op="close_write", path="/app/hello.txt", pid=1100, agent=True)
    path = writes_path(trial_copy)
    path.parent.mkdir()
    path.write_text(own.model_dump_json() + "\n")
    expected = WriteRow(
        trial_id=TRIAL_ID,
        step_idx=1,
        path="/app/hello.txt",
        writer_step=1,
        mapped_by="tool_window",
        events=1,
        ops=("close_write",),
        first_at=when,
        last_at=when,
    )
    store = tmp_path / "store"

    without = build_trial(trial_copy, store, images=None, work_dir=tmp_path / "w")
    assert without.writes == ()  # no deltas to join

    rebuilt = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    assert rebuilt.writes == (expected,)
    assert count(store, "writes") == 1

    skipped = build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "w")
    assert skipped.skipped_existing is True
    assert skipped.writes == (expected,)
    with schema.connect(schema.record_path(store), read_only=True) as con:
        assert con.execute(
            "SELECT attribution, causal_step, writer_steps, rule_attribution FROM delta_attribution"
        ).fetchall() == [("recorded", 1, [1], "untrusted_mtime")]  # the layer's made-up time
