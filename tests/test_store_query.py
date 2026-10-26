"""The query tool's host side (ADR-0019): the statement check, the disposable child, the cap,
the timeout, what the agent's connection cannot reach, and the log entry."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError

from tests.test_store_derive import call, change, make_store, message, rows, stop
from trajlab.contracts import QueryLogEntry
from trajlab.store import query, schema
from trajlab.store.describe import relations
from trajlab.store.query import (
    AUDITED_DUCKDB,
    DENIED_FUNCTIONS,
    REFUSED,
    Outcome,
    StatementRefused,
    check_statement,
    function_names,
    render,
    run_query,
)

FAST = {"timeout_s": 30.0}


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Three tool calls, a message, and a stop record; two changed paths."""
    return make_store(
        tmp_path,
        [
            call(1, "ls", output="a\nb\n"),
            call(2, "python x.py", output="Traceback (most recent call last):\n", is_error=True),
            message(3, "Fixing it."),
            call(4, "python x.py", output="ok\n"),
            stop(5),
        ],
        [change(2, "/app/x.py", before=None, hunk="--- /dev/null\n+++ b/app/x.py\n+print(1)\n")],
    )


# -- the statement check --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("INSERT INTO steps VALUES (1)", "only a SELECT or WITH"),
        ("CREATE TABLE x (a INTEGER)", "only a SELECT or WITH"),
        ("CREATE VIEW v AS SELECT 1", "only a SELECT or WITH"),
        ("ATTACH 'heldout.duckdb' AS h", "only a SELECT or WITH"),
        ("COPY steps TO 'out.csv'", "only a SELECT or WITH"),
        ("COPY (SELECT 1) TO '/dev/stdout'", "only a SELECT or WITH"),
        ("PRAGMA version", "only a SELECT or WITH"),
        ("PRAGMA enable_profiling", "only a SELECT or WITH"),
        ("SET threads = 1", "only a SELECT or WITH"),
        ("SET VARIABLE v = 1", "only a SELECT or WITH"),
        ("CALL enable_logging()", "only a SELECT or WITH"),
        ("CALL checkpoint()", "only a SELECT or WITH"),
        ("DELETE FROM steps", "only a SELECT or WITH"),
        ("UPDATE steps SET tool = 'x'", "only a SELECT or WITH"),
        ("DROP TABLE steps", "only a SELECT or WITH"),
        ("LOAD json", "only a SELECT or WITH"),
        ("INSTALL httpfs", "only a SELECT or WITH"),
        ("EXPLAIN SELECT 1", "only a SELECT or WITH"),
        ("DESCRIBE steps", "only a SELECT or WITH"),
        ("SHOW TABLES", "only a SELECT or WITH"),
        ("SUMMARIZE steps", "only a SELECT or WITH"),
        ("FROM steps", "only a SELECT or WITH"),
        ("VALUES (1)", "only a SELECT or WITH"),
        ("(SELECT 1)", "only a SELECT or WITH"),
        ("BEGIN", "only a SELECT or WITH"),
        ("EXPORT DATABASE 'd'", "only a SELECT or WITH"),
        ("SELECT 1; SELECT 2", "one statement at a time, not 2"),
        ("SELECT 1;\nDELETE FROM steps", "one statement at a time, not 2"),
        ("", "one statement at a time, not 0"),
        ("  -- a comment only\n", "one statement at a time, not 0"),
        ("SELECT * FROM enable_logging()", "function enable_logging is not available"),
        ('SELECT * FROM "enable_LOGGING"()', "function enable_logging is not available"),
        ("SELECT * FROM disable_logging()", "function disable_logging is not available"),
        ("SELECT * FROM truncate_duckdb_logs()", "function truncate_duckdb_logs is not available"),
        ("SELECT * FROM enable_profiling()", "function enable_profiling is not available"),
        ("SELECT * FROM disable_profiling()", "function disable_profiling is not available"),
        ("SELECT * FROM checkpoint()", "function checkpoint is not available"),
        ("SELECT * FROM force_checkpoint()", "function force_checkpoint is not available"),
        ("SELECT * FROM query('SELECT 1')", "function query is not available"),
        ("WITH t AS (SELECT * FROM query('SELECT 1')) SELECT * FROM t", "function query is not"),
        (
            "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT 1'))",
            "function json_execute_serialized_sql is not available",
        ),
        ("SELECT 1 FROM steps FOR UPDATE", "not valid SQL"),
        ("not sql at all", "not valid SQL"),
        ("/* unterminated SELECT 1", "not valid SQL"),
    ],
)
def test_the_check_refuses_every_form_that_is_not_one_query(sql: str, reason: str) -> None:
    with pytest.raises(StatementRefused, match=re.escape(reason)):
        check_statement(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "SELECT 1;",
        "SELECT 1 -- a comment",
        "  -- a comment\n  /* another */ SELECT 1",
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) "
        "SELECT * FROM t",
        "SELECT s.step_idx, d.path FROM steps s JOIN deltas d USING (trial_id, step_idx)",
        "SELECT * FROM step_flags WHERE is_mutating",
        "SELECT path FROM deltas WHERE hunk LIKE '%print%'",
        "SELECT regexp_matches(hunk, 'Error') FROM deltas",
        "SELECT * FROM range(3)",
        "SELECT * FROM duckdb_logs()",
        "SELECT * FROM pragma_version()",
        "SELECT current_setting('threads')",
    ],
)
def test_the_check_accepts_a_single_query(sql: str) -> None:
    check_statement(sql)


