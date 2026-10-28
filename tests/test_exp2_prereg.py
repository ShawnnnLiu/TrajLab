"""The preregistration (technical spec §5.3): the sample, the file's round trip, the drift check,
the committed-at-HEAD check, and the `trajlab exp2` commands that read it."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.test_exp2_loop import make_corpus, make_settings
from tests.test_store_build import TRIAL_ID
from trajlab.cli import app
from trajlab.exp2 import leaks, prereg
from trajlab.exp2.prompt import prefix, sha256
from trajlab.store import schema


@pytest.fixture
def corpus(tmp_path: Path) -> dict[str, Path]:
    return make_corpus(tmp_path)


def test_the_sample_is_every_failure_with_a_complete_chain(corpus: dict[str, Path]) -> None:
    usable, excluded = prereg.usable_failures(corpus["store"])
    assert [(r.trial_name, r.trial_id) for r in usable] == [("hello-world__K3GBok3", TRIAL_ID)]
    assert excluded == []
    with schema.connect(schema.heldout_path(corpus["store"]), read_only=False) as con:
        con.execute("UPDATE chains SET complete = false, reasons = ['1 hooked call timed out']")
    usable, excluded = prereg.usable_failures(corpus["store"])
    assert usable == []
    assert [(e.trial_name, e.reason) for e in excluded] == [
        ("hello-world__K3GBok3", "1 hooked call timed out")
    ]


def test_a_named_trial_must_be_usable(corpus: dict[str, Path]) -> None:
    with pytest.raises(prereg.PreregError, match="not usable failures: nope__1"):
        prereg.settings(
            run="r",
            kind="dry-run",
            corpus_job=corpus["job"],
            corpus_store=corpus["store"],
            corpus_config=corpus["config"],
            trials=["nope__1"],
            budget=1,
            arms=["control"],
            test="none",
        )


def test_the_settings_fix_every_prompt_prefix(corpus: dict[str, Path]) -> None:
    found = make_settings(corpus, budget=3)
    text = prereg.schema_text(corpus["store"])
    for arm in found.arms:
        for k in (1, 2, 3):
            assert found.prefix_sha256[f"{arm}/{k}"] == sha256(prefix(arm, k, text))
    assert found.stop_hook is None and found.query is not None and found.query.query_cap == 200
    assert found.agent_kwargs == {"reasoning_effort": "medium", "version": "2.1.278"}
    assert found.sample[0].instruction_sha256 == sha256(
        'Create a file called hello.txt with "Hello, world!" as the content.\n'
    )


def test_the_file_round_trips(corpus: dict[str, Path], tmp_path: Path) -> None:
    found = make_settings(corpus)
    path = tmp_path / "preregistration.md"
    path.write_text(prereg.render_markdown(found))
    assert prereg.parse(path) == found
    text = path.read_text()
    assert "## Sample" in text and "`hello-world__K3GBok3`" in text
    assert found.prefix_sha256["record-engine/2"] in text


def test_drift_is_empty_until_the_code_changes(
    corpus: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    found = make_settings(corpus)
    assert prereg.drift(found) == []
    monkeypatch.setattr(prereg, "PROTOCOL_STEPS", "Work in this order:\n1. Something else.")
    problems = prereg.drift(found)
    assert any(p.startswith("protocol_sha256:") for p in problems)


def test_drift_catches_a_changed_instruction(corpus: dict[str, Path]) -> None:
    found = make_settings(corpus)
    with schema.connect(schema.record_path(corpus["store"]), read_only=False) as con:
        con.execute("UPDATE trials SET prompt = 'another task'")
    assert prereg.drift(found) == [
        f"hello-world__K3GBok3: not a usable failure of {corpus['store']} with this instruction"
    ]


def test_every_prompt_is_checked_for_leaks(corpus: dict[str, Path]) -> None:
    found = make_settings(corpus)
    clean = leaks.Needles(frozenset({"test_hello_file_exists"}), frozenset())
    assert prereg.prompt_leaks(found, lambda _: clean) == {}
    dirty = leaks.Needles(frozenset({"Diagnose"}), frozenset())
    assert prereg.prompt_leaks(found, lambda _: dirty) == {
        "hello-world__K3GBok3": ["check name Diagnose"]
    }


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_committed_is_the_head_version_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    path = repo / "docs" / "preregistration.md"
    path.parent.mkdir()
    path.write_text("one\n")
    assert prereg.committed(path) is None
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Preregister")
    commit = prereg.committed(path)
    assert commit is not None and len(commit) == 40
    path.write_text("two\n")
    assert prereg.committed(path) is None
    assert prereg.committed(tmp_path / "outside.md") is None


def test_the_prereg_command_writes_a_clean_file(
    corpus: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harbor.models.task.id.PACKAGE_CACHE_DIR", tmp_path / "cache")
    out = tmp_path / "p.md"
    args = [
        "exp2", "prereg", "--run", "t-exp2", "--test", "none (dry run)", "--dry-run",
        "--corpus-job", str(corpus["job"]), "--corpus-store", str(corpus["store"]),
        "--corpus-config", str(corpus["config"]), "--budget", "1", "--out", str(out),
    ]  # fmt: skip
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "3 prompts grep-clean" in result.output
    found = prereg.parse(out)
    assert (found.kind, found.budget, len(found.sample)) == ("dry-run", 1, 1)
    again = CliRunner().invoke(app, args)
    assert again.exit_code != 0 and "exists" in again.output


def test_run_refuses_an_uncommitted_preregistration_for_record_engine(
    corpus: dict[str, Path], tmp_path: Path
) -> None:
    path = tmp_path / "p.md"
    path.write_text(prereg.render_markdown(make_settings(corpus)))
    env = tmp_path / "env"
    env.write_text("")
    result = CliRunner().invoke(
        app,
        ["exp2", "run", str(path), "--jobs-dir", str(tmp_path / "jobs"), "--env-file", str(env)],
    )
    assert result.exit_code == 1
    assert "is not committed at HEAD; record-engine does not start" in result.output
    assert "no watcher holds" in result.output
    assert not (tmp_path / "jobs").exists()


def test_check_reports_what_is_wrong(corpus: dict[str, Path], tmp_path: Path) -> None:
    path = tmp_path / "p.md"
    found = make_settings(corpus)
    hashes = dict(found.prefix_sha256) | {"control/1": "0" * 64}
    path.write_text(prereg.render_markdown(found.model_copy(update={"prefix_sha256": hashes})))
    result = CliRunner().invoke(app, ["exp2", "check", str(path)])
    assert result.exit_code == 1
    assert "prefix_sha256" in result.output and "not the version committed" in result.output
    assert json.loads(json.dumps(hashes))  # the file kept what it was given
