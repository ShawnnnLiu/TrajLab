"""The retry prompt (ADR-0016 item 1, ADR-0022, ADR-0023): control keeps round 2's steps, the
two record arms get one text but for how the record is reached, every query the text gives runs
and every file it names is in the bundle, each prompt says FAILED, and none names a check or
quotes the verifier."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from harbor.agents.installed.claude_code import ClaudeCode

from tests.test_store_build import PROMPT, all_images, capture
from trajlab.capture.repair import REPAIR_PROTOCOL, RepairClaudeCode
from trajlab.contracts import EXP2_ARMS, Exp2Arm
from trajlab.exp2 import leaks
from trajlab.exp2.prompt import (
    CLAIMS,
    EARLIER_WORK_CLAUSE,
    ENGINE_ACCESS,
    ENGINE_DESCRIPTIONS,
    FILES_ACCESS,
    FILES_DESCRIPTIONS,
    GUIDE,
    GUIDE_REST,
    OUTPUT_CAP,
    PAGE_CHARS,
    PROTOCOL_STEPS,
    QUERY_COMMAND,
    QUESTIONS,
    RECORD_INTRO,
    RECORD_STEPS,
    TABLE_GUIDE,
    PromptError,
    answer,
    prefix,
    protocol_steps,
    record_text,
    render,
    sha256,
)
from trajlab.store import schema
from trajlab.store.build import build_trial
from trajlab.store.describe import _descriptions, columns, relations
from trajlab.store.export import export
from trajlab.store.reconcile import hunk_versions

# hello-world's tests: the two checks the fixture's verifier ran (verifier/ctrf.json).
TASK_TESTS = """\
from pathlib import Path


def test_hello_file_exists():
    assert Path("/app/hello.txt").exists()


def test_hello_file_contents():
    assert Path("/app/hello.txt").read_text().strip() == "Hello, world!"
"""
RECORD_ARMS: tuple[Exp2Arm, ...] = ("record-files", "record-engine")


@pytest.fixture
def chain_store(trial_copy: Path, tmp_path: Path) -> Path:
    """A chain's store holding its root failure: the fixture trial."""
    capture(trial_copy)
    store = tmp_path / "store"
    build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "work")
    return store


@pytest.fixture
def needles(trial_copy: Path, tmp_path: Path) -> leaks.Needles:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_state.py").write_text(TASK_TESTS)
    return leaks.needles(trial_copy, tests)


def rendered(k: int = 1) -> dict[Exp2Arm, str]:
    return {arm: render(prefix(arm, k), PROMPT) for arm in EXP2_ARMS}


def note_of(k: int) -> str:
    return (
        f"{k} previous attempt(s) at this task FAILED the task's tests. The tests are not "
        "visible to you and may check cases beyond the examples in the environment."
    )


def test_the_record_arms_differ_only_in_how_the_record_is_reached() -> None:
    own = {
        "record-engine": (ENGINE_ACCESS, ENGINE_DESCRIPTIONS),
        "record-files": (FILES_ACCESS, FILES_DESCRIPTIONS),
    }
    skeletons = set()
    for arm in RECORD_ARMS:
        text = record_text(arm)
        access, descriptions = own[arm]
        for shared in (RECORD_INTRO, RECORD_STEPS, CLAIMS, TABLE_GUIDE):
            assert text.count(shared) == 1
        for question in QUESTIONS:
            assert text.count(f"{question.text}:\n{answer(arm, question)}") == 1
        skeleton = text.replace(access, "<access>").replace(descriptions, "<descriptions>")
        for question in QUESTIONS:
            skeleton = skeleton.replace(answer(arm, question), "<answer>")
        skeletons.add(skeleton)
    assert len(skeletons) == 1
    assert FILES_ACCESS not in record_text("record-engine")
    assert ENGINE_ACCESS not in record_text("record-files")
    assert QUERY_COMMAND not in record_text("record-files")


