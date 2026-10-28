"""The retry prompt (ADR-0016 item 1, ADR-0022): the three arms of one chain differ only in the
evidence sentence, each says FAILED, and none names a check or quotes the verifier."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from harbor.agents.installed.claude_code import ClaudeCode

from tests.test_store_build import PROMPT, all_images, capture
from trajlab.capture.repair import REPAIR_PROTOCOL, RepairClaudeCode
from trajlab.contracts import EXP2_ARMS, Exp2Arm
from trajlab.exp2 import leaks
from trajlab.exp2.prompt import (
    EARLIER_WORK_CLAUSE,
    PROTOCOL_STEPS,
    PromptError,
    evidence,
    prefix,
    protocol_steps,
    render,
    sha256,
)
from trajlab.store import schema
from trajlab.store.build import build_trial
from trajlab.store.describe import describe
from trajlab.store.export import export

# hello-world's tests: the two checks the fixture's verifier ran (verifier/ctrf.json).
TASK_TESTS = """\
from pathlib import Path


def test_hello_file_exists():
    assert Path("/app/hello.txt").exists()


def test_hello_file_contents():
    assert Path("/app/hello.txt").read_text().strip() == "Hello, world!"
"""


@pytest.fixture
def chain_store(trial_copy: Path, tmp_path: Path) -> Path:
    """A chain's store holding its root failure: the fixture trial."""
    capture(trial_copy)
    store = tmp_path / "store"
    build_trial(trial_copy, store, images=all_images(), work_dir=tmp_path / "work")
    return store


@pytest.fixture
def schema_text(chain_store: Path) -> str:
    with schema.connect_agent(chain_store) as con:
        return describe(con)


@pytest.fixture
def needles(trial_copy: Path, tmp_path: Path) -> leaks.Needles:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_state.py").write_text(TASK_TESTS)
    return leaks.needles(trial_copy, tests)


def rendered(schema_text: str, k: int = 1) -> dict[Exp2Arm, str]:
    return {arm: render(prefix(arm, k, schema_text), PROMPT) for arm in EXP2_ARMS}


def test_the_arms_differ_only_in_the_evidence_sentence(schema_text: str) -> None:
    for k in (1, 2, 3):
        prompts = rendered(schema_text, k)
        control = prompts["control"]
        for arm in ("record-files", "record-engine"):
            sentence = evidence(arm, schema_text)
            assert sentence is not None and sentence not in control
            assert prompts[arm].count(sentence) == 1
            assert prompts[arm].replace(sentence + "\n\n", "", 1) == control
        assert len(set(prompts.values())) == 3


def test_the_prompt_is_note_evidence_steps_instruction(schema_text: str) -> None:
    for arm, text in rendered(schema_text, 2).items():
        sentence = evidence(arm, schema_text)
        middle = [] if sentence is None else [sentence]
        parts = [
            "2 previous attempt(s) at this task FAILED the task's tests. The tests are not "
            "visible to you and may check cases beyond the examples in the environment.",
            *middle,
            PROTOCOL_STEPS,
            PROMPT,
        ]
        assert text == "\n\n".join(parts)


def test_every_arm_says_failed_and_leaks_nothing(
    schema_text: str, needles: leaks.Needles, fixture_trial: Path
) -> None:
    assert {"test_hello_file_exists", "test_hello_file_contents"} <= needles.check_names
    assert needles.verifier_lines  # the fixture's test-stdout.txt
    for k in (1, 2, 3):
        for text in rendered(schema_text, k).values():
            assert "FAILED" in text
            assert leaks.hits(text, needles) == []
            assert leaks.leaks(text, PROMPT, needles) == []
    stdout = (fixture_trial / "verifier" / "test-stdout.txt").read_text()
    for text in rendered(schema_text).values():
        assert not any(line.strip() and line.strip() in text for line in stdout.splitlines()[1:])


def test_a_check_name_or_verifier_line_in_the_prompt_is_a_leak(
    schema_text: str, needles: leaks.Needles
) -> None:
    text = rendered(schema_text)["record-engine"]
    named = text.replace("FAILED the task's tests.", "FAILED test_hello_file_contents.")
    assert leaks.leaks(named, PROMPT, needles) == ["check name test_hello_file_contents"]
    line = sorted(needles.verifier_lines)[0]
    assert leaks.leaks(text + "\n" + line, PROMPT, needles) == [f"verifier text {line!r}"]
    # Part of a longer identifier is not the check's name.
    assert leaks.hits("see test_hello_file_contents_v2", needles) == []


def test_a_needle_the_instruction_holds_itself_is_not_a_leak(
    schema_text: str, needles: leaks.Needles
) -> None:
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


def test_the_files_sentence_names_the_bundles_files(chain_store: Path, tmp_path: Path) -> None:
    sentence = evidence("record-files")
    assert sentence is not None
    bundle = tmp_path / "bundle"
    export(chain_store, bundle)
    listed = sentence.split("/logs/agent/record/: ", 1)[1].split(", and deltas/", 1)[0]
    assert listed.split(", ") == sorted(
        (path.name for path in bundle.glob("*.jsonl")), key=listed.split(", ").index
    )
    assert {name for name in listed.split(", ")} == {p.name for p in bundle.glob("*.jsonl")}
    assert (bundle / "deltas").is_dir() and (bundle / "README.md").is_file()


def test_the_engine_sentence_carries_the_readmes_schema_text(
    chain_store: Path, schema_text: str, tmp_path: Path
) -> None:
    sentence = evidence("record-engine", schema_text)
    assert sentence is not None
    assert '/logs/agent/bin/query "SELECT ...". It prints the rows as JSON lines.' in sentence
    bundle = tmp_path / "bundle"
    export(chain_store, bundle)
    assert sentence.endswith(schema_text.strip())
    assert (bundle / "README.md").read_text().strip().endswith(schema_text.strip())
    with pytest.raises(PromptError):
        evidence("record-engine")


def test_k_is_at_least_one() -> None:
    with pytest.raises(PromptError):
        prefix("control", 0)


def test_render_is_what_repair_claude_code_pipes_in(
    schema_text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def harbor_run(
        self: ClaudeCode, instruction: str, environment: Any, context: Any
    ) -> None:
        seen.append(instruction)

    monkeypatch.setattr(ClaudeCode, "run", harbor_run)
    for arm in EXP2_ARMS:
        note = prefix(arm, 1, schema_text)
        agent = RepairClaudeCode(
            logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5", repair_note=note
        )
        asyncio.run(agent.run(PROMPT, environment=None, context=None))  # type: ignore[arg-type]
        assert seen[-1] == render(note, PROMPT)
        assert sha256(seen[-1]) == sha256(render(note, PROMPT))


def test_the_prefix_hash_depends_only_on_arm_and_k(
    chain_store: Path, schema_text: str, tmp_path: Path
) -> None:
    other = Path(shutil.copytree(chain_store, tmp_path / "other"))
    with schema.connect_agent(other) as con:
        assert describe(con) == schema_text
    assert sha256(prefix("record-engine", 2, schema_text)) != sha256(
        prefix("record-engine", 3, schema_text)
    )
