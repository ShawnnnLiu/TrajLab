"""The Exp 2 store's DDL, in one place, versioned by `SCHEMA_VERSION` (ADR-0014).

A store is a directory:

    <store>/record.duckdb    trials, steps, deltas, job_notices, writes, store_meta, and the
                             derived views
                             (trajlab.store.derive): what an agent may read
    <store>/heldout.duckdb   checks, faults, blame, chains, store_meta: never shown to an agent
    <store>/blobs/<sha256>   later versions of changed rows without a hunk (`blob_ref`)

Held-out tables live in their own file, and an agent's connection (`connect_agent`) opens only
`record.duckdb`, read-only, with DuckDB's access to other files turned off and its configuration
locked: no `read_blob`, `glob`, `read_csv`, `ATTACH`, or `COPY` can reach `heldout.duckdb` or
`blobs/` (ADR-0014, item 2). A default DuckDB connection could read either.

Every table's columns are, in order, the fields of its row model in `trajlab.contracts.store`; a
test keeps them equal. A shape change bumps `SCHEMA_VERSION` and needs an ADR. The derived views
are created with the tables and versioned apart from them (`StoreMeta.derived`): `replace_views`
swaps a store's views for this code's without touching a row (ADR-0018).
"""

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel, ValidationError

from trajlab.contracts import (
    BLOBS_DIRNAME,
    HELDOUT_DB_FILENAME,
    RECORD_DB_FILENAME,
    SCHEMA_VERSION,
    StoreMeta,
)
from trajlab.store.derive import VIEWS, derived_id

RECORD_TABLES: dict[str, str] = {
    "trials": """
        CREATE TABLE trials (
            trial_id UUID PRIMARY KEY,
            trial_name VARCHAR NOT NULL,
            task VARCHAR NOT NULL,
            attempt_idx INTEGER NOT NULL,
            parent_trial_id UUID,
            prompt VARCHAR NOT NULL,
            model VARCHAR NOT NULL,
            harness VARCHAR NOT NULL,
            harness_version VARCHAR,
            reward DOUBLE,
            tokens_in BIGINT,
            tokens_out BIGINT,
            cost_usd DOUBLE,
            wall_ms BIGINT
        )""",
    "steps": """
        CREATE TABLE steps (
            trial_id UUID NOT NULL,
            step_idx INTEGER NOT NULL,
            atif_step_id INTEGER,
            source VARCHAR NOT NULL,
            tool_call_id VARCHAR,
            tool VARCHAR,
            args_json VARCHAR,
            output VARCHAR,
            is_error BOOLEAN,
            message VARCHAR,
            reasoning VARCHAR,
            t_start TIMESTAMPTZ,
            t_end TIMESTAMPTZ,
            checkpoint_seq INTEGER,
            checkpoint_id VARCHAR,
            checkpoint_at TIMESTAMPTZ,
            command_shell VARCHAR,
            PRIMARY KEY (trial_id, step_idx)
        )""",
    "deltas": """
        CREATE TABLE deltas (
            trial_id UUID NOT NULL,
            step_idx INTEGER NOT NULL,
            path VARCHAR NOT NULL,
            op VARCHAR NOT NULL,
            kind_before VARCHAR,
            kind_after VARCHAR,
            is_text BOOLEAN,
            hunk VARCHAR,
            blob_ref VARCHAR,
            sha256_before VARCHAR,
            sha256_after VARCHAR,
            bytes_before BIGINT,
            bytes_after BIGINT,
            mode_before VARCHAR,
            mode_after VARCHAR,
            owner_before VARCHAR,
            owner_after VARCHAR,
            mtime_before TIMESTAMPTZ,
            mtime_after TIMESTAMPTZ,
            PRIMARY KEY (trial_id, step_idx, path)
        )""",
    "job_notices": """
        CREATE TABLE job_notices (
            trial_id UUID NOT NULL,
            task_id VARCHAR NOT NULL,
            tool_call_id VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            noticed_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (trial_id, task_id)
        )""",
    "writes": """
        CREATE TABLE writes (
            trial_id UUID NOT NULL,
            step_idx INTEGER NOT NULL,
            path VARCHAR NOT NULL,
            writer_step INTEGER,
            mapped_by VARCHAR,
            events INTEGER NOT NULL,
            ops VARCHAR[] NOT NULL,
            first_at TIMESTAMPTZ NOT NULL,
            last_at TIMESTAMPTZ NOT NULL,
            via_dir VARCHAR
        )""",
}

