"""The derived views (ADR-0018) on hand-made rows: each rule, and how the views are versioned."""

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb
import pytest

from trajlab.capture import trace
from trajlab.contracts import DeltaRow, JobNoticeRow, StepRow, TrialRow, WriteRow
from trajlab.store import derive, schema
from trajlab.store.build import store_meta
from trajlab.store.derive import IDLE_SECONDS, SUCCESS_CLAIM, VARIANT_MIN_LINES, VIEWS
from trajlab.store.steps import shell_text

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
        args_json=json.dumps({"command": command}),
        output=output,
        is_error=is_error,
        message=message,
        t_start=t_start,
        t_end=t_start + timedelta(seconds=seconds),
        command_shell=shell_text(command) if tool == "Bash" else None,
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
    mtime: datetime | None = None,
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
        mtime_after=None if after is None else mtime,
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
    notices: Sequence[JobNoticeRow] = (),
    writes: Sequence[WriteRow] = (),
) -> Path:
    store = tmp_path / "store"
    schema.create_store(store, store_meta())
    with schema.connect(schema.record_path(store), read_only=False) as con:
        schema.insert_rows(con, "trials", list(trials) or [trial()])
        schema.insert_rows(con, "steps", steps)
        schema.insert_rows(con, "deltas", deltas)
        schema.insert_rows(con, "job_notices", notices)
        schema.insert_rows(con, "writes", writes)
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
        ("test result: ok. 3 passed; 0 failed; 0 ignored", False, False),
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
        ("FOO=1 timeout 600 uv run pytest tests", True),
        ("if [ -d t ]; then pytest; fi", True),
        ("cd t; ./run_tests.sh", True),
        ("pip install pytest", False),  # a mention is not a run
        ("cat pytest.ini", False),
        ("grep -r pytest .", False),
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


def test_a_mode_change_alone_is_no_revert(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2)],
        [change(1, "/app/run.sh", before=5, after=5), change(2, "/app/run.sh", before=5, after=5)],
    )
    assert set(delta_flags(store, "is_revert").values()) == {False}


def test_a_deleted_file_written_again_is_a_revert(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1), call(2)],
        [change(1, "/app/x", before=5, after=None), change(2, "/app/x", before=None, after=5)],
    )
    assert delta_flags(store, "is_revert") == {(1, "/app/x"): False, (2, "/app/x"): True}


def test_a_directory_named_with_a_slash_and_urls(tmp_path: Path) -> None:
    prompt = "See https://example.com/app/out and write into /app/out/ only."
    store = make_store(
        tmp_path,
        [call(1)],
        [change(1, "/app/out"), change(1, "/app/out/x"), change(1, "/example.com/app/out")],
        [trial(prompt=prompt)],
    )
    assert delta_flags(store, "names_prompt_path") == {
        (1, "/app/out"): True,
        (1, "/app/out/x"): False,
        (1, "/example.com/app/out"): False,
    }


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


def test_a_claim_that_also_says_something_failed_is_no_claim(tmp_path: Path) -> None:
    for text in ["I could not get it to work.", "The plan is written, but the MES load failed."]:
        store = make_store(tmp_path / text[:5], [call(1), message(2, text)], [change(1, "/x")])
        assert summary(store, "unverified_completion") is False, text


def test_the_success_pattern_is_the_trace_sites() -> None:
    assert SUCCESS_CLAIM == f"(?i){trace._SUCCESS_CLAIM.pattern}"
    assert trace._SUCCESS_CLAIM.flags & re.IGNORECASE


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


def test_a_store_with_stale_views_is_not_read(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1)])
    with schema.connect(schema.record_path(store), read_only=False) as con:
        con.execute("UPDATE store_meta SET value = '\"0+old\"' WHERE key = 'derived'")
    for refused in (
        lambda: schema.connect(schema.record_path(store), read_only=True),
        lambda: schema.connect_agent(store),
    ):
        with pytest.raises(schema.StoreError, match="run `trajlab store derive"):
            refused()