def test_the_prompt_is_note_arm_text_instruction() -> None:
    for k in (1, 2, 3):
        prompts = rendered(k)
        assert prompts["control"] == "\n\n".join([note_of(k), PROTOCOL_STEPS, PROMPT])
        for arm in RECORD_ARMS:
            assert prompts[arm] == "\n\n".join([note_of(k), record_text(arm), PROMPT])
        assert len(set(prompts.values())) == 3
    with pytest.raises(PromptError):
        record_text("control")


def test_the_guide_names_real_relations_and_columns(chain_store: Path) -> None:
    with schema.connect_agent(chain_store) as con:
        for entry in GUIDE:
            found = {name for name, _ in columns(con, entry.table)}
            named = {column.split(" = ")[0] for column in entry.columns}
            assert named <= found, (entry.table, named - found)
    rest = set(GUIDE_REST.split(":")[0].split(", "))
    assert {entry.table for entry in GUIDE} | rest == set(relations())


def test_every_query_the_engine_is_given_runs(chain_store: Path) -> None:
    with schema.connect_agent(chain_store) as con:
        for question in QUESTIONS:
            for sql in question.queries:
                con.execute(sql).fetchall()  # as written: no placeholder to fill in
        (sql,) = re.findall(r'"(SELECT [^"]*)"', ENGINE_DESCRIPTIONS)
        rows = con.execute(sql).fetchall()
        assert rows and all(comment for _, _, _, comment in rows)
        _, diffs = QUESTIONS[2].queries
        rows = con.execute(diffs).fetchall()
        (length,) = con.execute("SELECT max(length(hunk)) FROM deltas").fetchone() or (0,)
        # The paging the access paragraph gives reads a hunk back whole.
        pages = [
            con.execute(
                f"SELECT substr(hunk, {start}, {PAGE_CHARS}) FROM deltas WHERE hunk IS NOT NULL "
                "ORDER BY length(hunk) DESC LIMIT 1"
            ).fetchone()[0]
            for start in range(1, length + 1, PAGE_CHARS)
        ]
        (longest,) = con.execute(
            "SELECT hunk FROM deltas WHERE hunk IS NOT NULL ORDER BY length(hunk) DESC LIMIT 1"
        ).fetchone()
    assert "".join(pages) == longest
    # The file question's diffs hold each file whole, as that step left it.
    assert rows
    assert hunk_versions(rows[-1][-1])[1] == "Hello, world!\n"


def test_the_diagnosis_reads_the_files_and_no_given_query_cuts_a_hunk() -> None:
    (last,) = re.findall(r"Answer questions 1 to (\d) below", RECORD_STEPS)
    (versions,) = re.findall(r"failed attempts' versions \(question (\d)\)", RECORD_STEPS)
    files = QUESTIONS[int(versions) - 1]
    assert files.text.startswith("What each attempt wrote into its files")
    assert int(versions) <= int(last)  # the diagnosis answers the file question
    queries = [sql for question in QUESTIONS for sql in question.queries]
    assert "d.hunk FROM deltas" in files.queries[-1]
    assert not any(re.search(r"(left|substr)\([^)]*hunk", sql) for sql in queries)
    # At least as wide as the cuts agents chose for themselves on 2026-11-03.
    cuts = [
        int(n) for sql in queries for n in re.findall(r"left\([^()]*(?:\([^()]*\))?, (\d+)\)", sql)
    ]
    assert cuts and min(cuts) >= 300
    assert all(
        int(n) >= 600 for sql in queries for n in re.findall(r"left\(s\.output, (\d+)\)", sql)
    )
    for access in (ENGINE_ACCESS, FILES_ACCESS):
        assert OUTPUT_CAP in access
    # The diagnosis is due before any file changes, as in control, not before the first command
    # (a record-files agent's first read of its record is a command in the environment).
    assert "Before you change any file, write them to /logs/agent/diagnosis.md" in RECORD_STEPS
    # Record-files agents are pointed at the `.diff` files first, with the command that lists them.
    assert files.files.startswith("each change's diff is its own file")
    assert "find /logs/agent/record/deltas -name '*.diff'" in files.files