def test_function_names_fold_case_and_quoting() -> None:
    names = function_names("SELECT \"Regexp_Matches\"(hunk, 'x') FROM deltas, glob('*') g")
    assert {"regexp_matches", "glob"} <= names


def test_the_denied_functions_are_this_duckdbs_table_functions() -> None:
    # The audit is of one release; another release gets its table functions read again.
    assert duckdb.__version__ == AUDITED_DUCKDB, (
        f"DuckDB is {duckdb.__version__}, audited {AUDITED_DUCKDB}: re-read the table functions "
        "(trajlab.store.query, DENIED_FUNCTIONS) and bump AUDITED_DUCKDB"
    )
    with duckdb.connect(":memory:") as con:
        found = {
            name
            for (name,) in con.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() "
                "WHERE function_type = 'table' AND internal"
            ).fetchall()
        }
    assert DENIED_FUNCTIONS <= found


# -- running --------------------------------------------------------------------------------


def test_run_answers_in_the_exports_encoding(store: Path) -> None:
    # The same `to_json` the record-files export writes: both record arms see one spelling.
    with schema.connect(schema.record_path(store), read_only=True) as con:
        for name in relations():
            target = store.parent / f"{name}.json"
            con.execute(f"COPY (SELECT * FROM {name} ORDER BY ALL) TO '{target}' (FORMAT json)")
    for name in relations():
        outcome = run_query(store, f"SELECT * FROM {name} ORDER BY ALL", **FAST)
        assert outcome.error is None, outcome.error
        expected = (store.parent / f"{name}.json").read_text().splitlines()
        assert outcome.rows == expected, name


def test_run_reads_instants_in_utc(store: Path) -> None:
    outcome = run_query(store, "SELECT t_start FROM steps WHERE step_idx = 1", **FAST)
    assert outcome.rows == ['{"t_start":"2026-10-26 12:01:00+00"}']


def test_run_keeps_the_querys_order_and_caps_the_rows(store: Path) -> None:
    sql = "SELECT step_idx FROM steps ORDER BY step_idx DESC"
    full = run_query(store, sql, cap=5, **FAST)
    assert (full.rows, full.truncated) == (
        [f'{{"step_idx":{i}}}' for i in (5, 4, 3, 2, 1)],
        False,
    )
    cut = run_query(store, sql, cap=2, **FAST)
    assert (cut.rows, cut.truncated) == (['{"step_idx":5}', '{"step_idx":4}'], True)
    assert render(cut, 2) == (
        '{"step_idx":5}\n{"step_idx":4}\n'
        "-- 2 rows shown; the query has more. Add a LIMIT, or narrow it.\n"
    )
    assert render(full, 5).endswith("-- 5 rows\n")
    one = run_query(store, "SELECT count(*) AS n FROM deltas", **FAST)
    assert render(one, 200) == '{"n":1}\n-- 1 row\n'
    none = run_query(store, "SELECT 1 WHERE false", **FAST)
    assert render(none, 200) == "-- 0 rows\n"


def test_run_takes_a_trailing_semicolon_and_comment(store: Path) -> None:
    assert run_query(store, "SELECT count(*) AS n FROM steps; -- all\n", **FAST).rows == ['{"n":5}']


def test_run_reads_the_views(store: Path) -> None:
    outcome = run_query(
        store, "SELECT tool_calls, mutating_calls, first_error_step FROM trial_summary", **FAST
    )
    assert outcome.rows == ['{"tool_calls":3,"mutating_calls":1,"first_error_step":2}']


