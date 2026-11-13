"""The store's DDL against its row models, the store files' guards, and the row contracts."""

import types
import typing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb
import pytest
from pydantic import AwareDatetime, BaseModel, ValidationError
from pydantic.fields import FieldInfo

from trajlab.checkpoint import changes
from trajlab.checkpoint.hook import HOOK_MATCHER
from trajlab.contracts import (
    HOOKED_TOOLS,
    SCHEMA_VERSION,
    BlameRow,
    ChainReport,
    CheckRow,
    DeltaRow,
    FaultRow,
    JobNoticeRow,
    StepRow,
    StoreMeta,
    TrialRow,
    WriteRow,
)
from trajlab.store import exclusions, schema
from trajlab.store.derive import VIEWS, derived_id

TABLE_MODELS: dict[str, type[BaseModel]] = {
    "trials": TrialRow,
    "steps": StepRow,
    "deltas": DeltaRow,
    "job_notices": JobNoticeRow,
    "writes": WriteRow,
    "checks": CheckRow,
    "faults": FaultRow,
    "blame": BlameRow,
    "chains": ChainReport,
}
META = StoreMeta(
    schema_version=SCHEMA_VERSION,
    exclusions=(r"/logs", r"/tmp/claude-\d+"),
    text_rule="utf-8 without NUL",
    reconcile_method="test",
    derived=derived_id(),
)
TIMESTAMPTZ = "TIMESTAMP WITH TIME ZONE"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

TRIAL_ID = UUID("e1eee115-a864-4c21-896b-5b60c8741d97")
PARENT_ID = UUID("0b5e1d0a-0000-4000-8000-00000000c0de")
SHA_A = "a" * 64
SHA_B = "b" * 64
# Offsets other than UTC, so a round trip that dropped or misread the offset would show.
NEW_YORK = timezone(timedelta(hours=-4))
KOLKATA = timezone(timedelta(hours=5, minutes=30))

# Every field set to a value other than its default, for each model.
FULL_ROWS: dict[str, BaseModel] = {
    "trials": TrialRow(
        trial_id=TRIAL_ID,
        trial_name="hello-world__K3GBok3",
        task="hello-world/hello-world",
        attempt_idx=2,
        parent_trial_id=PARENT_ID,
        prompt='Create a file called hello.txt with "Hello, world!" as the content.\n',
        model="anthropic/claude-sonnet-5",
        harness="claude-code",
        harness_version="2.1.278",
        reward=0.5,
        tokens_in=12_345_678_901,  # over 2**31: needs BIGINT
        tokens_out=115,
        cost_usd=0.0320233,
        wall_ms=4090,
    ),
    "steps": StepRow(
        trial_id=TRIAL_ID,
        step_idx=3,
        atif_step_id=2,
        source="agent",
        tool_call_id="toolu_01TKn2WsYzAzVaTXotNFqp2Z",
        tool="Bash",
        args_json='{"command": "cat > /app/hello.txt <<\'EOF\'\\nHello, world!\\nEOF"}',
        output="(no output)",
        is_error=False,
        message="I'll create the file now.",
        reasoning="The file does not exist yet.",
        t_start=datetime(2026, 9, 21, 20, 49, 17, 70001, tzinfo=NEW_YORK),
        t_end=datetime(2026, 9, 22, 6, 19, 18, 300000, tzinfo=KOLKATA),
        checkpoint_seq=1,
        checkpoint_id=f"sha256:{1:064x}",
        checkpoint_at=datetime(2026, 9, 22, 6, 19, 21, 900000, tzinfo=KOLKATA),
        command_shell="cat > /app/hello.txt <<heredoc>",
    ),
    "deltas": DeltaRow(
        trial_id=TRIAL_ID,
        step_idx=3,
        path="/app/hello.txt",
        op="modify",
        kind_before="file",
        kind_after="file",
        is_text=True,
        hunk="--- a/app/hello.txt\n+++ b/app/hello.txt\n@@ -1 +1 @@\n-Hello\n+Hello, world!\n",
        sha256_before=SHA_A,
        sha256_after=SHA_B,
        bytes_before=6,
        bytes_after=14,
        mode_before="0644",
        mode_after="0755",
        owner_before="0:0",
        owner_after="1000:1000",
        mtime_before=datetime(2026, 9, 21, 20, 49, 0, tzinfo=NEW_YORK),
        mtime_after=datetime(2026, 9, 22, 6, 19, 18, tzinfo=KOLKATA),
    ),
    "job_notices": JobNoticeRow(
        trial_id=TRIAL_ID,
        task_id="b4cnj2dq1",
        tool_call_id="toolu_01TKn2WsYzAzVaTXotNFqp2Z",
        status="completed",
        noticed_at=datetime(2026, 9, 22, 6, 24, 18, 84000, tzinfo=KOLKATA),
    ),
    "writes": WriteRow(
        trial_id=TRIAL_ID,
        step_idx=3,
        path="/app/hello.txt",
        writer_step=1,
        mapped_by="session",
        events=7,
        ops=("close_write", "create", "modify"),
        first_at=datetime(2026, 9, 22, 6, 19, 1, 250000, tzinfo=KOLKATA),
        last_at=datetime(2026, 9, 21, 20, 49, 17, 999999, tzinfo=NEW_YORK),
    ),
    "checks": CheckRow(
        trial_id=TRIAL_ID, check_id="pytest:test_hello_file", passed=False, fault_id="f1"
    ),
    "faults": FaultRow(
        trial_id=TRIAL_ID,
        fault_id="f1",
        kind="wrong_edit",
        method="counterfactual",
        flags=("multi_call", "late"),
    ),
    "blame": BlameRow(
        trial_id=TRIAL_ID,
        fault_id="f1",
        role="alternative",
        fix_id="fix2",
        tool_call_id="toolu_01TKn2WsYzAzVaTXotNFqp2Z",
        step_idx=3,
    ),
    "chains": ChainReport(
        trial_id=TRIAL_ID,
        complete=False,
        hooked_calls=4,
        answered_calls=2,
        timed_out_calls=("toolu_a",),
        unanswered_calls=("toolu_b",),
        checkpoints=3,
        images_checked=True,
        missing_images=(2, 3),
        final_state_recorded=True,
        reasons=("1 hooked call timed out", "checkpoint 2 has no image"),
    ),
}