def test_replace_views_finishes_a_store_left_half_done(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1)])
    with duckdb.connect(str(schema.heldout_path(store))) as con:
        con.execute("UPDATE store_meta SET value = '\"0+old\"' WHERE key = 'derived'")
    assert schema.replace_views(store, store_meta()) == "0+old"
    assert schema.replace_views(store, store_meta()) == store_meta().derived


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


# -- a store of another schema version ---------------------------------------------------------


def as_schema(store: Path, version: int) -> None:
    """Mark a store as built by code of another schema version."""
    for path in (schema.record_path(store), schema.heldout_path(store)):
        with duckdb.connect(str(path)) as con:
            con.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_version'", [version])


def test_a_store_of_another_schema_version_is_refused_and_told_to_rebuild(
    tmp_path: Path,
) -> None:
    """Since schema 3 the rows carry values an older store lacks (ADR-0020): no in-place
    upgrade, by `trajlab store derive` or anything else."""
    store = make_store(tmp_path, [call(1)], [change(1, "/app/x")])
    as_schema(store, 2)
    for refused in (
        lambda: schema.connect(schema.record_path(store), read_only=True),
        lambda: schema.connect_agent(store),
        lambda: schema.create_store(store, store_meta()),
        lambda: schema.replace_views(store, store_meta()),
    ):
        with pytest.raises(schema.SchemaVersionError, match="schema 2.*build it again"):
            refused()
    with duckdb.connect(str(schema.record_path(store)), read_only=True) as con:
        assert con.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        ).fetchone() == ("2",)


# -- step_jobs and delta_attribution (ADR-0020) -------------------------------------------------

TASK = "b4cnj2dq1"
BACKGROUNDED_OUTPUT = (
    "Command did not complete within its 120s timeout and was moved to the background "
    f"(ID: {TASK}). Output is being written to: /tmp/claude-0/-app/x/tasks/{TASK}.output. "
    "You will be notified when it completes."
)


def at(minutes: float, seconds: float = 0) -> datetime:
    return START + timedelta(minutes=minutes, seconds=seconds)


def notice(
    step: StepRow, minutes: float, status: str = "completed", task: str = TASK
) -> JobNoticeRow:
    assert step.tool_call_id is not None
    return JobNoticeRow(
        trial_id=step.trial_id,
        task_id=task,
        tool_call_id=step.tool_call_id,
        status=status,
        noticed_at=at(minutes),
    )


def jobs(store: Path, column: str, trial_id: UUID = TRIAL) -> dict[int, Any]:
    return dict(
        rows(
            store,
            f"SELECT step_idx, {column} FROM step_jobs WHERE trial_id = ? ORDER BY 1",
            [trial_id],
        )
    )


def attributions(
    store: Path, trial_id: UUID = TRIAL
) -> dict[tuple[int, str], tuple[str, int | None, int, bool]]:
    """(landing step, path) -> (attribution, launched_by_step, causal_step, is_late)."""
    found = rows(
        store,
        "SELECT step_idx, path, attribution, launched_by_step, causal_step, is_late "
        "FROM delta_attribution WHERE trial_id = ? ORDER BY 1, 2",
        [trial_id],
    )
    return {(k, path): (a, launched, causal, late) for k, path, a, launched, causal, late in found}


def test_a_backgrounded_call_and_its_job_window(tmp_path: Path) -> None:
    first = call(1, "python train.py", output=BACKGROUNDED_OUTPUT, seconds=120)
    store = make_store(
        tmp_path, [first, call(2, "ls"), call(3, "ls")], notices=[notice(first, 2.5)]
    )
    assert jobs(store, "backgrounded") == {1: True, 2: False, 3: False}
    assert jobs(store, "task_id") == {1: TASK, 2: None, 3: None}
    assert jobs(store, "job_status") == {1: "completed", 2: None, 3: None}
    assert jobs(store, "job_end") == {1: at(2.5), 2: None, 3: None}
    assert jobs(store, "background_done_step") == {1: 3, 2: None, 3: None}


