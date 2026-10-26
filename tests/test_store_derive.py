"""The derived views (ADR-0018) on hand-made rows: each rule, and how the views are versioned."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb
import pytest

from trajlab.contracts import DeltaRow, StepRow, TrialRow
from trajlab.store import derive, schema
from trajlab.store.build import store_meta
from trajlab.store.derive import IDLE_SECONDS, VARIANT_MIN_LINES, VIEWS

TRIAL = UUID("e1eee115-a864-4c21-896b-5b60c8741d97")
OTHER = UUID("0b5e1d0a-0000-4000-8000-00000000c0de")
START = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
PROMPT = "Write the plan to /app/out/plan.json. The inputs are in /app/data/."


def trial(trial_id: UUID = TRIAL, prompt: str = PROMPT) -> TrialRow:
    return TrialRow(
        trial_id=trial_id,
        trial_name=f"task__{str(trial_id)[:7]}",
        task="terminal-bench/task",
        attempt_idx=0,
        prompt=prompt,
        model="anthropic/claude-sonnet-5-5",
        harness="claude-code",
    )


def call(
    idx: int,
    command: str = "ls",
    *,
    output: str = "",
    is_error: bool = False,
    seconds: float = 1,
    tool: str = "Bash",
    message: str | None = None,
    trial_id: UUID = TRIAL,
) -> StepRow:
    t_start = START + timedelta(minutes=idx)
    return StepRow(
        trial_id=trial_id,
        step_idx=idx,
        source="agent",
        tool_call_id=f"toolu_{idx}",
        tool=tool,
        args_json=f'{{"command": "{command}"}}',
        output=output,
        is_error=is_error,
        message=message,
        t_start=t_start,
        t_end=t_start + timedelta(seconds=seconds),
    )


def message(idx: int, text: str, trial_id: UUID = TRIAL) -> StepRow:
    return StepRow(trial_id=trial_id, step_idx=idx, source="agent", message=text, t_start=START)


def stop(idx: int, trial_id: UUID = TRIAL) -> StepRow:
    return StepRow(
        trial_id=trial_id, step_idx=idx, source="system", tool_call_id="stop_1", tool="Stop"
    )


def sha(n: int) -> str:
    return f"{n:064x}"


def change(
    idx: int,
    path: str,
    *,
    before: int | None = 1,
    after: int | None = 2,
    hunk: str | None = None,
    trial_id: UUID = TRIAL,
) -> DeltaRow:
    op = "add" if before is None else "delete" if after is None else "modify"
    return DeltaRow(
        trial_id=trial_id,
        step_idx=idx,
        path=path,
        op=op,
        kind_before=None if before is None else "file",
        kind_after=None if after is None else "file",
        is_text=True,
        hunk=hunk,
        sha256_before=None if before is None else sha(before),
        sha256_after=None if after is None else sha(after),
    )


def script(idx: int, path: str, lines: Sequence[str], trial_id: UUID = TRIAL) -> DeltaRow:
    """A script added at step `idx` with these lines."""
    body = "".join(f"+{line}\n" for line in lines)
    hunk = f"--- /dev/null\n+++ b{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    return change(idx, path, before=None, after=100 + idx, hunk=hunk, trial_id=trial_id)


def make_store(
    tmp_path: Path,
    steps: Sequence[StepRow],
    deltas: Sequence[DeltaRow] = (),
    trials: Sequence[TrialRow] = (),
) -> Path:
    store = tmp_path / "store"
    schema.create_store(store, store_meta())
    with schema.connect(schema.record_path(store), read_only=False) as con:
        schema.insert_rows(con, "trials", list(trials) or [trial()])
        schema.insert_rows(con, "steps", steps)
        schema.insert_rows(con, "deltas", deltas)
    return store


def rows(store: Path, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    with schema.connect(schema.record_path(store), read_only=True) as con:
        return con.execute(sql, list(params)).fetchall()


def flags(store: Path, column: str, trial_id: UUID = TRIAL) -> dict[int, Any]:
    return dict(
        rows(
            store,
            f"SELECT step_idx, {column} FROM step_flags WHERE trial_id = ? ORDER BY 1",
            [trial_id],
        )
    )


def summary(store: Path, column: str, trial_id: UUID = TRIAL) -> Any:
    ((value,),) = rows(store, f"SELECT {column} FROM trial_summary WHERE trial_id = ?", [trial_id])
    return value


# -- the views as declared ---------------------------------------------------------------------


@pytest.mark.parametrize("view", VIEWS, ids=lambda view: view.name)
def test_view_columns_are_the_declared_ones_in_order(tmp_path: Path, view: derive.View) -> None:
    store = make_store(tmp_path, [call(1)])
    with schema.connect(schema.record_path(store), read_only=True) as con:
        found = [row[0] for row in con.execute(f"DESCRIBE {view.name}").fetchall()]
    assert found == [column.name for column in view.columns]
    assert {column.view for column in view.columns} == {view.name}


def test_an_empty_store_has_empty_views(tmp_path: Path) -> None:
    store = tmp_path / "store"
    schema.create_store(store, store_meta())
    for view in VIEWS:
        assert rows(store, f"SELECT count(*) FROM {view.name}") == [(0,)]


def test_the_agent_connection_reads_the_views(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1)], [change(1, "/app/x")])
    with schema.connect_agent(store) as con:
        for view in VIEWS:
            assert con.execute(f"SELECT count(*) FROM {view.name}").fetchone() == (1,)


# -- step_flags --------------------------------------------------------------------------------


def test_cache_files_alone_do_not_make_a_call_mutating(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2), call(3)],
        [
            change(1, "/app/x.py"),
            change(2, "/app/__pycache__/x.cpython-312.pyc"),
            change(3, "/app/.pytest_cache/v/cache/lastfailed"),
            change(3, "/app/x.py", before=2, after=3),
        ],
    )
    assert flags(store, "is_mutating") == {1: True, 2: False, 3: True}
    assert flags(store, "files_touched") == {1: 1, 2: 1, 3: 2}


def test_tail_probes_are_unchanging_calls_after_the_last_change(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2), call(3), message(4, "Checking."), call(5), call(6), stop(7)],
        [change(3, "/app/x")],
    )
    assert flags(store, "is_tail_probe") == {
        1: False,
        2: False,
        3: False,
        4: False,  # not a tool call
        5: True,
        6: True,
        7: False,  # a stop record
    }
    assert summary(store, "tail_probes") == 2
    assert summary(store, "last_mutating_step") == 3


def test_every_call_is_a_tail_probe_when_nothing_changed(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1), call(2)])
    assert flags(store, "is_tail_probe") == {1: True, 2: True}
    assert summary(store, "last_mutating_step") is None


def test_an_idle_wait_ran_long_and_changed_nothing(tmp_path: Path) -> None:
    long = IDLE_SECONDS + 1
    store = make_store(
        tmp_path,
        [call(1, seconds=long), call(2, seconds=long), call(3, seconds=IDLE_SECONDS)],
        [change(2, "/app/log")],
    )
    assert flags(store, "is_idle_wait") == {1: True, 2: False, 3: False}


def test_a_call_without_an_end_is_not_an_idle_wait(tmp_path: Path) -> None:
    unfinished = call(1).model_copy(update={"t_end": None})
    store = make_store(tmp_path, [unfinished])
    assert flags(store, "is_idle_wait") == {1: False}


@pytest.mark.parametrize(
    ("output", "is_error", "expected"),
    [
        ("Exit code 1\nboom", False, True),
        ("Exit code 143\nCommand timed out after 2m 0s", False, True),
        ("ok\n[exit_code] 2", False, True),
        ('Traceback (most recent call last):\n  File "x.py"', False, True),
        ("FAILED tests/test_x.py::test_y - assert 1 == 2", False, True),
        ("==== 3 failed, 9 passed in 0.5s ====", False, True),
        ("test result: FAILED. 1 passed; 1 failed", False, True),
        ("ModuleNotFoundError: No module named 'scipy'", False, True),
        ("all 12 checks passed", False, False),
        ("the word Error: appears mid-line", False, False),
        ("", True, True),
    ],
)
def test_error_markers(tmp_path: Path, output: str, is_error: bool, expected: bool) -> None:
    store = make_store(tmp_path, [call(1, output=output, is_error=is_error)])
    assert flags(store, "has_error_marker") == {1: expected}


def test_first_error_step(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1), call(2, output="Exit code 1"), call(3, is_error=True)])
    assert summary(store, "first_error_step") == 2


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd /app && python -m pytest -q", True),
        ("npm run test", True),
        ("cargo test --release", True),
        ("make -C build check", True),
        ("bash run_tests.sh", True),
        ("python check.py", False),
        ("cat pytest.ini", True),  # a mention counts: the pattern is the trace site's
    ],
)
def test_test_commands(tmp_path: Path, command: str, expected: bool) -> None:
    store = make_store(tmp_path, [call(1, command)])
    assert flags(store, "is_test_command") == {1: expected}


def test_only_bash_calls_are_test_commands(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1, "pytest", tool="Write")])
    assert flags(store, "is_test_command") == {1: False}


# -- delta_flags and path_versions ---------------------------------------------------------------


def delta_flags(store: Path, column: str) -> dict[tuple[int, str], Any]:
    found = rows(store, f"SELECT step_idx, path, {column} FROM delta_flags ORDER BY 1, 2")
    return {(idx, path): value for idx, path, value in found}


def test_versions_and_reverts_of_a_path(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2), call(3), call(4)],
        [
            change(1, "/app/x", before=None, after=1),
            change(2, "/app/x", before=1, after=2),
            change(3, "/app/x", before=2, after=1),  # back to version 1's content
            change(4, "/app/x", before=1, after=None),
        ],
    )
    assert delta_flags(store, "version") == {
        (1, "/app/x"): 1,
        (2, "/app/x"): 2,
        (3, "/app/x"): 3,
        (4, "/app/x"): 4,
    }
    assert delta_flags(store, "is_revert") == {
        (1, "/app/x"): False,
        (2, "/app/x"): False,
        (3, "/app/x"): True,
        (4, "/app/x"): False,  # a delete has no later content
    }


def test_a_deleted_file_written_again_is_a_revert(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2)],
        [change(1, "/app/x", before=5, after=None), change(2, "/app/x", before=None, after=5)],
    )
    assert delta_flags(store, "is_revert") == {(1, "/app/x"): False, (2, "/app/x"): True}


def test_paths_the_instruction_names(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1)],
        [
            change(1, "/app/out/plan.json"),  # named, before the sentence's period
            change(1, "/app/data/orders.csv"),  # under a named dir: not named itself
            change(1, "/app/out/plan.json.bak"),
        ],
    )
    assert delta_flags(store, "names_prompt_path") == {
        (1, "/app/data/orders.csv"): False,
        (1, "/app/out/plan.json"): True,
        (1, "/app/out/plan.json.bak"): False,
    }


def test_cache_files(tmp_path: Path) -> None:
    paths = ["/app/__pycache__/a.cpython-312.pyc", "/app/b.pyc", "/app/.pytest_cache/x", "/a.py"]
    store = make_store(tmp_path, [call(1)], [change(1, path) for path in paths])
    assert delta_flags(store, "is_cache") == {
        (1, "/a.py"): False,
        (1, "/app/.pytest_cache/x"): True,
        (1, "/app/__pycache__/a.cpython-312.pyc"): True,
        (1, "/app/b.pyc"): True,
    }


PLAN = [f"line {n}" for n in range(10)]


def test_a_script_that_repeats_an_earlier_one_is_its_variant(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(n) for n in range(1, 6)],
        [
            script(1, "/w/plan.py", PLAN),
            script(2, "/w/other.py", [f"other {n}" for n in range(10)]),
            script(3, "/w/p2.py", [*PLAN[:6], "new a", "new b", "new c", "new d"]),  # 6 of 10
            script(4, "/w/p3.py", [*PLAN[:4], *(f"new {n}" for n in range(6))]),  # 4 of 10
            script(5, "/w/p4.py", [*PLAN[:6], "new a", "new b", "new c", "new e"]),
        ],
    )
    assert delta_flags(store, "variant_of") == {
        (1, "/w/plan.py"): None,
        (2, "/w/other.py"): None,
        (3, "/w/p2.py"): "/w/plan.py",
        (4, "/w/p3.py"): None,
        # p4 shares 6 lines with plan.py and 9 with p2.py: the closer one wins.
        (5, "/w/p4.py"): "/w/p2.py",
    }
    assert summary(store, "scripts_added") == 5
    assert summary(store, "script_variants") == 2


def test_a_short_script_is_no_variant(tmp_path: Path) -> None:
    short = PLAN[: VARIANT_MIN_LINES - 1]
    store = make_store(
        tmp_path, [call(1), call(2)], [script(1, "/w/a.py", PLAN), script(2, "/w/b.py", short)]
    )
    assert delta_flags(store, "variant_of")[(2, "/w/b.py")] is None


def test_scripts_are_known_by_extension_and_only_when_added(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2)],
        [
            script(1, "/w/a.sh", PLAN),
            script(1, "/w/notes.txt", PLAN),
            change(2, "/w/a.sh", before=101, after=7),
        ],
    )
    assert delta_flags(store, "is_script_add") == {
        (1, "/w/a.sh"): True,
        (1, "/w/notes.txt"): False,
        (2, "/w/a.sh"): False,
    }
    assert summary(store, "scripts_added") == 1


def test_scripts_added_in_one_step_are_not_variants_of_each_other(tmp_path: Path) -> None:
    store = make_store(
        tmp_path, [call(1)], [script(1, "/w/a.py", PLAN), script(1, "/w/b.py", PLAN)]
    )
    assert set(delta_flags(store, "variant_of").values()) == {None}


def test_path_versions(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2), call(3)],
        [
            change(1, "/app/out/plan.json", before=None, after=1, hunk="+v1\n"),
            change(3, "/app/out/plan.json", before=1, after=2, hunk="-v1\n+v2\n"),
            change(2, "/app/blob.bin", before=None, after=3),
        ],
    )
    found = rows(
        store,
        "SELECT path, versions, steps, first_step, last_step, last_op, last_hunk, "
        "names_prompt_path FROM path_versions ORDER BY path",
    )
    assert found == [
        ("/app/blob.bin", 1, [2], 2, 2, "add", None, False),
        ("/app/out/plan.json", 2, [1, 3], 1, 3, "modify", "-v1\n+v2\n", True),
    ]
    assert summary(store, "prompt_path_versions") == 2


def test_a_last_version_without_a_hunk_has_none(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2)],
        [
            change(1, "/app/x", before=None, after=1, hunk="+v1\n"),
            change(2, "/app/x", before=1, after=None),
        ],
    )
    assert rows(store, "SELECT last_op, last_hunk FROM path_versions") == [("delete", None)]


# -- trial_summary -----------------------------------------------------------------------------


DONE = "All tests pass; the plan is written."


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        # A claim, and no test ran: unverified.
        ([call(1), message(2, DONE)], True),
        # A claim, and a passing test ran after the last change: verified.
        ([call(1), call(2, "pytest -q"), message(3, DONE)], False),
        # The test ran in the same call as the last change.
        ([call(1, "pytest -q"), message(2, DONE)], False),
        # The last test failed.
        ([call(1), call(2, "pytest -q", output="1 failed"), message(3, DONE)], True),
        # A test ran, then the agent changed something and claimed success.
        ([call(1, "pytest -q"), call(2), message(3, DONE)], True),
        # No claim.
        ([call(1), message(2, "I could not finish the plan.")], False),
        # No final message at all (the time cap).
        ([call(1)], False),
    ],
    ids=[
        "no-test",
        "tested",
        "tested-in-last-change",
        "failed",
        "changed-after",
        "no-claim",
        "cut",
    ],
)
def test_unverified_completion(tmp_path: Path, steps: list[StepRow], expected: bool) -> None:
    store = make_store(
        tmp_path,
        steps,
        [change(step.step_idx, "/app/x", after=step.step_idx + 10) for step in steps[:-1]],
    )
    assert summary(store, "unverified_completion") is expected


def test_the_final_message_is_the_last_one(tmp_path: Path) -> None:
    store = make_store(tmp_path, [message(1, DONE), call(2), message(3, "Gave up.")])
    assert summary(store, "unverified_completion") is False


def test_a_trial_without_steps_has_a_summary(tmp_path: Path) -> None:
    store = make_store(tmp_path, [])
    found = rows(
        store,
        "SELECT tool_calls, mutating_calls, tail_probes, unverified_completion FROM trial_summary",
    )
    assert found == [(0, 0, 0, False)]


def test_a_trials_values_do_not_depend_on_the_other_trials(tmp_path: Path) -> None:
    def trial_rows(trial_id: UUID) -> tuple[list[StepRow], list[DeltaRow]]:
        steps = [
            call(1, trial_id=trial_id),
            call(2, "pytest", trial_id=trial_id),
            stop(3, trial_id),
        ]
        deltas = [
            script(1, "/w/plan.py", PLAN, trial_id=trial_id),
            change(2, "/w/plan.py", before=101, after=1, trial_id=trial_id),
        ]
        return steps, deltas

    alone = make_store(tmp_path / "alone", *trial_rows(TRIAL))
    steps, deltas = trial_rows(TRIAL)
    other_steps, other_deltas = trial_rows(OTHER)
    shared = make_store(
        tmp_path / "shared",
        [*steps, *other_steps],
        [*deltas, *other_deltas],
        [trial(TRIAL), trial(OTHER)],
    )
    for view in VIEWS:
        query = f"SELECT * FROM {view.name} WHERE trial_id = ? ORDER BY ALL"
        assert rows(alone, query, [TRIAL]) == rows(shared, query, [TRIAL]), view.name


# -- versioning --------------------------------------------------------------------------------


def test_the_derived_id_follows_the_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    before = derive.derived_id()
    assert before.startswith(f"{derive.DERIVE_VERSION}+")
    changed = derive.View(VIEWS[0].name, VIEWS[0].description, VIEWS[0].columns, "SELECT 1")
    monkeypatch.setattr(derive, "VIEWS", (changed, *VIEWS[1:]))
    assert derive.derived_id() != before


def test_a_store_with_other_views_is_refused_with_the_command_that_fixes_it(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    old = store_meta().model_copy(update={"derived": "0+old"})
    schema.create_store(store, old)
    with pytest.raises(schema.StoreError, match="run `trajlab store derive"):
        schema.create_store(store, store_meta())


def test_replace_views_swaps_the_views_and_keeps_the_rows(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1)], [change(1, "/app/x")])
    meta = store_meta()
    with schema.connect(schema.record_path(store), read_only=False) as con:
        con.execute("DROP VIEW trial_summary")
        con.execute("CREATE VIEW trial_summary AS SELECT 1 AS stale")
        con.execute("CREATE VIEW leftover AS SELECT 1 AS x")
        con.execute("UPDATE store_meta SET value = '\"0+old\"' WHERE key = 'derived'")
    with schema.connect(schema.heldout_path(store), read_only=False) as con:
        con.execute("UPDATE store_meta SET value = '\"0+old\"' WHERE key = 'derived'")

    assert schema.replace_views(store, meta) == "0+old"

    schema.create_store(store, meta)  # no longer refused
    for path in (schema.record_path(store), schema.heldout_path(store)):
        with schema.connect(path, read_only=True) as con:
            assert schema.read_meta(con) == meta
    views = {
        name for (name,) in rows(store, "SELECT view_name FROM duckdb_views() WHERE NOT internal")
    }
    assert views == {view.name for view in VIEWS}
    assert summary(store, "tool_calls") == 1
    assert rows(store, "SELECT count(*) FROM deltas") == [(1,)]
    assert schema.replace_views(store, meta) == meta.derived  # again: nothing to do


def test_replace_views_refuses_a_store_built_under_other_settings(tmp_path: Path) -> None:
    store = tmp_path / "store"
    schema.create_store(store, store_meta().model_copy(update={"text_rule": "ascii only"}))
    with pytest.raises(schema.StoreError, match="was built under"):
        schema.replace_views(store, store_meta())


def test_replace_views_refuses_a_missing_store(tmp_path: Path) -> None:
    with pytest.raises(schema.StoreError, match="does not exist"):
        schema.replace_views(tmp_path / "nothing", store_meta())


def test_replace_views_is_all_or_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path, [call(1)])
    broken = derive.View("trial_summary", "x", VIEWS[-1].columns, "SELECT * FROM no_such_table")
    monkeypatch.setattr(schema, "VIEWS", (*VIEWS[:-1], broken))
    with pytest.raises(duckdb.Error):
        schema.replace_views(store, store_meta())
    assert summary(store, "tool_calls") == 1  # the old views are still there


# -- upgrading a schema-1 store ------------------------------------------------------------------


def as_schema_1(store: Path) -> None:
    """Turn a store into what schema-1 code built: the three step columns, no views, no
    `derived`."""
    with duckdb.connect(str(schema.record_path(store))) as con:
        for column, sql_type in [
            ("is_loop", "BOOLEAN"),
            ("is_revert", "BOOLEAN"),
            ("files_touched", "INTEGER"),
        ]:
            con.execute(f"ALTER TABLE steps ADD COLUMN {column} {sql_type}")
        for view in reversed(VIEWS):
            con.execute(f"DROP VIEW {view.name}")
    for path in (schema.record_path(store), schema.heldout_path(store)):
        with duckdb.connect(str(path)) as con:
            con.execute("UPDATE store_meta SET value = '1' WHERE key = 'schema_version'")
            con.execute("DELETE FROM store_meta WHERE key = 'derived'")


def test_a_schema_1_store_is_refused_with_the_command_that_upgrades_it(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1)])
    as_schema_1(store)
    for refused in (
        lambda: schema.connect(schema.record_path(store), read_only=True),
        lambda: schema.connect_agent(store),
        lambda: schema.create_store(store, store_meta()),
    ):
        with pytest.raises(schema.SchemaVersionError, match=f"`trajlab store derive {store}`"):
            refused()


def test_replace_views_upgrades_a_schema_1_store(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1), call(2)], [change(1, "/app/x")])
    with schema.connect(schema.record_path(store), read_only=True) as con:
        steps_before = con.execute("SELECT * FROM steps ORDER BY ALL").fetchall()
        columns = con.execute("DESCRIBE steps").fetchall()
    as_schema_1(store)

    assert schema.replace_views(store, store_meta()) == "schema 1"

    schema.create_store(store, store_meta())  # accepted as this code's
    with schema.connect(schema.record_path(store), read_only=True) as con:
        assert con.execute("DESCRIBE steps").fetchall() == columns
        assert con.execute("SELECT * FROM steps ORDER BY ALL").fetchall() == steps_before
    with schema.connect(schema.heldout_path(store), read_only=True) as con:
        assert schema.read_meta(con) == store_meta()
    assert summary(store, "mutating_calls") == 1


def test_a_schema_1_store_with_a_derived_value_is_not_upgraded(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1)])
    as_schema_1(store)
    with duckdb.connect(str(schema.record_path(store))) as con:
        con.execute("UPDATE steps SET files_touched = 3")
    with pytest.raises(schema.StoreError, match="1 steps rows have a value"):
        schema.replace_views(store, store_meta())
    with duckdb.connect(str(schema.record_path(store)), read_only=True) as con:
        assert con.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        ).fetchone() == ("1",)