# Every nullable field null and every defaulted field at its default.
SPARSE_ROWS: dict[str, BaseModel] = {
    "trials": TrialRow(
        trial_id=PARENT_ID,
        trial_name="x__y",
        task="x",
        attempt_idx=0,
        prompt="",
        model="m",
        harness="h",
    ),
    "steps": StepRow(trial_id=TRIAL_ID, step_idx=1, source="system"),
    "deltas": DeltaRow(
        trial_id=TRIAL_ID, step_idx=1, path="/opt/x", op="delete", kind_before="dir"
    ),
    "job_notices": JobNoticeRow(
        trial_id=TRIAL_ID,
        task_id="b1",
        tool_call_id="toolu_c",
        status="failed",
        noticed_at=EPOCH,
    ),
    "writes": WriteRow(
        trial_id=TRIAL_ID,
        step_idx=1,
        path="/opt/x",
        events=1,
        ops=("delete",),
        first_at=EPOCH,
        last_at=EPOCH,
    ),
    "checks": CheckRow(trial_id=TRIAL_ID, check_id="pytest:x", passed=None),
    "faults": FaultRow(trial_id=TRIAL_ID, fault_id="f2", kind="omission", method="replay"),
    "blame": BlameRow(trial_id=TRIAL_ID, fault_id="f2", role="blamed", tool_call_id="toolu_c"),
    "chains": ChainReport(
        trial_id=PARENT_ID,
        complete=True,
        hooked_calls=0,
        answered_calls=0,
        checkpoints=0,
        images_checked=True,
        final_state_recorded=False,
    ),
}


def db_path(store: Path, table: str) -> Path:
    if table in schema.RECORD_TABLES:
        return schema.record_path(store)
    return schema.heldout_path(store)


def read_back[M: BaseModel](con: duckdb.DuckDBPyConnection, table: str, model: type[M]) -> list[M]:
    """Rows in insertion order."""
    names = list(model.model_fields)
    rows = con.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY rowid").fetchall()
    return [model.model_validate(dict(zip(names, row, strict=True))) for row in rows]


def _optional_args(annotation: Any) -> tuple[list[Any], bool]:
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        args = typing.get_args(annotation)
        return [a for a in args if a is not type(None)], type(None) in args
    return [annotation], False