def test_a_job_without_a_notice_runs_to_the_attempts_last_instant(tmp_path: Path) -> None:
    first = call(1, "python train.py", output=BACKGROUNDED_OUTPUT, seconds=120)
    last = call(5, "ls", seconds=7)  # after the backgrounded call's own t_end (its move)
    store = make_store(tmp_path, [first, last, message(6, "Waiting."), stop(7)])
    assert jobs(store, "job_end") == {1: last.t_end, 5: None}
    assert jobs(store, "background_done_step") == {1: None, 5: None}
    assert jobs(store, "job_status") == {1: None, 5: None}


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("nohup python x.py > x.log 2>&1 &", True),
        ("(python x.py > x.log 2>&1 &); sleep 60", True),
        ("python a.py L & python a.py R & wait", True),
        ("setsid python x.py", True),
        ("make && make test", False),
        ("python x.py 2>&1 | tail -3", False),
        ("python x.py &> /dev/null", False),
        ("cat > f.py <<'EOF'\nimport os\nif a & b: pass\nEOF\npython f.py", False),
        ("python -c 'import os; print(1 & 2)'", False),
        ('bash -c "nohup sleep 1"', False),
        ("ls", False),
        ("sed -i 's/a/[\\&]/' f.txt", False),  # an escaped `&` in a sed replacement
    ],
)
def test_hand_launched_jobs(tmp_path: Path, command: str, expected: bool) -> None:
    store = make_store(tmp_path, [call(1, command)])
    assert jobs(store, "launches_job") == {1: expected}


def test_a_stop_record_is_no_job(tmp_path: Path) -> None:
    store = make_store(tmp_path, [call(1, "ls"), stop(2)])
    assert list(jobs(store, "backgrounded")) == [1]


def test_a_write_in_the_landing_window_with_no_job_open_is_foreground(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1, "touch /app/x", seconds=2), call(2, "ls")],
        [change(1, "/app/x", before=None, mtime=at(1, 1))],
    )
    assert attributions(store) == {(1, "/app/x"): ("foreground", 1, 1, False)}


def test_a_late_write_of_a_backgrounded_job_goes_to_the_launching_call(tmp_path: Path) -> None:
    first = call(1, "python run.py > /app/out.json", output=BACKGROUNDED_OUTPUT, seconds=120)
    store = make_store(
        tmp_path,
        [first, call(2, "ls"), call(3, "ls"), call(4, "ls")],
        [
            change(3, "/app/out.json", before=None, mtime=at(2, 30)),  # between calls 2 and 3
            change(4, "/app/other", before=None, mtime=at(4, 0.5)),  # inside call 4's window
        ],
        notices=[notice(first, 4.5)],
    )
    assert attributions(store) == {
        (3, "/app/out.json"): ("background", 1, 1, True),
        # The job was still open, but nothing ties it to this file: the landing call wrote it.
        (4, "/app/other"): ("foreground", 4, 4, False),
    }


def test_a_write_after_the_jobs_end_is_not_its(tmp_path: Path) -> None:
    first = call(1, "python run.py > /app/out.json", output=BACKGROUNDED_OUTPUT, seconds=120)
    store = make_store(
        tmp_path,
        [first, call(2, "ls"), call(3, "ls")],
        [change(3, "/app/out.json", before=None, mtime=at(2, 30))],
        notices=[notice(first, 2.25)],
    )
    assert attributions(store) == {(3, "/app/out.json"): ("unknown", None, 3, False)}


