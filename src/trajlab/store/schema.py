"""The Exp 2 store's DDL, in one place, versioned by `SCHEMA_VERSION` (ADR-0014).

A store is a directory:

    <store>/record.duckdb    trials, steps, deltas, store_meta: what an agent may read
    <store>/heldout.duckdb   checks, faults, blame, chains, store_meta: never shown to an agent
    <store>/blobs/<sha256>   binary file versions that deltas name in `blob_ref`

Keeping held-out tables in their own file makes "an agent cannot reach them" a property of which
file a connection opens, not of permissions inside one database (ADR-0014). Every table's
columns are, in order, the fields of its row model in `trajlab.contracts.store`; a test keeps
them equal. A shape change bumps `SCHEMA_VERSION` and needs an ADR.
"""

import json
from collections.abc import Sequence
from pathlib import Path

import duckdb
from pydantic import BaseModel

from trajlab.contracts import (
    BLOBS_DIRNAME,
    HELDOUT_DB_FILENAME,
    RECORD_DB_FILENAME,
    SCHEMA_VERSION,
    StoreMeta,
)

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
            is_loop BOOLEAN,
            is_revert BOOLEAN,
            files_touched INTEGER,
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
            PRIMARY KEY (trial_id, step_idx, path)
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


def _create(path: Path, tables: dict[str, str], meta: StoreMeta) -> None:
    with duckdb.connect(str(path)) as con:
        for ddl in tables.values():
            con.execute(ddl)
        con.execute(META_TABLE)
        con.executemany(
            "INSERT INTO store_meta VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in meta.model_dump(mode="json").items()],
        )


def read_meta(con: duckdb.DuckDBPyConnection) -> StoreMeta:
    rows = con.execute("SELECT key, value FROM store_meta").fetchall()
    return StoreMeta.model_validate({key: json.loads(value) for key, value in rows})


def create_store(store_dir: Path, meta: StoreMeta) -> None:
    """Create an empty store in `store_dir`, or check that the one there matches `meta`.

    Raises `StoreError` if an existing store was built under a different schema version,
    exclusion list, text rule, or reconcile method: rows from both would not be comparable.
    """
    if meta.schema_version != SCHEMA_VERSION:
        raise StoreError(f"this code writes schema {SCHEMA_VERSION}, not {meta.schema_version}")
    store_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir(store_dir).mkdir(exist_ok=True)
    for path, tables in (
        (record_path(store_dir), RECORD_TABLES),
        (heldout_path(store_dir), HELDOUT_TABLES),
    ):
        if not path.exists():
            _create(path, tables, meta)
            continue
        with duckdb.connect(str(path), read_only=True) as con:
            found = read_meta(con)
        if found != meta:
            raise StoreError(f"{path} was built under {found}, not {meta}")


def connect(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    """Open one of a store's database files; it must exist and carry this SCHEMA_VERSION.

    The session's time zone is UTC, so TIMESTAMPTZ values read back (and print) in UTC whatever
    the host's zone is. DuckDB needs `pytz` to return them to Python.
    """
    if not path.is_file():
        raise StoreError(f"{path} does not exist")
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute("SET TimeZone = 'UTC'")
    version = read_meta(con).schema_version
    if version != SCHEMA_VERSION:
        con.close()
        raise StoreError(f"{path} has schema {version}; this code reads {SCHEMA_VERSION}")
    return con


def insert_rows(con: duckdb.DuckDBPyConnection, table: str, rows: Sequence[BaseModel]) -> None:
    """Insert row models into `table`, columns named by the models' fields, in field order."""
    if not rows:
        return
    fields = list(type(rows[0]).model_fields)
    placeholders = ", ".join("?" for _ in fields)
    con.executemany(
        f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders})",
        [
            [
                list(value) if isinstance(value, tuple) else value
                for value in row.model_dump().values()
            ]
            for row in rows
        ],
    )