def nullable(field: FieldInfo) -> bool:
    return _optional_args(field.annotation)[1]


SQL_TYPES: dict[Any, set[str]] = {
    UUID: {"UUID"},
    str: {"VARCHAR"},
    bool: {"BOOLEAN"},
    int: {"INTEGER", "BIGINT"},
    float: {"DOUBLE"},
    AwareDatetime: {TIMESTAMPTZ},
}


def sql_types(annotation: Any) -> set[str]:
    """The DuckDB column types that hold a field of this annotation without a cast."""
    (base,), _ = _optional_args(annotation)
    if typing.get_origin(base) is typing.Literal:
        (kind,) = {type(value) for value in typing.get_args(base)}
        return SQL_TYPES[kind]
    if typing.get_origin(base) is tuple:
        item, _ellipsis = typing.get_args(base)
        return {f"{sql}[]" for sql in sql_types(item)}
    return SQL_TYPES[base]


# -- DDL against models ------------------------------------------------------------------------


def test_every_table_has_a_row_model() -> None:
    assert set(schema.RECORD_TABLES) == {"trials", "steps", "deltas", "job_notices", "writes"}
    assert set(schema.HELDOUT_TABLES) == {"checks", "faults", "blame", "chains"}
    assert {*schema.RECORD_TABLES, *schema.HELDOUT_TABLES} == set(TABLE_MODELS)


@pytest.mark.parametrize("table", list(TABLE_MODELS))
def test_columns_are_the_model_fields_in_order(tmp_path: Path, table: str) -> None:
    schema.create_store(tmp_path, META)
    with schema.connect(db_path(tmp_path, table), read_only=True) as con:
        columns = con.execute(
            'SELECT name, type, "notnull" FROM pragma_table_info(?)', [table]
        ).fetchall()
    fields = TABLE_MODELS[table].model_fields

    assert [name for name, _, _ in columns] == list(fields)
    for name, sql, notnull in columns:
        assert notnull is not nullable(fields[name]), f"{table}.{name}: NOT NULL is {notnull}"
        assert sql in sql_types(fields[name].annotation), f"{table}.{name}: {sql}"


def test_each_file_holds_only_its_tables(tmp_path: Path) -> None:
    schema.create_store(tmp_path, META)
    found = {}
    for path in (schema.record_path(tmp_path), schema.heldout_path(tmp_path)):
        with schema.connect(path, read_only=True) as con:
            found[path.name] = {row[0] for row in con.execute("SHOW TABLES").fetchall()}

    assert found == {
        "record.duckdb": {
            "trials",
            "steps",
            "deltas",
            "job_notices",
            "writes",
            "store_meta",
            *(v.name for v in VIEWS),
        },
        "heldout.duckdb": {"checks", "faults", "blame", "chains", "store_meta"},
    }
    assert schema.blobs_dir(tmp_path).is_dir()


# -- store guards ------------------------------------------------------------------------------