def test_a_job_redirecting_into_the_file_beats_a_landing_call_that_does_not_write(
    tmp_path: Path,
) -> None:
    """The XK8w3KT case: `nohup ... > log_$s.txt &` at one call, `sleep; tail log_1[1-8].txt`
    at the next, and the log's time inside the tail call's window."""
    store = make_store(
        tmp_path,
        [
            call(
                1, "cd /tmp/w && for s in 11 12; do nohup python c.py $s > log_$s.txt 2>&1 & done"
            ),
            call(2, "sleep 115; tail -qn1 /tmp/w/log_1[1-2].txt", seconds=116),
            call(3, "sleep 115; tail -qn1 /tmp/w/log_11.txt", seconds=116),
        ],
        [
            change(2, "/tmp/w/log_11.txt", mtime=at(2, 110)),
            change(3, "/tmp/w/log_11.txt", before=2, after=3, mtime=at(3, 110)),
        ],
    )
    assert attributions(store) == {
        # `log_$s.txt` redirects into `log_11.txt` once `$s` stands for anything, and the
        # landing call writes nothing and names no file: the open job is the writer.
        (2, "/tmp/w/log_11.txt"): ("background_untracked", 1, 1, True),
        # A landing call that names the file literally could be the writer (it is not, but
        # the text cannot say so): contested, and the landing step is the causal step.
        (3, "/tmp/w/log_11.txt"): ("contested", None, 3, False),
    }


def test_the_latest_open_job_wins_unless_an_earlier_one_names_the_file(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            call(1, "nohup python a.py > /app/a.log 2>&1 &"),
            call(2, "nohup python b.py > /app/b.log 2>&1 &"),
            call(3, "sleep 30", seconds=30),
            call(4, "ls"),
        ],
        [
            change(4, "/app/a.log", mtime=at(3, 45)),
            change(4, "/app/c.log", before=None, mtime=at(3, 45)),
        ],
    )
    assert attributions(store) == {
        (4, "/app/a.log"): ("background_untracked", 1, 1, True),  # names it, over the latest
        (4, "/app/c.log"): ("background_untracked", 2, 2, True),  # the latest open job
    }


def test_background_by_path_names_the_overridden_latest_backgrounded_job(tmp_path: Path) -> None:
    other = BACKGROUNDED_OUTPUT.replace(TASK, "bzzzzzzzz")
    store = make_store(
        tmp_path,
        [
            call(1, "python a.py > /app/a.log", output=BACKGROUNDED_OUTPUT, seconds=120),
            call(2, "python b.py > /app/b.log", output=other, seconds=120),
            call(3, "ls"),
        ],
        [
            change(3, "/app/a.log", mtime=at(2, 30)),
            change(3, "/app/c.log", before=None, mtime=at(2, 30)),
        ],
    )
    assert attributions(store) == {
        (3, "/app/a.log"): ("background_by_path", 1, 1, True),
        (3, "/app/c.log"): ("background", 2, 2, True),
    }


def test_a_time_before_the_attempt_began_is_untrusted(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1, "tar xf a.tar", seconds=2), call(2, "nohup x &"), call(3, "tar xf b.tar")],
        [
            change(1, "/app/old", before=None, mtime=at(-60)),
            change(3, "/app/older", before=None, mtime=at(-60)),
        ],
    )
    assert attributions(store) == {
        (1, "/app/old"): ("untrusted_mtime", 1, 1, False),  # no job could have written it
        (3, "/app/older"): ("untrusted_mtime", None, 3, False),  # a job was open: unknown writer
    }


def test_a_delete_and_a_write_outside_every_window_are_unknown(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1, "rm /app/gone"), call(2, "ls")],
        [
            change(1, "/app/gone", after=None),
            change(2, "/app/x", mtime=at(1, 30)),  # between the two calls, no job open
        ],
    )
    assert attributions(store) == {
        (1, "/app/gone"): ("unknown", None, 1, False),
        (2, "/app/x"): ("unknown", None, 2, False),
    }


