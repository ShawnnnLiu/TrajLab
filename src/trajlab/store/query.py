"""The host side of the record-engine query tool (ADR-0019): check one statement, run it in a
disposable child process against the agent's connection, and render the answer.

An agent's query is one `SELECT` or `WITH` statement. `check_statement` parses it with DuckDB's
own parser and refuses anything else: another statement type, two statements, a `PRAGMA`,
`DESCRIBE`, `SHOW`, or `FROM`-first form (each parses as a SELECT but is not one lexically), or a
function on `DENIED_FUNCTIONS`, found by name in the statement's serialized syntax tree so that
quoting and case cannot hide it. The denied functions are the table functions that act on the
database instance past the configuration lock `connect_agent` sets: logging (which can break the
instance), profiling (which prints into the answer), checkpointing, and the two that run SQL
given as a string, past this check. Audited against DuckDB `AUDITED_DUCKDB`; a test fails on any
other version so the list is re-read.

`run_query` executes the statement in a child process (`trajlab.store.query_child`): the child opens
the store with `connect_agent`, under the memory and thread limits given, checks and runs the
statement, and writes the rows as JSON lines in DuckDB's own encoding (the same `to_json` the
`record-files` export writes, so both record arms see values spelled alike). The parent never
opens the store: a child that hangs is killed at the timeout, a child that breaks its instance
dies alone, and nothing it does outlives it. Fetching stops at the cap plus one row, so a wide
query costs the cap, not the table.
"""

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb

from trajlab.store.schema import connect_agent

logger = logging.getLogger(__name__)

DEFAULT_ROW_CAP = 200
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MEMORY_LIMIT = "512MB"
DEFAULT_THREADS = 2
# The DuckDB release whose table functions were audited for DENIED_FUNCTIONS (2026-10-26).
AUDITED_DUCKDB = "1.5.6"
# Table functions a SELECT can call that act on the instance or run SQL from a string. The lock
# on the configuration does not stop them, and `PRAGMA` statements, their other spelling, are
# refused by the lexical rule.
DENIED_FUNCTIONS = frozenset(
    {
        "enable_logging",
        "disable_logging",
        "truncate_duckdb_logs",
        "enable_profiling",
        "disable_profiling",
        "checkpoint",
        "force_checkpoint",
        "query",
        "json_execute_serialized_sql",
    }
)
# What a refusal's message starts with, in the query log and the agent's stderr.
REFUSED = "refused: "
_LEADING = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)
_QUERY_KEYWORD = re.compile(r"(?:select|with)\b", re.IGNORECASE)
CHILD_MODULE = "trajlab.store.query_child"


class StatementRefused(ValueError):
    """The statement is not one single read-only query; the message says why."""


def body(sql: str) -> str:
    """`sql` past its leading whitespace and comments."""
    match = _LEADING.match(sql)
    return sql[match.end() if match else 0 :]


def function_names(sql: str) -> frozenset[str]:
    """Every function the statement calls, scalar or table, by its catalog name (lower case,
    quoting and case folded), from DuckDB's serialized syntax tree.

    Raises `StatementRefused` if DuckDB cannot serialize the statement, which it does only for
    a SELECT, so a form it does not serialize is not analyzed and not run.
    """
    with duckdb.connect(":memory:") as con:
        (text,) = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()  # type: ignore[misc]
    tree = json.loads(text)
    if tree.get("error"):
        raise StatementRefused(f"cannot be analyzed: {tree.get('error_message', 'unknown')}")
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            name = node.get("function_name")
            if isinstance(name, str):
                names.add(name.lower())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tree)
    return frozenset(names)


def check_statement(sql: str) -> None:
    """Raise `StatementRefused` unless `sql` is one SELECT or WITH query that calls no denied
    function. Parsing is DuckDB's; nothing is executed."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        raise StatementRefused(f"not valid SQL: {_first_line(error)}") from error
    if len(statements) != 1:
        raise StatementRefused(f"one statement at a time, not {len(statements)}")
    (statement,) = statements
    if statement.type != duckdb.StatementType.SELECT or not _QUERY_KEYWORD.match(body(sql)):
        raise StatementRefused("only a SELECT or WITH query")
    denied = sorted(function_names(sql) & DENIED_FUNCTIONS)
    if denied:
        raise StatementRefused(f"function {denied[0]} is not available")


def execute(
    store_dir: Path, sql: str, *, cap: int, memory_limit: str | None, threads: int | None
) -> tuple[list[str], bool]:
    """Check `sql` and run it on the agent's connection in this process: at most `cap` rows as
    JSON text, and whether the query had more. The child's body; call `run` instead."""
    check_statement(sql)
    with connect_agent(store_dir, memory_limit=memory_limit, threads=threads) as con:
        relation = con.sql(sql)
        if relation is None:  # a statement with no result; the check refuses those
            raise StatementRefused("only a SELECT or WITH query")
        rows = relation.set_alias("q").project("to_json(q)").fetchmany(cap + 1)
    texts = [row[0] for row in rows[:cap]]
    return texts, len(rows) > cap


@dataclass(frozen=True)
class Outcome:
    """What one query came to: its rows, or why there are none, and the wall time it took."""

    rows: list[str] | None
    truncated: bool
    error: str | None
    latency_ms: int

    @property
    def refused(self) -> bool:
        return self.error is not None and self.error.startswith(REFUSED)


def run_query(
    store_dir: Path,
    sql: str,
    *,
    cap: int = DEFAULT_ROW_CAP,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_limit: str | None = DEFAULT_MEMORY_LIMIT,
    threads: int | None = DEFAULT_THREADS,
) -> Outcome:
    """Run one query in a disposable child process; never raises for anything the query did.

    The child is killed at `timeout_s` and the outcome says so; a child that exits with any
    error, including a refusal, gives an outcome whose `error` is its message; a child that
    dies otherwise gives one naming its exit status.
    """
    command = [
        sys.executable,
        "-m",
        CHILD_MODULE,
        str(store_dir),
        "--cap",
        str(cap),
        *(["--memory-limit", memory_limit] if memory_limit is not None else []),
        *(["--threads", str(threads)] if threads is not None else []),
    ]
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command, input=sql.encode("utf-8"), capture_output=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        latency = _ms(start)
        return Outcome(None, False, f"stopped after {timeout_s:g} s; narrow the query", latency)
    latency = _ms(start)
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    if completed.returncode == 1 and stderr:
        return Outcome(None, False, stderr, latency)
    if completed.returncode != 0:
        tail = stderr.splitlines()[-1] if stderr else "no message"
        return Outcome(None, False, f"query process exited {completed.returncode}: {tail}", latency)
    try:
        answer = json.loads(completed.stdout.decode("utf-8"))
        rows = list(answer["rows"])
        truncated = bool(answer["truncated"])
    except (ValueError, KeyError, TypeError) as error:
        return Outcome(None, False, f"query process gave no readable answer: {error}", latency)
    return Outcome(rows, truncated, None, latency)


def render(outcome: Outcome, cap: int) -> str:
    """The answer as the agent reads it: the rows, one JSON object per line, then one line
    saying how many, or that the cap cut them. `outcome` must have rows."""
    if outcome.rows is None:
        raise ValueError("render needs rows; the outcome is an error")
    if outcome.truncated:
        last = f"-- {cap} rows shown; the query has more. Add a LIMIT, or narrow it."
    else:
        n = len(outcome.rows)
        last = f"-- {n} row{'' if n == 1 else 's'}"
    return "".join(f"{row}\n" for row in outcome.rows) + last + "\n"


def _first_line(error: BaseException) -> str:
    return str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__


def _ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)