def test_create_store_is_idempotent(tmp_path: Path) -> None:
    schema.create_store(tmp_path, META)
    with schema.connect(schema.record_path(tmp_path), read_only=False) as con:
        schema.insert_rows(con, "trials", [FULL_ROWS["trials"]])

    schema.create_store(tmp_path, META)

    for path in (schema.record_path(tmp_path), schema.heldout_path(tmp_path)):
        with schema.connect(path, read_only=True) as con:
            assert schema.read_meta(con) == META
            assert con.execute("SELECT count(*) FROM store_meta").fetchone() == (
                len(StoreMeta.model_fields),
            )
    with schema.connect(schema.record_path(tmp_path), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM trials").fetchone() == (1,)


@pytest.mark.parametrize(
    "change",
    [
        {"exclusions": (r"/logs",)},
        {"text_rule": "ascii only"},
        {"reconcile_method": "export-and-diff"},
    ],
    ids=["exclusions", "text_rule", "reconcile_method"],
)
def test_create_store_refuses_a_store_built_under_other_settings(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    schema.create_store(tmp_path, META)

    with pytest.raises(schema.StoreError, match="was built under"):
        schema.create_store(tmp_path, META.model_copy(update=change))


def test_create_store_refuses_another_schema_version(tmp_path: Path) -> None:
    other = META.model_copy(update={"schema_version": SCHEMA_VERSION + 1})

    with pytest.raises(schema.StoreError, match=f"writes schema {SCHEMA_VERSION}"):
        schema.create_store(tmp_path, other)
    assert not schema.record_path(tmp_path).exists()


def test_connect_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(schema.StoreError, match="does not exist"):
        schema.connect(schema.record_path(tmp_path), read_only=True)


def test_connect_refuses_another_schema_version(tmp_path: Path) -> None:
    schema.create_store(tmp_path, META)
    with duckdb.connect(str(schema.heldout_path(tmp_path))) as con:
        con.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_version'", ["99"])

    with pytest.raises(schema.StoreError, match="has schema 99"):
        schema.connect(schema.heldout_path(tmp_path), read_only=True)
    # The other file is untouched and still opens.
    schema.connect(schema.record_path(tmp_path), read_only=True).close()


# -- insert_rows -------------------------------------------------------------------------------


@pytest.mark.parametrize("table", list(TABLE_MODELS))
def test_insert_rows_round_trips(tmp_path: Path, table: str) -> None:
    rows = [FULL_ROWS[table], SPARSE_ROWS[table]]
    schema.create_store(tmp_path, META)
    with schema.connect(db_path(tmp_path, table), read_only=False) as con:
        schema.insert_rows(con, table, rows)
        assert read_back(con, table, TABLE_MODELS[table]) == rows


def test_insert_rows_stores_uuids_instants_and_lists(tmp_path: Path) -> None:
    schema.create_store(tmp_path, META)
    step = FULL_ROWS["steps"]
    with schema.connect(schema.record_path(tmp_path), read_only=False) as con:
        schema.insert_rows(con, "steps", [step])
        trial_id, t_start, t_end = con.execute(
            "SELECT trial_id, epoch_us(t_start), epoch_us(t_end) FROM steps"
        ).fetchone()  # type: ignore[misc]
    assert isinstance(trial_id, UUID) and trial_id == TRIAL_ID
    # 20:49:17.070001-04:00 and 06:19:18.3+05:30 are 00:49:17.070001Z and 00:49:18.3Z.
    assert EPOCH + timedelta(microseconds=t_start) == datetime(
        2026, 9, 22, 0, 49, 17, 70001, tzinfo=UTC
    )
    assert EPOCH + timedelta(microseconds=t_end) == datetime(
        2026, 9, 22, 0, 49, 18, 300000, tzinfo=UTC
    )
    with schema.connect(schema.heldout_path(tmp_path), read_only=False) as con:
        schema.insert_rows(con, "chains", [FULL_ROWS["chains"]])
        assert con.execute("SELECT missing_images, reasons FROM chains").fetchone() == (
            [2, 3],
            ["1 hooked call timed out", "checkpoint 2 has no image"],
        )


def test_insert_rows_of_nothing_is_a_no_op(tmp_path: Path) -> None:
    schema.create_store(tmp_path, META)
    with schema.connect(schema.record_path(tmp_path), read_only=False) as con:
        schema.insert_rows(con, "deltas", [])
        assert con.execute("SELECT count(*) FROM deltas").fetchone() == (0,)


# -- exclusions and hooked tools ---------------------------------------------------------------

PROBE_PATHS = (
    "/logs",
    "/logs/agent/x",
    "/logs/verifier/reward.txt",
    "/logsx",
    "/tmp/claude-0",
    "/tmp/claude-0/-app/tasks/b1.output",
    "/tmp/claude-1000/x",
    "/tmp/claude-x",
    "/tmp/claude-code-settings",
    "/tmp/claude-code-settings/settings.json",
    "/tmp/foo",
    "/root/.claude",
    "/root/.claude/projects/-app/x.jsonl",
    "/root/.claude.json",
    "/root/.claude.json.backup",
    "/root/.claudex",
    "/home/agent/.claude.json",
    "/home/agent/.cache/claude/staging/x",
    "/home/agent/.cache/pip/x",
    "/root/.local/state/claude/locks/2.1.278.lock",
    "/root/.local/state/other",
    "/app/.claude/settings.json",
    "/app/hello.txt",
    "/proc/1/status",
    "/sys/fs/cgroup",
)


def detector_patterns() -> list[str]:
    """The path patterns of the change detector's regex, `^<p>(?:/|$)|...|^<p>$`."""
    patterns = []
    for alternative in changes.EXCLUDED.pattern.removeprefix("^").split("|^"):
        if alternative.endswith("(?:/|$)"):
            patterns.append(alternative.removesuffix("(?:/|$)"))
        else:
            assert alternative.endswith("$"), alternative
            patterns.append(alternative.removesuffix("$"))
    return patterns


def test_store_exclusions_include_every_detector_pattern() -> None:
    patterns = detector_patterns()
    assert len(patterns) == 7
    for pattern in patterns:
        assert pattern in exclusions.EXCLUDED_PATTERNS


def test_store_excludes_every_path_the_detector_excludes() -> None:
    excluded_by_detector = [path for path in PROBE_PATHS if changes.excluded(path)]
    assert len(excluded_by_detector) >= 10  # the probe exercises the detector's list
    for path in excluded_by_detector:
        assert exclusions.excluded(path), path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/tmp/foo", False),
        ("/tmp/claude-x", False),
        ("/logs/agent/x", True),
        ("/logsx", False),
        ("/proc/1/status", True),
        ("/root/.claude.json", True),
        ("/root/.claude.json.backup", False),
        ("/app/.claude/settings.json", False),
        ("/app/hello.txt", False),
    ],
)
def test_excluded(path: str, expected: bool) -> None:
    assert exclusions.excluded(path) is expected