HELDOUT_TABLES: dict[str, str] = {
    "checks": """
        CREATE TABLE checks (
            trial_id UUID NOT NULL,
            check_id VARCHAR NOT NULL,
            passed BOOLEAN,
            fault_id VARCHAR,
            PRIMARY KEY (trial_id, check_id)
        )""",
    "faults": """
        CREATE TABLE faults (
            trial_id UUID NOT NULL,
            fault_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            method VARCHAR NOT NULL,
            flags VARCHAR[] NOT NULL,
            PRIMARY KEY (trial_id, fault_id)
        )""",
    "blame": """
        CREATE TABLE blame (
            trial_id UUID NOT NULL,
            fault_id VARCHAR NOT NULL,
            role VARCHAR NOT NULL,
            fix_id VARCHAR,
            tool_call_id VARCHAR NOT NULL,
            step_idx INTEGER
        )""",
    "chains": """
        CREATE TABLE chains (
            trial_id UUID PRIMARY KEY,
            complete BOOLEAN NOT NULL,
            hooked_calls INTEGER NOT NULL,
            answered_calls INTEGER NOT NULL,
            timed_out_calls VARCHAR[] NOT NULL,
            unanswered_calls VARCHAR[] NOT NULL,
            interrupted_calls VARCHAR[] NOT NULL,
            rejected_calls VARCHAR[] NOT NULL,
            checkpoints INTEGER NOT NULL,
            images_checked BOOLEAN NOT NULL,
            missing_images INTEGER[] NOT NULL,
            final_state_recorded BOOLEAN NOT NULL,
            reasons VARCHAR[] NOT NULL
        )""",
}

META_TABLE = """
    CREATE TABLE store_meta (
        key VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    )"""


class StoreError(ValueError):
    """The store directory is not a store this code can use."""


def record_path(store_dir: Path) -> Path:
    return store_dir / RECORD_DB_FILENAME


def heldout_path(store_dir: Path) -> Path:
    return store_dir / HELDOUT_DB_FILENAME


def blobs_dir(store_dir: Path) -> Path:
    return store_dir / BLOBS_DIRNAME


def _create(path: Path, ddls: Sequence[str], meta: StoreMeta) -> None:
    """Create a database file whole or not at all: it is built under a temporary name and
    renamed, so a build killed midway leaves no file without its tables, views, and meta."""
    tmp = path.with_name(f".{path.name}.tmp")
    for leftover in (tmp, tmp.with_name(tmp.name + ".wal")):
        leftover.unlink(missing_ok=True)
    with duckdb.connect(str(tmp)) as con:
        for ddl in ddls:
            con.execute(ddl)
        con.execute(META_TABLE)
        con.executemany(
            "INSERT INTO store_meta VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in meta.model_dump(mode="json").items()],
        )
    os.replace(tmp, path)


class SchemaVersionError(StoreError):
    """A store file of a schema version this code does not read."""

    def __init__(self, version: object, path: Path | None = None) -> None:
        self.version = version
        where = f"{path} has" if path is not None else "has"
        super().__init__(
            f"{where} schema {version}; this code reads {SCHEMA_VERSION}, and no store of "
            "another version is upgraded in place: build it again from the trial dirs and "
            "their images (ADR-0020)"
        )