def test_the_landing_window_runs_to_the_checkpoint(tmp_path: Path) -> None:
    done = call(1, "python w.py", seconds=10)
    with_checkpoint = done.model_copy(
        update={
            "checkpoint_seq": 1,
            "checkpoint_id": sha(1),
            "checkpoint_at": done.t_end + timedelta(seconds=4),
        }
    )
    late_write = [change(1, "/app/x", mtime=done.t_end + timedelta(seconds=2))]
    assert attributions(make_store(tmp_path / "a", [done], late_write)) == {
        (1, "/app/x"): ("unknown", None, 1, False)
    }
    assert attributions(make_store(tmp_path / "b", [with_checkpoint], late_write)) == {
        (1, "/app/x"): ("foreground", 1, 1, False)
    }


def test_a_jobs_window_is_its_own_trials(tmp_path: Path) -> None:
    first = call(1, "python run.py", output=BACKGROUNDED_OUTPUT, seconds=120)
    store = make_store(
        tmp_path,
        [first, call(2, "ls"), call(1, "ls", trial_id=OTHER), call(2, "ls", trial_id=OTHER)],
        [
            change(2, "/app/x", mtime=at(1, 30)),
            change(2, "/app/x", mtime=at(1, 30), trial_id=OTHER),
        ],
        trials=[trial(), trial(OTHER)],
    )
    assert attributions(store) == {(2, "/app/x"): ("background", 1, 1, True)}
    assert attributions(store, OTHER) == {(2, "/app/x"): ("unknown", None, 2, False)}


def test_a_server_left_running_does_not_contest_later_writes(tmp_path: Path) -> None:
    """vba-userform-port__K3NYT7L: `uvicorn ... &` at one call has no end on record; a later
    npm install's writes are the install's, not the server's."""
    store = make_store(
        tmp_path,
        [
            call(1, "cd /app && nohup uvicorn api:app --port 8000 > /tmp/api.log 2>&1 &"),
            call(2, "cd /app/web && npm install", seconds=40),
        ],
        [
            change(2, "/app/web/node_modules/left-pad/index.js", before=None, mtime=at(2, 20)),
            change(2, "/tmp/api.log", mtime=at(2, 20)),
        ],
    )
    assert attributions(store) == {
        (2, "/app/web/node_modules/left-pad/index.js"): ("foreground", 2, 2, False),
        # The server's own log: it redirects into it and npm names nothing of it.
        (2, "/tmp/api.log"): ("background_untracked", 1, 1, True),
    }


def test_a_launcher_naming_the_file_or_its_directory_still_contests(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [
            call(1, "cd /tmp/w && nohup python grid.py results.csv &"),
            call(2, "python summarize.py /tmp/w/results.csv > /tmp/w/summary.txt", seconds=10),
        ],
        [
            change(2, "/tmp/w/results.csv", mtime=at(2, 5)),  # the launcher names it
            change(2, "/tmp/w/grid.log", before=None, mtime=at(2, 5)),  # its directory
            change(2, "/app/other.txt", before=None, mtime=at(2, 5)),  # neither: `/app` is shallow
        ],
    )
    assert attributions(store) == {
        (2, "/app/other.txt"): ("foreground", 2, 2, False),
        (2, "/tmp/w/grid.log"): ("contested", None, 2, False),
        (2, "/tmp/w/results.csv"): ("contested", None, 2, False),
    }


# -- the record of writing processes (ADR-0021) ------------------------------------------------


def written(
    idx: int, path: str, writer: int | None, when: datetime, trial_id: UUID = TRIAL
) -> WriteRow:
    return WriteRow(
        trial_id=trial_id,
        step_idx=idx,
        path=path,
        writer_step=writer,
        mapped_by=None if writer is None else "session",
        events=3,
        ops=("close_write", "modify"),
        first_at=when,
        last_at=when,
    )


