import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import assemble_job_dir
from trajlab.capture import harbor_runner
from trajlab.capture.harbor_runner import RunPlan, RunRefusedError, execute, plan_run
from trajlab.contracts import CorpusManifest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clean git repo holding one committed job config, configs/harbor/hello.json."""
    repo = tmp_path / "repo"
    config = repo / "configs" / "harbor" / "hello.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "job_name": "hello-run",
                "jobs_dir": str(tmp_path / "jobs"),
                "agents": [{"name": "claude-code", "model_name": "anthropic/claude-sonnet-5"}],
                "tasks": [{"name": "hello-world/hello-world", "ref": "latest"}],
            }
        )
    )
    (repo / ".env").write_text("CLAUDE_FORCE_OAUTH=1\n")
    (repo / ".gitignore").write_text(".env\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


def _plan(repo: Path, **overrides: object) -> RunPlan:
    kwargs: dict = {
        "repo_dir": repo,
        "corpus_id": None,
        "manifests_dir": repo / "corpus" / "manifests",
        "env_file": repo / ".env",
        "allow_dirty": False,
    }
    kwargs.update(overrides)
    config = kwargs.pop("config_path", repo / "configs" / "harbor" / "hello.json")
    return plan_run(config, **kwargs)


def test_plan_run_builds_harbor_command(repo: Path, tmp_path: Path) -> None:
    plan = _plan(repo)
    assert plan.corpus_id == "hello"
    assert plan.config_repo_path == "configs/harbor/hello.json"
    assert plan.job_dir == tmp_path / "jobs" / "hello-run"
    assert plan.manifest_path == repo / "corpus" / "manifests" / "hello.json"
    assert plan.repo.sha == _git(repo, "rev-parse", "HEAD")
    assert plan.command[1:] == [
        "run",
        "--config",
        str(repo / "configs/harbor/hello.json"),
        "--env-file",
        str(repo / ".env"),
    ]
    assert Path(plan.command[0]).name == "harbor"


def test_plan_run_refuses_existing_job_dir(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "jobs" / "hello-run").mkdir(parents=True)
    with pytest.raises(RunRefusedError, match="new job_name"):
        _plan(repo)


def test_plan_run_refuses_taken_corpus_id(repo: Path) -> None:
    taken = repo / "corpus" / "manifests" / "hello.json"
    taken.parent.mkdir(parents=True)
    taken.write_text("{}")
    with pytest.raises(RunRefusedError, match="is taken"):
        _plan(repo)
    # The untracked manifest dirties the tree, which is not what this test is about.
    assert _plan(repo, corpus_id="hello-2", allow_dirty=True).corpus_id == "hello-2"


def test_plan_run_refuses_dirty_tree_unless_allowed(repo: Path) -> None:
    (repo / "untracked.txt").write_text("x")
    with pytest.raises(RunRefusedError, match="uncommitted changes"):
        _plan(repo)
    assert _plan(repo, allow_dirty=True).repo.dirty


def test_plan_run_refuses_missing_env_file(repo: Path) -> None:
    with pytest.raises(RunRefusedError, match="not found"):
        _plan(repo, env_file=repo / "missing.env")
    assert "--env-file" not in _plan(repo, env_file=None).command


def test_plan_run_refuses_config_outside_repo(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "hello.json"
    outside.write_text((repo / "configs" / "harbor" / "hello.json").read_text())
    with pytest.raises(RunRefusedError, match="not inside the repo"):
        _plan(repo, config_path=outside)


def test_plan_run_refuses_unloadable_config(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(RunRefusedError, match="cannot load job config"):
        _plan(repo, config_path=tmp_path / "missing.json")
    bad = repo / "configs" / "harbor" / "bad.json"
    bad.write_text('{"n_attempts": 0}')
    with pytest.raises(RunRefusedError, match="cannot load job config"):
        _plan(repo, config_path=bad)


def test_plan_run_refuses_outside_a_git_repo(repo: Path, tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(RunRefusedError, match="rev-parse"):
        _plan(repo, repo_dir=not_a_repo)


def test_execute_writes_manifest_after_harbor_succeeds(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repo)

    def fake_harbor(command: list[str]) -> int:
        assemble_job_dir(plan.job_dir.parent, plan.job_dir.name)
        return 0

    monkeypatch.setattr(harbor_runner, "run_harbor", fake_harbor)
    assert execute(plan, storage="local") == 0
    manifest = CorpusManifest.model_validate_json(plan.manifest_path.read_text())
    assert manifest.corpus_id == "hello"
    assert manifest.config_path == "configs/harbor/hello.json"
    assert manifest.repo_sha == plan.repo.sha
    assert not manifest.repo_dirty
    assert manifest.storage == "local"
    assert [job.job_name for job in manifest.jobs] == ["hello-run"]


def test_execute_skips_manifest_when_harbor_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repo)
    monkeypatch.setattr(harbor_runner, "run_harbor", lambda command: 3)
    assert execute(plan, storage=None) == 3
    assert not plan.manifest_path.exists()


def test_execute_fails_when_job_dir_is_unusable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(repo)
    monkeypatch.setattr(harbor_runner, "run_harbor", lambda command: 0)
    assert execute(plan, storage=None) == 1
    assert not plan.manifest_path.exists()
