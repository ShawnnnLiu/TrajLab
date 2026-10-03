import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import assemble_job_dir
from trajlab.capture import harbor_runner
from trajlab.capture.harbor_runner import RunPlan, RunRefusedError, execute, plan_run
from trajlab.capture.pins import CLAUDE_CODE_VERSION
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
                "agents": [
                    {
                        "name": "claude-code",
                        "model_name": "anthropic/claude-sonnet-5",
                        "kwargs": {"version": CLAUDE_CODE_VERSION},
                    }
                ],
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
        "watcher_running": lambda jobs_dir: False,
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


def _enable_hooks(repo: Path, settings: object) -> None:
    config = repo / "configs" / "harbor" / "hello.json"
    data = json.loads(config.read_text())
    data["agents"][0]["kwargs"]["config"] = settings
    config.write_text(json.dumps(data))
    _git(repo, "commit", "-q", "-am", "hooks")


HOOKS = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": []}]}}


@pytest.mark.parametrize("inline", [True, False], ids=["inline", "file"])
def test_plan_run_refuses_hooks_without_watcher(
    repo: Path, tmp_path: Path, inline: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)  # Harbor resolves a settings path against its cwd, which is ours
    if inline:
        _enable_hooks(repo, HOOKS)
    else:
        (repo / "settings.json").write_text(json.dumps(HOOKS))
        _git(repo, "add", "settings.json")
        _enable_hooks(repo, "settings.json")
    asked: list[Path] = []

    def running(jobs_dir: Path) -> bool:
        asked.append(jobs_dir)
        return False

    with pytest.raises(RunRefusedError, match="trajlab watch"):
        _plan(repo, watcher_running=running)
    assert asked == [tmp_path / "jobs"]
    assert _plan(repo, watcher_running=lambda jobs_dir: True).corpus_id == "hello"


def test_plan_run_ignores_settings_without_hooks(repo: Path) -> None:
    _enable_hooks(repo, {"model": "x"})
    assert _plan(repo).corpus_id == "hello"


def test_plan_run_refuses_unreadable_settings(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    _enable_hooks(repo, "missing.json")
    with pytest.raises(RunRefusedError, match="cannot read agent settings"):
        _plan(repo)


def _set_agent(repo: Path, agent: dict) -> None:
    config = repo / "configs" / "harbor" / "hello.json"
    data = json.loads(config.read_text())
    data["agents"] = [agent]
    config.write_text(json.dumps(data))
    _git(repo, "commit", "-q", "-am", "agent")


@pytest.mark.parametrize(
    "agent",
    [
        {"name": "claude-code"},
        {"name": "claude-code", "kwargs": {"version": "2.1.285"}},
        {"name": "claude-code", "kwargs": {"version": "latest"}},
        {
            "import_path": "harbor.agents.installed.claude_code:ClaudeCode",
            "kwargs": {"version": "2.1.0"},
        },
    ],
    ids=["unpinned", "other-version", "latest", "subclass-other-version"],
)
def test_plan_run_refuses_claude_code_off_the_pin(repo: Path, agent: dict) -> None:
    _set_agent(repo, agent)
    with pytest.raises(RunRefusedError, match=f"pins Claude Code {CLAUDE_CODE_VERSION}"):
        _plan(repo)


def test_plan_run_leaves_other_agents_alone(repo: Path) -> None:
    _set_agent(repo, {"name": "oracle"})
    assert _plan(repo).corpus_id == "hello"


def test_plan_run_refuses_unimportable_agent(repo: Path) -> None:
    _set_agent(repo, {"import_path": "no.such.module:Agent"})
    with pytest.raises(RunRefusedError, match="cannot import agent"):
        _plan(repo)


def test_committed_job_configs_use_the_pin() -> None:
    from harbor.models.job.config import JobConfig

    from trajlab.capture.harbor_runner import check_version_pins

    configs = sorted((Path(__file__).parent.parent / "configs" / "harbor").glob("*.json"))
    assert configs
    for path in configs:
        check_version_pins(JobConfig.model_validate_json(path.read_text()))


def test_config_behind_a_symlink_counts_as_inside_the_repo(repo: Path, tmp_path: Path) -> None:
    # corpus/jobs links to shared storage outside the repo; repair configs live under it.
    shared = tmp_path / "shared"
    shared.mkdir()
    (repo / "corpus").mkdir()
    (repo / "corpus" / "jobs").symlink_to(shared)
    (repo / ".gitignore").write_text(".env\ncorpus/jobs\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore jobs")
    config = repo / "corpus" / "jobs" / "_repair-inputs" / "x" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text((repo / "configs" / "harbor" / "hello.json").read_text())
    plan = plan_run(
        config,
        repo_dir=repo,
        corpus_id="x",
        manifests_dir=tmp_path / "manifests",
        env_file=None,
        allow_dirty=False,
        watcher_running=lambda _: True,
    )
    assert plan.config_repo_path == "corpus/jobs/_repair-inputs/x/config.json"