def _raw_meta(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    try:
        rows = con.execute("SELECT key, value FROM store_meta").fetchall()
        return {key: json.loads(value) for key, value in rows}
    except (duckdb.Error, json.JSONDecodeError) as error:
        raise StoreError(f"not a complete store: no readable store_meta ({error})") from error


def read_meta(con: duckdb.DuckDBPyConnection) -> StoreMeta:
    """The store_meta of an open store file; `SchemaVersionError` if it names another schema
    version, `StoreError` if it has none that reads."""
    values = _raw_meta(con)
    version = values.get("schema_version")
    if version is not None and version != SCHEMA_VERSION:
        raise SchemaVersionError(version)
    try:
        return StoreMeta.model_validate(values)
    except ValidationError as error:
        raise StoreError(f"not a complete store: no readable store_meta ({error})") from error


def create_store(store_dir: Path, meta: StoreMeta) -> None:
    """Create an empty store in `store_dir`, or check that the one there matches `meta`.

    Raises `StoreError` if an existing store was built under a different schema version,
    exclusion list, text rule, or reconcile method: rows from both would not be comparable. One
    whose derived views differ is refused too, with the command that replaces them.
    """
    if meta.schema_version != SCHEMA_VERSION:
        raise StoreError(f"this code writes schema {SCHEMA_VERSION}, not {meta.schema_version}")
    store_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir(store_dir).mkdir(exist_ok=True)
    for path, ddls in (
        (record_path(store_dir), [*RECORD_TABLES.values(), *(view.ddl() for view in VIEWS)]),
        (heldout_path(store_dir), list(HELDOUT_TABLES.values())),
    ):
        wal = path.with_name(path.name + ".wal")
        if not path.exists():
            if wal.exists():
                # DuckDB would replay it into the new file: old rows back, or a failed replay.
                raise StoreError(f"{wal} is left from a deleted {path.name}; delete it too")
            _create(path, ddls, meta)
            continue
        with duckdb.connect(str(path), read_only=True) as con:
            found = _existing_meta(con, path)
        if found != meta:
            if found.model_copy(update={"derived": meta.derived}) == meta:
                raise StoreError(
                    f"{path} has derived views {found.derived}, not {meta.derived}; "
                    f"run `trajlab store derive {store_dir}` to replace them"
                )
            raise StoreError(f"{path} was built under {found}, not {meta}")


def _existing_meta(con: duckdb.DuckDBPyConnection, path: Path) -> StoreMeta:
    try:
        return read_meta(con)
    except SchemaVersionError as error:
        raise SchemaVersionError(error.version, path) from None
    except StoreError as error:
        wal = path.with_name(path.name + ".wal")
        raise StoreError(
            f"{path}: {error}; delete it (and {wal.name}, if there) to build the store again"
        ) from error


def replace_views(store_dir: Path, meta: StoreMeta) -> str:
    """Replace the derived views of the store in `store_dir` with this code's and record their
    id, `meta.derived`, in both files' `store_meta`; return the id they replace. Rows are not
    touched, since every view is a function of them.

    Raises `StoreError` if a file is missing or was built under settings other than `meta`'s
    (apart from the views): only those can be brought up to date. A store of another schema
    version is refused with `SchemaVersionError`: since version 3 the rows themselves carry
    values an older store lacks (ADR-0020), so it is built again, not upgraded. A store left
    between the two files' updates (record done, heldout not) is finished, and the heldout
    file's old id returned.
    """
    record, heldout = record_path(store_dir), heldout_path(store_dir)
    record_old = _replaceable(record, meta)
    heldout_old = _replaceable(heldout, meta)
    with duckdb.connect(str(record)) as con:
        con.begin()
        try:
            for (name,) in con.execute(
                "SELECT view_name FROM duckdb_views() WHERE NOT internal AND NOT temporary"
            ).fetchall():
                con.execute(f'DROP VIEW "{name}"')
            for view in VIEWS:
                con.execute(view.ddl())
            _write_versions(con, meta)
        except BaseException:
            con.rollback()
            raise
        con.commit()
    with duckdb.connect(str(heldout)) as con:
        _write_versions(con, meta)
    return record_old if record_old != meta.derived else heldout_old


def _replaceable(path: Path, meta: StoreMeta) -> str:
    """The derived id of a store file `replace_views` can bring to `meta`."""
    if not path.is_file():
        raise StoreError(f"{path} does not exist")
    with duckdb.connect(str(path), read_only=True) as con:
        found = _existing_meta(con, path)
    if found.model_copy(update={"derived": meta.derived}) != meta:
        raise StoreError(f"{path} was built under {found}, not {meta}")
    return found.derived


def _write_versions(con: duckdb.DuckDBPyConnection, meta: StoreMeta) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO store_meta VALUES (?, ?)",
        [
            ("schema_version", json.dumps(meta.schema_version)),
            ("derived", json.dumps(meta.derived)),
        ],
    )