def recorded(store: Path) -> dict[tuple[int, str], tuple[Any, ...]]:
    """(landing step, path) -> (attribution, causal_step, writer_steps, rule_attribution,
    rule_causal_step)."""
    found = rows(
        store,
        "SELECT step_idx, path, attribution, causal_step, writer_steps, rule_attribution, "
        "rule_causal_step FROM delta_attribution ORDER BY 1, 2",
    )
    return {(k, path): tuple(rest) for k, path, *rest in found}


def test_the_record_settles_what_the_write_time_cannot(tmp_path: Path) -> None:
    """The XK8w3KT case again, now with the writing processes recorded: the log a job keeps
    writing while the agent polls it is the job's, whatever the landing call names."""
    steps = [
        call(1, "cd /tmp/w && for s in 11 12; do nohup python c.py $s > log_$s.txt 2>&1 & done"),
        call(2, "sleep 115; tail -qn1 /tmp/w/log_11.txt", seconds=116),
        call(3, "python fix.py", seconds=5),
        call(4, "ls"),
    ]
    deltas = [
        change(2, "/tmp/w/log_11.txt", mtime=at(2, 110)),
        change(3, "/tmp/w/log_11.txt", before=2, after=3, mtime=at(3, 2)),
        change(4, "/app/result.bin", mtime=at(4, 0.5)),
        change(4, "/app/cache.db", mtime=at(4, 0.5)),
    ]
    writes = [
        written(2, "/tmp/w/log_11.txt", 1, at(2, 110)),
        written(3, "/tmp/w/log_11.txt", 1, at(3, 1)),
        written(3, "/tmp/w/log_11.txt", 3, at(3, 2)),
        written(4, "/app/cache.db", None, at(4, 0.5)),  # only a process of no call
    ]
    store = make_store(tmp_path, steps, deltas, writes=writes)
    assert recorded(store) == {
        (2, "/tmp/w/log_11.txt"): ("recorded", 1, [1], "contested", 2),
        # The rule names the job alone; the record saw the landing call write it too.
        (3, "/tmp/w/log_11.txt"): ("multi_writer", 3, [1, 3], "background_untracked", 1),
        # No record of a call's process: the write time decides, as without the recorder.
        (4, "/app/result.bin"): ("foreground", 4, None, "foreground", 4),
        (4, "/app/cache.db"): ("foreground", 4, [], "foreground", 4),
    }
    assert attributions(store)[(2, "/tmp/w/log_11.txt")] == ("recorded", 1, 1, True)
    assert attributions(store)[(3, "/tmp/w/log_11.txt")] == ("multi_writer", None, 3, False)


def test_a_record_of_the_landing_call_is_foreground_by_record(tmp_path: Path) -> None:
    store = make_store(
        tmp_path,
        [call(1, "touch /app/x", seconds=2)],
        [change(1, "/app/x", before=None, mtime=at(1, 1))],
        writes=[written(1, "/app/x", 1, at(1, 1))],
    )
    assert recorded(store) == {(1, "/app/x"): ("recorded", 1, [1], "foreground", 1)}
    assert attributions(store) == {(1, "/app/x"): ("recorded", 1, 1, False)}


def test_a_record_is_its_own_trials(tmp_path: Path) -> None:
    steps = [call(1, "nohup x > /app/x &"), call(2, "ls")]
    store = make_store(
        tmp_path,
        [*steps, *(s.model_copy(update={"trial_id": OTHER}) for s in steps)],
        [
            change(2, "/app/x", mtime=at(2, 0.5)),
            change(2, "/app/x", mtime=at(2, 0.5), trial_id=OTHER),
        ],
        trials=[trial(), trial(OTHER)],
        writes=[written(2, "/app/x", 1, at(2, 0.5), trial_id=OTHER)],
    )
    assert attributions(store) == {(2, "/app/x"): ("background_untracked", 1, 1, True)}
    assert attributions(store, OTHER) == {(2, "/app/x"): ("recorded", 1, 1, True)}