def test_hooked_tools_are_the_hook_matcher() -> None:
    assert set(HOOK_MATCHER.split("|")) == HOOKED_TOOLS


# -- row contracts -----------------------------------------------------------------------------


def _with(table: str, **changes_: Any) -> dict[str, Any]:
    return FULL_ROWS[table].model_dump() | changes_


@pytest.mark.parametrize(
    ("attempt_idx", "parent", "valid"),
    [(0, None, True), (0, PARENT_ID, False), (1, None, False), (3, PARENT_ID, True)],
)
def test_trial_row_parent_iff_retry(attempt_idx: int, parent: UUID | None, valid: bool) -> None:
    fields = _with("trials", attempt_idx=attempt_idx, parent_trial_id=parent)
    if valid:
        TrialRow(**fields)
    else:
        with pytest.raises(ValidationError, match="attempt 0 has no parent_trial_id"):
            TrialRow(**fields)


@pytest.mark.parametrize(
    "change",
    [
        {"checkpoint_seq": None},
        {"checkpoint_id": None},
    ],
    ids=["id-without-seq", "seq-without-id"],
)
def test_step_row_checkpoint_seq_and_id_go_together(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="set together"):
        StepRow(**_with("steps", **change))
    StepRow(**_with("steps", checkpoint_seq=None, checkpoint_id=None, checkpoint_at=None))


def test_step_row_checkpoint_at_belongs_to_a_checkpoint() -> None:
    with pytest.raises(ValidationError, match="checkpoint_at belongs to a checkpoint_seq"):
        StepRow(**_with("steps", checkpoint_seq=None, checkpoint_id=None))


@pytest.mark.parametrize("field", ["args_json", "output"])
def test_step_row_without_a_tool_has_no_call_fields(field: str) -> None:
    fields = _with("steps", tool=None, args_json=None, output=None, command_shell=None)
    StepRow(**fields)
    with pytest.raises(ValidationError, match="without a tool"):
        StepRow(**fields | {field: "x"})


def test_step_row_command_shell_is_a_bash_calls() -> None:
    StepRow(**_with("steps", tool="Bash"))
    with pytest.raises(ValidationError, match="only a Bash call"):
        StepRow(**_with("steps", tool="Write"))


@pytest.mark.parametrize(
    ("op", "kind_before", "kind_after", "valid"),
    [
        ("add", None, "file", True),
        ("add", "file", "file", False),
        ("delete", "dir", None, True),
        ("delete", "dir", "dir", False),
        ("modify", "file", "symlink", True),
        ("modify", None, "file", False),
        ("modify", "file", None, False),
    ],
)
def test_delta_row_op_matches_kinds(
    op: str, kind_before: str | None, kind_after: str | None, valid: bool
) -> None:
    fields = _with("deltas", op=op, kind_before=kind_before, kind_after=kind_after, hunk=None)
    if kind_before is None:
        fields["mtime_before"] = None
    if kind_after is None:
        fields["mtime_after"] = None
    if valid:
        DeltaRow(**fields)
    else:
        with pytest.raises(ValidationError, match=f"op {op}"):
            DeltaRow(**fields)