def test_the_store_carries_every_description_the_readme_has(chain_store: Path) -> None:
    with schema.connect_agent(chain_store) as con:
        tables = dict(
            con.execute(
                "SELECT table_name, comment FROM duckdb_tables() UNION ALL "
                "SELECT view_name, comment FROM duckdb_views() WHERE NOT internal"
            ).fetchall()
        )
        notes = {
            (table, column): comment
            for table, column, comment in con.execute(
                "SELECT table_name, column_name, comment FROM duckdb_columns() WHERE NOT internal"
            ).fetchall()
        }
    for name in relations():
        description, column_notes = _descriptions(name)
        assert tables[name] == description
        for column, note in column_notes.items():
            assert notes[(name, column)] == note


def test_every_file_the_files_arm_is_pointed_to_is_in_the_bundle(
    chain_store: Path, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    export(chain_store, bundle)
    text = record_text("record-files")
    for name in set(re.findall(r"[a-z_]+\.jsonl", text)) - {"<table>.jsonl"}:
        assert (bundle / name).is_file(), name
    for entry in GUIDE:
        assert (bundle / f"{entry.table}.jsonl").is_file()
    assert (bundle / "README.md").is_file()
    assert (bundle / "deltas").is_dir() and not (bundle / "versions").exists()
    assert "deltas/<trial_id>/<step_idx>/<path>.diff" in text
    assert "versions/" not in text and "text_after" not in record_text("record-engine")


def test_every_arm_says_failed_and_leaks_nothing(
    needles: leaks.Needles, fixture_trial: Path
) -> None:
    assert {"test_hello_file_exists", "test_hello_file_contents"} <= needles.check_names
    assert needles.verifier_lines  # the fixture's test-stdout.txt
    for k in (1, 2, 3):
        for text in rendered(k).values():
            assert "FAILED" in text
            assert leaks.hits(text, needles) == []
            assert leaks.leaks(text, PROMPT, needles) == []
    stdout = (fixture_trial / "verifier" / "test-stdout.txt").read_text()
    for text in rendered().values():
        assert not any(line.strip() and line.strip() in text for line in stdout.splitlines()[1:])


def test_a_check_name_or_verifier_line_in_the_prompt_is_a_leak(needles: leaks.Needles) -> None:
    text = rendered()["record-engine"]
    named = text.replace("FAILED the task's tests.", "FAILED test_hello_file_contents.")
    assert leaks.leaks(named, PROMPT, needles) == ["check name test_hello_file_contents"]
    line = sorted(needles.verifier_lines)[0]
    assert leaks.leaks(text + "\n" + line, PROMPT, needles) == [f"verifier text {line!r}"]
    # Part of a longer identifier is not the check's name.
    assert leaks.hits("see test_hello_file_contents_v2", needles) == []


def test_a_needle_the_instruction_holds_itself_is_not_a_leak(needles: leaks.Needles) -> None:
    instruction = PROMPT + "Your file must pass test_hello_file_exists.\n"
    text = render(prefix("control", 1), instruction)
    assert leaks.hits(text, needles) == ["check name test_hello_file_exists"]
    assert leaks.leaks(text, instruction, needles) == []


def test_needles_come_from_the_task_tests_and_the_ctrf(trial_copy: Path, tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_more.py").write_text(
        "class TestShape:\n    async def test_param_case(self):\n        pass\n"
    )
    found = leaks.needles(trial_copy, tests)
    assert {"TestShape", "test_param_case", "test_hello_file_exists"} <= found.check_names


def test_no_check_names_is_refused_not_clean(
    trial_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harbor.models.task.id.PACKAGE_CACHE_DIR", tmp_path / "empty-cache")
    (trial_copy / "verifier" / "ctrf.json").unlink()
    with pytest.raises(leaks.LeakCheckError, match="tests cannot be read"):
        leaks.needles(trial_copy, tests_dir=None)


def test_a_graded_task_has_verifier_text_and_no_check_names(
    trial_copy: Path, tmp_path: Path
) -> None:
    verifier = trial_copy / "verifier"
    (verifier / "ctrf.json").unlink()
    reason = "reference_base.FCStd vs answer_base.FCStd: overall=0.237"
    (verifier / "reward_details.json").write_text(
        json.dumps({"score": 0.0, "base": {"geometry_similarity_reason": reason}})
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text("#!/bin/bash\npython3 /tests/run_scorer.py\n")
    found = leaks.needles(trial_copy, tests)
    assert found.check_names == frozenset()
    assert reason in found.verifier_lines
    assert leaks.leaks(f"close: {reason}", PROMPT, found) == [f"verifier text {reason!r}"]


def test_the_tests_dir_is_harbors_package_cache(
    trial_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "packages"
    monkeypatch.setattr("harbor.models.task.id.PACKAGE_CACHE_DIR", cache)
    digest = "38d7a077f07fbee8efc78db5dec9a72f82e727510ad1dcfeac0b55fa845256b7"  # lock.json
    assert leaks.task_tests_dir(trial_copy) is None
    tests = cache / "hello-world" / "hello-world" / digest / "tests"
    tests.mkdir(parents=True)
    assert leaks.task_tests_dir(trial_copy) == tests


def test_the_steps_are_round_2s_without_the_earlier_work_clause() -> None:
    assert EARLIER_WORK_CLAUSE in REPAIR_PROTOCOL
    assert EARLIER_WORK_CLAUSE not in PROTOCOL_STEPS
    assert PROTOCOL_STEPS.startswith("Work in this order:\n1. Diagnose.")
    assert "(the files in the environment), work out" in PROTOCOL_STEPS
    assert PROTOCOL_STEPS.endswith(
        "If you would have to say a requirement is untested, test it instead."
    )
    assert REPAIR_PROTOCOL.endswith(
        PROTOCOL_STEPS.replace("environment)", "environment" + EARLIER_WORK_CLAUSE + ")")
    )
    with pytest.raises(PromptError):
        protocol_steps(REPAIR_PROTOCOL.replace(EARLIER_WORK_CLAUSE, ""))


def test_control_prefix_verbatim() -> None:
    assert prefix("control", 1) == (
        "1 previous attempt(s) at this task FAILED the task's tests. The tests are not visible "
        "to you and may check cases beyond the examples in the environment.\n\n"
        "Work in this order:\n"
        "1. Diagnose. From whatever evidence you have (the files in the environment), work out "
        "the most likely reasons the previous attempt failed. Before changing any other file, "
        "write them to /logs/agent/diagnosis.md: each suspected cause, the evidence for it, and "
        "how you will check it.\n"
        "2. List every requirement in the task, each with a check you can run that would fail "
        "if the requirement were not met. Include inputs other than the ones provided.\n"
        "3. Fix, run the checks, and repeat until they all pass.\n"
        "4. Before finishing, run every check again. If you would have to say a requirement is "
        "untested, test it instead."
    )


def test_k_is_at_least_one() -> None:
    with pytest.raises(PromptError):
        prefix("control", 0)


def test_render_is_what_repair_claude_code_pipes_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def harbor_run(
        self: ClaudeCode, instruction: str, environment: Any, context: Any
    ) -> None:
        seen.append(instruction)

    monkeypatch.setattr(ClaudeCode, "run", harbor_run)
    for arm in EXP2_ARMS:
        note = prefix(arm, 1)
        agent = RepairClaudeCode(
            logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5", repair_note=note
        )
        asyncio.run(agent.run(PROMPT, environment=None, context=None))  # type: ignore[arg-type]
        assert seen[-1] == render(note, PROMPT)
        assert sha256(seen[-1]) == sha256(render(note, PROMPT))


def test_the_prefix_hash_depends_only_on_arm_and_k() -> None:
    hashes = {sha256(prefix(arm, k)) for arm in EXP2_ARMS for k in (1, 2, 3)}
    assert len(hashes) == 9
    assert sha256(prefix("record-engine", 2)) == sha256(prefix("record-engine", 2))