def test_run_cannot_reach_the_held_out_file_or_the_blobs(store: Path) -> None:
    heldout = schema.heldout_path(store)
    blob = schema.blobs_dir(store) / ("0" * 64)
    blob.write_bytes(b"secret")
    for sql in [
        f"SELECT * FROM read_blob('{heldout}')",
        f"SELECT * FROM read_text('{blob}')",
        f"SELECT * FROM glob('{store}/*')",
        f"SELECT * FROM '{heldout}'",
        f"SELECT * FROM read_csv('{blob}')",
        f"SELECT * FROM sniff_csv('{blob}')",
    ]:
        outcome = run_query(store, sql, **FAST)
        assert outcome.rows is None
        assert "file system operations are disabled" in (outcome.error or ""), sql
    for sql in ["SELECT * FROM checks", "SELECT * FROM heldout.checks"]:
        outcome = run_query(store, sql, **FAST)
        assert "does not exist" in (outcome.error or ""), sql


def test_run_reports_a_refusal_and_a_database_error(store: Path) -> None:
    refused = run_query(store, "DELETE FROM steps", **FAST)
    assert refused.rows is None and refused.refused
    assert refused.error == f"{REFUSED}only a SELECT or WITH query"
    failed = run_query(store, "SELECT nope FROM steps", **FAST)
    assert failed.rows is None and not failed.refused
    assert "Binder Error" in (failed.error or "") and "nope" in (failed.error or "")
    assert rows(store, "SELECT count(*) FROM steps") == [(5,)]


def test_run_kills_a_query_over_the_timeout(store: Path) -> None:
    outcome = run_query(
        store, "SELECT count(*) FROM range(100000000000) r(i) WHERE i % 7 = 3", timeout_s=0.8
    )
    assert outcome.rows is None
    assert outcome.error == "stopped after 0.8 s; narrow the query"
    assert outcome.latency_ms < 5000


def test_run_applies_the_memory_and_thread_limits(store: Path) -> None:
    sql = "SELECT current_setting('memory_limit') AS m, current_setting('threads') AS t"
    outcome = run_query(store, sql, memory_limit="64MB", threads=1, **FAST)
    assert outcome.rows == ['{"m":"61.0 MiB","t":1}']


def test_run_refuses_a_store_whose_views_are_stale(store: Path) -> None:
    with schema.connect(schema.record_path(store), read_only=False) as con:
        con.execute("UPDATE store_meta SET value = '\"0+stale\"' WHERE key = 'derived'")
    outcome = run_query(store, "SELECT 1", **FAST)
    assert outcome.rows is None
    assert "derived views" in (outcome.error or "") and "trajlab store derive" in outcome.error


def test_run_reports_a_missing_store(tmp_path: Path) -> None:
    outcome = run_query(tmp_path / "none", "SELECT 1", **FAST)
    assert outcome.rows is None and "does not exist" in (outcome.error or "")


def test_render_needs_rows() -> None:
    with pytest.raises(ValueError, match="error"):
        render(Outcome(None, False, "x", 1), 200)


# -- the log entry --------------------------------------------------------------------------


def test_log_entry_round_trips() -> None:
    entry = QueryLogEntry(
        query_id="q_1790000000_42",
        trial_name="task__abc1234",
        received_at=datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        sql="SELECT 1",
        rows=1,
        latency_ms=250,
    )
    assert QueryLogEntry.model_validate_json(entry.model_dump_json()) == entry
    failed = entry.model_copy(update={"rows": None, "error": f"{REFUSED}only a SELECT"})
    assert json.loads(failed.model_dump_json())["error"].startswith(REFUSED)


@pytest.mark.parametrize(
    "fields",
    [
        {"rows": None},  # no rows and no error
        {"error": "x"},  # rows and an error
        {"rows": None, "error": "x", "truncated": True},  # truncated without rows
        {"query_id": "../x"},
        {"rows": -1},
    ],
)
def test_log_entry_rejects_an_inconsistent_record(fields: dict[str, object]) -> None:
    base = {
        "query_id": "q_1",
        "trial_name": "t",
        "received_at": datetime(2026, 10, 26, tzinfo=UTC),
        "sql": "SELECT 1",
        "rows": 1,
        "latency_ms": 1,
    }
    with pytest.raises(ValidationError):
        QueryLogEntry.model_validate(base | fields)


def test_query_module_does_not_import_capture() -> None:
    import trajlab.store.serve as serve_module

    for module in (query, serve_module):
        source = Path(module.__file__).read_text()
        assert "trajlab.capture" not in source and "trajlab.checkpoint" not in source