@pytest.mark.parametrize("side", ["before", "after"])
def test_delta_row_mtime_belongs_to_a_version_that_exists(side: str) -> None:
    op, kind = ("add", "kind_before") if side == "before" else ("delete", "kind_after")
    fields = _with("deltas", op=op, hunk=None, **{kind: None, f"mtime_{side}": None})
    DeltaRow(**fields)
    with pytest.raises(ValidationError, match="an mtime belongs to a version that exists"):
        DeltaRow(**fields | {f"mtime_{side}": EPOCH})


@pytest.mark.parametrize("is_text", [False, None])
def test_delta_row_hunk_only_for_text(is_text: bool | None) -> None:
    with pytest.raises(ValidationError, match="only text has a hunk"):
        DeltaRow(**_with("deltas", is_text=is_text))


def test_delta_row_blob_ref_only_for_a_non_text_later_version() -> None:
    blob = {"hunk": None, "blob_ref": f"blobs/{SHA_B}"}
    DeltaRow(**_with("deltas", is_text=False, **blob))
    with pytest.raises(ValidationError, match="blob_ref"):
        DeltaRow(**_with("deltas", is_text=True, **blob))
    with pytest.raises(ValidationError, match="blob_ref"):
        DeltaRow(**_with("deltas", is_text=None, **blob))
    with pytest.raises(ValidationError, match="blob_ref"):
        DeltaRow(
            **_with(
                "deltas",
                op="delete",
                kind_after=None,
                is_text=False,
                sha256_after=None,
                mtime_after=None,
                **blob,
            )
        )


@pytest.mark.parametrize(
    "change",
    [{"path": "app/hello.txt"}, {"sha256_after": "B" * 64}, {"mode_after": "644"}],
    ids=["relative-path", "sha256", "mode"],
)
def test_delta_row_field_formats(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DeltaRow(**_with("deltas", **change))


def test_chain_report_is_complete_exactly_without_reasons() -> None:
    with pytest.raises(ValidationError, match="complete exactly when"):
        ChainReport(**_with("chains", complete=True))
    with pytest.raises(ValidationError, match="complete exactly when"):
        ChainReport(**SPARSE_ROWS["chains"].model_dump() | {"complete": False})


@pytest.mark.parametrize("row", [*FULL_ROWS.values(), *SPARSE_ROWS.values(), META])
def test_json_round_trip(row: BaseModel) -> None:
    assert type(row).model_validate_json(row.model_dump_json()) == row


# -- review findings, 2026-10-18 ----------------------------------------------------------------

# Integer widths that matter: sizes past 2 GiB and token counts need 64 bits.
BIGINT_COLUMNS = {
    ("trials", "tokens_in"),
    ("trials", "tokens_out"),
    ("trials", "wall_ms"),
    ("deltas", "bytes_before"),
    ("deltas", "bytes_after"),
}


def test_sizes_and_counts_are_64_bit(tmp_path: Path) -> None:
    schema.create_store(tmp_path, META)
    with schema.connect(schema.record_path(tmp_path), read_only=True) as con:
        found = {
            (table, name)
            for table, name, kind in con.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "JOIN information_schema.tables USING (table_name) "
                "WHERE table_type = 'BASE TABLE'"
            ).fetchall()
            if kind == "BIGINT"
        }
    assert found == BIGINT_COLUMNS


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("trials", {}),
        ("steps", {"tool": "Bash", "args_json": "{}"}),
        ("deltas", {"op": "modify", "kind_after": "dir", "kind_before": "dir"}),
        ("checks", {"passed": True}),
        ("faults", {"kind": "wrong_edit"}),
        ("chains", {"hooked_calls": 99}),
    ],
)
def test_primary_keys_refuse_a_second_row(tmp_path: Path, table: str, key: dict[str, Any]) -> None:
    schema.create_store(tmp_path, META)
    row = FULL_ROWS[table]
    twin = type(row).model_validate(row.model_dump() | key)  # same key, other values
    with schema.connect(db_path(tmp_path, table), read_only=False) as con:
        schema.insert_rows(con, table, [row])
        with pytest.raises(duckdb.ConstraintException):
            schema.insert_rows(con, table, [twin])


