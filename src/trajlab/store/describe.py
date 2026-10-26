"""The record's schema as text, one rendering for both record arms (ADR-0018).

`describe(con)` lists every relation of `record.duckdb` an agent can read (the tables,
`store_meta`, and the derived views) with each column's type, from DuckDB's catalog, and its
description, from the row models in `trajlab.contracts` or the derived views' specs. The
`record-files` README is this text, and the `record-engine` prompt is to carry the same, so the
two arms are told the same about the record. A relation or column without a description is an
error, not a gap in the text.
"""

import duckdb
from pydantic import BaseModel

from trajlab.contracts import DeltaRow, StepRow, TrialRow
from trajlab.store.derive import VIEWS

_TABLES: dict[str, tuple[str, type[BaseModel] | None]] = {
    "trials": (
        "One row per attempt at the task: the instruction it got, its outcome, and its cost.",
        TrialRow,
    ),
    "steps": (
        "One row per tool call of an attempt, in order, and per message without one or "
        "end-of-turn record.",
        StepRow,
    ),
    "deltas": (
        "One row per path whose state changed at a step: what changed, with a unified diff for "
        "text.",
        DeltaRow,
    ),
    "store_meta": ("How this record was built: one row per setting.", None),
}
_META_COLUMNS = {"key": "The setting.", "value": "Its value, as JSON."}


class DescribeError(ValueError):
    """The record has a relation or column this module has no description for."""


def relations() -> list[str]:
    """Every relation an agent can read, in the order the text lists them."""
    tables = [name for name in _TABLES if name != "store_meta"]
    return [*tables, *(view.name for view in VIEWS), "store_meta"]


def _descriptions(name: str) -> tuple[str, dict[str, str]]:
    for view in VIEWS:
        if view.name == name:
            return view.description, {c.name: c.description for c in view.columns}
    description, model = _TABLES[name]
    if model is None:
        return description, _META_COLUMNS
    fields = model.model_fields
    return description, {field: info.description or "" for field, info in fields.items()}


def columns(con: duckdb.DuckDBPyConnection, name: str) -> list[tuple[str, str]]:
    """A relation's columns and their DuckDB types, in order."""
    return con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [name],
    ).fetchall()


def describe(con: duckdb.DuckDBPyConnection) -> str:
    """The schema text of the record open on `con`, as Markdown."""
    found = {
        name
        for (name,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    expected = relations()
    if found != set(expected):
        raise DescribeError(
            f"the record has relations {sorted(found)}; this text describes {sorted(expected)}"
        )
    parts: list[str] = []
    for name in expected:
        description, notes = _descriptions(name)
        lines = [f"## {name}", "", description, "", "| column | type | description |"]
        lines.append("|---|---|---|")
        for column, sql_type in columns(con, name):
            note = notes.get(column, "")
            if not note:
                raise DescribeError(f"{name}.{column} has no description")
            lines.append(f"| {column} | {sql_type} | {note.replace('|', '\\|')} |")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"