def connect(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    """Open one of a store's database files; it must exist and carry this SCHEMA_VERSION and
    this code's derived views, so no reader sees rows computed by other rules than the ones it
    describes.

    The session's time zone is UTC, so TIMESTAMPTZ values read back (and print) in UTC whatever
    the host's zone is. DuckDB needs `pytz` to return them to Python.
    """
    if not path.is_file():
        raise StoreError(f"{path} does not exist")
    con = duckdb.connect(str(path), read_only=read_only)
    try:
        con.execute("SET TimeZone = 'UTC'")
        _read_checked(con, path)
    except BaseException:
        con.close()
        raise
    return con


def connect_agent(
    store_dir: Path, *, memory_limit: str | None = None, threads: int | None = None
) -> duckdb.DuckDBPyConnection:
    """`record.duckdb` as an agent's queries must see it: read-only, with no access to any other
    file (DuckDB's `enable_external_access` off: no `read_blob`, `glob`, `read_csv`, `ATTACH`,
    `COPY`, or extension install), its time zone UTC, and its configuration locked so no query
    can undo either. This is what keeps `heldout.duckdb` and `blobs/` out of an agent's reach
    (ADR-0014).

    Connections to one store in one process share a DuckDB instance: the first sets the time
    zone globally and locks it, and later ones find both already done. One gap the lock leaves:
    `enable_logging()` still runs, and logging to a file it cannot write makes every later
    query on the instance fail and closing it abort the process. No file is read, but the
    query runner therefore runs each agent query in a disposable child process and refuses
    statements that name the logging functions (`trajlab.store.query`, ADR-0019).

    `memory_limit` (a DuckDB size such as `512MB`) and `threads` bound that process's instance.
    They are part of the instance's configuration, which every connection to one file in a
    process must share, so pass them only where the store is opened once, as the runner's
    child does.
    """
    path = record_path(store_dir)
    if not path.is_file():
        raise StoreError(f"{path} does not exist")
    config: dict[str, Any] = {"enable_external_access": False}
    if memory_limit is not None:
        config["memory_limit"] = memory_limit
    if threads is not None:
        config["threads"] = threads
    con = duckdb.connect(str(path), read_only=True, config=config)
    try:
        zone, locked = con.execute(
            "SELECT current_setting('TimeZone'), current_setting('lock_configuration')"
        ).fetchone()  # type: ignore[misc]
        if not locked:
            if zone != "UTC":
                con.execute("SET GLOBAL TimeZone = 'UTC'")
            con.execute("SET lock_configuration = true")
        elif zone != "UTC":
            raise StoreError(f"{path} is open in this process with time zone {zone}, not UTC")
        _read_checked(con, path)
    except BaseException:
        con.close()
        raise
    return con


def _read_checked(con: duckdb.DuckDBPyConnection, path: Path) -> StoreMeta:
    try:
        meta = read_meta(con)
    except SchemaVersionError as error:
        raise SchemaVersionError(error.version, path) from None
    if meta.derived != derived_id():
        raise StoreError(
            f"{path} has derived views {meta.derived}, not {derived_id()}; "
            f"run `trajlab store derive {path.parent}` to replace them"
        )
    return meta


def insert_rows(con: duckdb.DuckDBPyConnection, table: str, rows: Sequence[BaseModel]) -> None:
    """Insert row models into `table`, columns named by the models' fields, in field order.

    Rows are written as newline-delimited JSON to a temporary file and loaded with `read_json`,
    typed by the table's own columns: about a thousand times faster than `executemany`, which
    managed some 600 rows a second (a package install can give tens of thousands of delta rows).
    A string holding a lone surrogate, which neither JSON nor DuckDB accepts, is stored with it
    written as `\\uXXXX`.
    """
    if not rows:
        return
    fields = list(type(rows[0]).model_fields)
    types = dict(con.execute("SELECT name, type FROM pragma_table_info(?)", [table]).fetchall())
    columns = ", ".join(f"'{name}': '{types[name]}'" for name in fields)
    with tempfile.TemporaryDirectory(prefix="trajlab-insert-") as directory:
        path = Path(directory) / f"{table}.ndjson"
        with path.open("w", encoding="utf-8") as out:
            for row in rows:
                out.write(json.dumps(_storable(row.model_dump(mode="json")), ensure_ascii=False))
                out.write("\n")
        con.execute(
            f"INSERT INTO {table} ({', '.join(fields)}) SELECT {', '.join(fields)} "
            f"FROM read_json(?, format = 'newline_delimited', columns = {{{columns}}})",
            [str(path)],
        )


def _storable(value: Any) -> Any:
    """`value` with every string's lone surrogates written as `\\uXXXX`."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "backslashreplace").decode("utf-8")
        return value
    if isinstance(value, dict):
        return {key: _storable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_storable(item) for item in value]
    return value