def test_a_store_file_without_its_meta_is_refused_clearly(tmp_path: Path) -> None:
    # What a build killed while creating the file used to leave behind.
    duckdb.connect(str(schema.record_path(tmp_path))).close()
    with pytest.raises(schema.StoreError, match=r"delete it \(and record.duckdb.wal"):
        schema.create_store(tmp_path, META)


def test_creating_a_store_leaves_no_temporary_files(tmp_path: Path) -> None:
    schema.create_store(tmp_path / "store", META)
    assert sorted(p.name for p in (tmp_path / "store").iterdir()) == [
        "blobs",
        "heldout.duckdb",
        "record.duckdb",
    ]


def test_the_agent_connection_cannot_reach_held_out_data(tmp_path: Path) -> None:
    schema.create_store(tmp_path, META)
    with schema.connect(schema.heldout_path(tmp_path), read_only=False) as con:
        schema.insert_rows(
            con, "checks", [CheckRow(trial_id=TRIAL_ID, check_id="pytest:secret", passed=False)]
        )
    heldout, blobs = schema.heldout_path(tmp_path), schema.blobs_dir(tmp_path)
    (blobs / ("0" * 64)).write_bytes(b"blob")
    with schema.connect_agent(tmp_path) as con:
        assert con.execute("SELECT count(*) FROM trials").fetchone() == (0,)
        assert con.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)
        for query in [
            f"SELECT count(*) FROM read_blob('{heldout}')",
            f"SELECT * FROM glob('{tmp_path}/*')",
            f"SELECT * FROM read_text('{blobs}/{'0' * 64}')",
            f"ATTACH '{heldout}' AS h",
            f"COPY trials TO '{tmp_path}/out.csv'",
            f"SELECT * FROM read_csv('{tmp_path}/out.csv')",
            "INSTALL httpfs",
        ]:
            with pytest.raises(duckdb.PermissionException):
                con.execute(query)
        for setting in ["enable_external_access = true", "lock_configuration = false"]:
            with pytest.raises(duckdb.InvalidInputException):
                con.execute(f"SET {setting}")
        with pytest.raises(duckdb.Error):
            con.execute("DELETE FROM trials")  # read-only


def test_bulk_insert_is_fast_and_exact(tmp_path: Path) -> None:
    import time

    schema.create_store(tmp_path, META)
    base = FULL_ROWS["deltas"]
    rows = [
        DeltaRow.model_validate(
            base.model_dump() | {"path": f"/opt/pkg/{i}.py", "step_idx": 1 + i % 7}
        )
        for i in range(20_000)
    ]
    rows.append(DeltaRow.model_validate(base.model_dump() | {"path": "/x", "hunk": "+caf\udce9\n"}))
    with schema.connect(schema.record_path(tmp_path), read_only=False) as con:
        start = time.monotonic()
        schema.insert_rows(con, "deltas", rows)
        assert time.monotonic() - start < 5  # executemany took about 30 s for this many
        assert con.execute("SELECT count(*) FROM deltas").fetchone() == (len(rows),)
        # A lone surrogate, which JSON and DuckDB refuse, is stored written out.
        assert con.execute("SELECT hunk FROM deltas WHERE path = '/x'").fetchone() == (
            "+caf\\udce9\n",
        )
        first = read_back(con, "deltas", DeltaRow)[0]
    assert first == rows[0]


def test_two_agent_connections_can_be_open_at_once(tmp_path: Path) -> None:
    # They share one DuckDB instance, which the first locks; the second must not need a SET.
    schema.create_store(tmp_path, META)
    with schema.connect_agent(tmp_path) as first, schema.connect_agent(tmp_path) as second:
        for con in (first, second):
            assert con.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)
            assert con.execute("SELECT count(*) FROM trials").fetchone() == (0,)


def test_a_wal_left_from_a_deleted_store_file_is_refused(tmp_path: Path) -> None:
    # DuckDB would replay it into the new file, bringing old rows back.
    schema.create_store(tmp_path, META)
    schema.record_path(tmp_path).unlink()
    (tmp_path / "record.duckdb.wal").write_bytes(b"old")
    with pytest.raises(schema.StoreError, match=r"record.duckdb.wal is left .* delete it too"):
        schema.create_store(tmp_path, META)
