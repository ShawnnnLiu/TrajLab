import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from harbor.environments.base import ExecResult
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import TaskConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import TrialPaths

from trajlab.capture import preinstall
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.preinstall import (
    REPOSITORY,
    AgentSpec,
    CommandResult,
    ContainerExec,
    Docker,
    PreinstalledDockerEnvironment,
    PreinstallError,
    agent_spec,
    derived_image,
    main_service_override,
)
from trajlab.contracts import PREINSTALL_RECORD_FILENAME, PreinstallRecord

VERSION = CLAUDE_CODE_VERSION
BINARY_SHA256 = "ab" * 32
TASK_IMAGE = "alexgshaw/regex-chess:20251031"


class FakeDocker:
    """A docker CLI over an in-memory image store; records every call."""

    def __init__(
        self, images: set[str] | None = None, installed: bool = True, install_ok: bool = True
    ) -> None:
        self.images = set(images or ())
        self.installed = installed  # what Harbor's version check finds after install
        self.install_ok = install_ok  # whether Harbor's install command succeeds
        self.calls: list[list[str]] = []
        self.commits = 0
        self.slow_commit = 0.0
        self.labels: dict[str, dict[str, str]] = {}

    async def __call__(self, args: list[str], timeout: float | None) -> CommandResult:
        assert args[0] == "docker"
        args = args[1:]
        self.calls.append(args)
        match args:
            case ["image", "inspect", "--format", "{{.Id}}", image]:
                if image in self.images:
                    return CommandResult(0, f"sha256:id-of-{image}\n", "")
                return CommandResult(1, "", f"No such image: {image}")
            case ["image", "inspect", "--format", fmt, image] if "RootFS" in fmt:
                return CommandResult(0, f'["layers-of-{image}"]{{"Cmd":["python3"]}}', "")
            case ["image", "inspect", "--format", fmt, image] if "index .Config.Labels" in fmt:
                key = fmt.split('"')[1]
                return CommandResult(0, self.labels.get(image, {}).get(key, "") + "\n", "")
            case ["image", "inspect", "--format", "{{json .Config.Cmd}}", _]:
                return CommandResult(0, '["python3"]\n', "")
            case ["pull", image]:
                self.images.add(image)
                return CommandResult(0, "", "")
            case ["build", "-t", tag, _]:
                self.images.add(tag)
                return CommandResult(0, "", "")
            case ["run", "-d", *_]:
                return CommandResult(0, "builder1\n", "")
            case ["exec", *rest]:
                command = rest[-1]
                if "sha256sum" in command:
                    return CommandResult(0, BINARY_SHA256 + "\n", "")
                if "bootstrap.sh" in command or "apt-get" in command:
                    if self.install_ok:
                        return CommandResult(0, f"{VERSION} (Claude Code)\n", "")
                    return CommandResult(100, "", "E: Unable to fetch some archives")
                if "claude --version" in command:
                    if self.installed:
                        return CommandResult(0, f"{VERSION} (Claude Code)\n", "")
                    return CommandResult(127, "", "claude: not found")
                return CommandResult(0, "", "")
            case ["commit", *changes, _, image]:
                await asyncio.sleep(self.slow_commit)
                self.commits += 1
                self.images.add(image)
                self.labels[image] = dict(
                    change.removeprefix("--change=LABEL ").split("=", 1)
                    for change in changes
                    if change.startswith("--change=LABEL ")
                )
                self.labels[image] = {k: json.loads(v) for k, v in self.labels[image].items()}
                return CommandResult(0, f"sha256:id-of-{image}\n", "")
            case ["rm", "--force", _]:
                return CommandResult(0, "", "")
        raise AssertionError(f"unexpected docker call {args}")

    def count(self, subcommand: str) -> int:
        return sum(call[0] == subcommand for call in self.calls)


def _task(tmp_path: Path, *, docker_image: str | None = TASK_IMAGE, compose: str | None = None):
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "environment" / "Dockerfile").write_text("FROM debian:bookworm\n")
    if compose is not None:
        (task / "environment" / "docker-compose.yaml").write_text(compose)
    image_line = f'docker_image = "{docker_image}"\n' if docker_image else ""
    (task / "task.toml").write_text(
        f'version = "1.0"\n[environment]\n{image_line}[agent]\nuser = "agent"\n'
    )
    return task


def _environment(
    tmp_path: Path,
    fake: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: dict[str, Any] | None = None,
    **task_kwargs: Any,
) -> PreinstalledDockerEnvironment:
    task = _task(tmp_path, **task_kwargs)
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    config = {
        "task": {"name": "terminal-bench/regex-chess", "ref": "latest"},
        "trial_name": "regex-chess__Ab12Cd3",
        "agent": agent or {"name": "claude-code", "kwargs": {"version": VERSION}},
    }
    paths.config_path.write_text(json.dumps(config))
    monkeypatch.setattr(PreinstalledDockerEnvironment, "docker", Docker(runner=fake))
    monkeypatch.setattr(PreinstalledDockerEnvironment, "_locks", {})
    task_config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
    return PreinstalledDockerEnvironment(
        environment_dir=task / "environment",
        environment_name="regex-chess",
        session_id="regex-chess__Ab12Cd3__env",
        trial_paths=paths,
        task_env_config=task_config.environment,
    )


def test_builds_derived_image_from_prebuilt_task_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    env = _environment(tmp_path, fake, monkeypatch)

    record = asyncio.run(env.prepare_image(force_build=False))

    assert record.task_image == TASK_IMAGE
    assert record.image.startswith(f"{REPOSITORY}:regex-chess_20251031-claude-code-{VERSION}-")
    assert record.image_id == f"sha256:id-of-{record.image}"
    assert not record.cache_hit
    assert record.build_seconds is not None
    assert (record.agent_name, record.agent_version) == ("claude-code", VERSION)
    assert fake.count("pull") == 1  # the task image was not local
    assert fake.count("build") == 0
    assert fake.commits == 1
    assert ["rm", "--force", "builder1"] in fake.calls
    run = next(call for call in fake.calls if call[0] == "run")
    assert run[-4:] == [TASK_IMAGE, "sh", "-c", "sleep infinity"]
    commit = next(call for call in fake.calls if call[0] == "commit")
    assert '--change=CMD ["python3"]' in commit
    assert f'--change=LABEL trajlab.preinstall.agent_version="{VERSION}"' in commit
    assert f'--change=LABEL trajlab.preinstall.agent_sha256="{BINARY_SHA256}"' in commit
    assert record.agent_sha256 == BINARY_SHA256
    # Harbor's setup step, then its install check, ran as the task's agent user or root.
    execs = [call for call in fake.calls if call[0] == "exec"]
    assert execs[0][execs[0].index("-u") + 1] == "root"
    assert all(call[-3:-1] == ["bash", "-c"] for call in execs)
    assert any(call[call.index("-u") + 1] == "agent" for call in execs if "-u" in call)


def test_reuses_cached_derived_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    first = asyncio.run(_environment(tmp_path / "a", fake, monkeypatch).prepare_image(False))
    second = asyncio.run(_environment(tmp_path / "b", fake, monkeypatch).prepare_image(False))

    assert second.cache_hit
    assert second.build_seconds is None
    assert second.image == first.image
    assert fake.commits == 1
    assert fake.count("pull") == 1


def test_concurrent_trials_build_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    fake.slow_commit = 0.05
    envs = [_environment(tmp_path / str(i), fake, monkeypatch) for i in range(3)]
    # _environment resets the lock table per call; share one table across the three.
    monkeypatch.setattr(PreinstalledDockerEnvironment, "_locks", {})

    async def run_all() -> list[PreinstallRecord]:
        return await asyncio.gather(*(env.prepare_image(False) for env in envs))

    records = asyncio.run(run_all())

    assert fake.commits == 1
    assert fake.count("pull") == 1
    assert sorted(r.cache_hit for r in records) == [False, True, True]


def test_builds_task_image_when_task_has_no_prebuilt_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    env = _environment(tmp_path, fake, monkeypatch, docker_image=None)

    record = asyncio.run(env.prepare_image(force_build=False))

    build = next(call for call in fake.calls if call[0] == "build")
    assert build == ["build", "-t", env._main_image_name, str(env.environment_dir)]
    assert record.task_image == env._main_image_name
    assert fake.count("pull") == 0


def test_force_build_builds_even_with_prebuilt_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    env = _environment(tmp_path, fake, monkeypatch)
    assert asyncio.run(env.prepare_image(force_build=True)).task_image == env._main_image_name


def test_cache_key_follows_content_not_image_id() -> None:
    spec = AgentSpec(agent_class=preinstall.ClaudeCode, name="claude-code", version=VERSION)
    base = derived_image(TASK_IMAGE, "sha256:content-a", spec, "0.23.0")

    assert base == derived_image(TASK_IMAGE, "sha256:content-a", spec, "0.23.0")
    assert base != derived_image(TASK_IMAGE, "sha256:content-b", spec, "0.23.0")
    assert base != derived_image(TASK_IMAGE, "sha256:content-a", spec, "0.24.0")
    other = AgentSpec(agent_class=preinstall.ClaudeCode, name="claude-code", version="2.1.300")
    assert base != derived_image(TASK_IMAGE, "sha256:content-a", other, "0.23.0")


@pytest.mark.parametrize(
    "task_image",
    ["hb__7140400b79079e55", "registry.io:5000/org/x:tag", "-dash/x", "y" * 300],
)
def test_derived_image_is_a_valid_reference(task_image: str) -> None:
    spec = AgentSpec(agent_class=preinstall.ClaudeCode, name="claude-code", version=VERSION)
    image = derived_image(task_image, "sha256:c", spec, "0.23.0")
    repository, tag = image.split(":", 1)
    assert repository == REPOSITORY
    assert len(tag) <= 128
    assert tag[0].isalnum() or tag[0] == "_"
    assert all(ch.isalnum() or ch in "_.-" for ch in tag)


def test_failed_install_fails_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker(installed=False, install_ok=False)
    env = _environment(tmp_path, fake, monkeypatch)

    with pytest.raises(PreinstallError, match="install failed on alexgshaw/regex-chess"):
        asyncio.run(env.prepare_image(force_build=False))

    assert fake.commits == 0
    assert ["rm", "--force", "builder1"] in fake.calls


def test_unverifiable_install_fails_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Harbor's install would then run apt-get etc.; make the check fail after it too.
    fake = FakeDocker(installed=False)
    env = _environment(tmp_path, fake, monkeypatch)

    with pytest.raises(PreinstallError, match="would not detect it"):
        asyncio.run(env.prepare_image(force_build=False))

    assert fake.commits == 0
    assert ["rm", "--force", "builder1"] in fake.calls


@pytest.mark.parametrize(
    ("agent", "message"),
    [
        ({"name": "claude-code"}, "pinned agent version"),
        ({"name": "claude-code", "kwargs": {"version": ""}}, "pinned agent version"),
        ({"name": "codex", "kwargs": {"version": "1"}}, "Claude Code only"),
        ({"name": "claude-code", "kwargs": {"version": "2.1.285"}}, "not the pinned"),
        ({"import_path": "pathlib:Path", "kwargs": {"version": "1"}}, "not a subclass"),
    ],
)
def test_agent_spec_refuses(agent: dict[str, Any], message: str) -> None:
    config = TrialConfig.model_validate(
        {"task": {"name": "org/t", "ref": "latest"}, "agent": agent}
    )
    with pytest.raises(PreinstallError, match=message):
        agent_spec(config)


def test_agent_spec_accepts_claude_code_subclass_by_import_path() -> None:
    config = TrialConfig.model_validate(
        {
            "task": {"name": "org/t", "ref": "latest"},
            "agent": {
                "import_path": "harbor.agents.installed.claude_code:ClaudeCode",
                "kwargs": {"version": VERSION},
            },
        }
    )
    spec = agent_spec(config)
    assert (spec.name, spec.version) == ("claude-code", VERSION)


@pytest.mark.parametrize(
    ("compose", "conflict"),
    [
        ("services:\n  main:\n    build: .\n", True),
        ("services:\n  main:\n    image: x\n", True),
        ("services:\n  main:\n    environment: [A=1]\n  db:\n    image: postgres\n", False),
        ("", False),
    ],
)
def test_task_compose_overriding_main_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compose: str, conflict: bool
) -> None:
    fake = FakeDocker()
    env = _environment(tmp_path, fake, monkeypatch, compose=compose)
    found = main_service_override([env._environment_docker_compose_path])
    assert (found is not None) == conflict
    if conflict:
        with pytest.raises(PreinstallError, match="main service"):
            asyncio.run(env.prepare_image(force_build=False))
        assert fake.calls == []  # refused before touching Docker


def test_container_exec_matches_harbor_exec_shape() -> None:
    calls: list[list[str]] = []

    async def runner(args: list[str], timeout: float | None) -> CommandResult:
        calls.append(args)
        return CommandResult(3, "out", "err")

    shim = ContainerExec(
        Docker(runner=runner), "c1", default_user="agent", workdir="/app", env={"A": "1"}
    )
    result = asyncio.run(shim.exec("echo hi", env={"B": "2"}))
    asyncio.run(shim.exec("id", user="root", cwd="/tmp"))

    assert (result.return_code, result.stdout, result.stderr) == (3, "out", "err")
    assert calls[0] == [
        "docker", "exec", "-w", "/app", "-e", "A=1", "-e", "B=2", "-u", "agent",
        "c1", "bash", "-c", "echo hi",
    ]  # fmt: skip
    assert calls[1][2:4] == ["-w", "/tmp"]
    assert calls[1][calls[1].index("-u") + 1] == "root"


def test_start_runs_trial_on_derived_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDocker()
    env = _environment(tmp_path, fake, monkeypatch)
    original_config = env.task_env_config
    seen: dict[str, Any] = {}

    async def harbor_start(self: DockerEnvironment, force_build: bool) -> None:
        seen.update(
            force_build=force_build,
            docker_image=self.task_env_config.docker_image,
            prebuilt=self._env_vars.prebuilt_image_name,
        )

    monkeypatch.setattr(DockerEnvironment, "start", harbor_start)
    execs = _fake_trial_exec(env, monkeypatch, stub_harbor_start=False)
    asyncio.run(env.start(force_build=True))
    # Both checks ran in the trial container, as the task's agent user.
    assert [user for _, user in execs] == ["agent", "agent"]

    record = PreinstallRecord.model_validate_json(
        (env.trial_paths.trial_dir / PREINSTALL_RECORD_FILENAME).read_text()
    )
    assert seen == {
        "force_build": False,
        "docker_image": record.image,
        "prebuilt": record.image,
    }
    # The task's own config object, which the verifier environment reads, is untouched.
    assert original_config.docker_image == TASK_IMAGE


def _fake_trial_exec(
    env: PreinstalledDockerEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = VERSION,
    sha256: str = BINARY_SHA256,
    stub_harbor_start: bool = True,
) -> list[tuple[str, Any]]:
    """Stand in for Harbor's exec into the trial container."""
    calls: list[tuple[str, Any]] = []

    async def exec_(command: str, user: Any = None, **_: Any) -> ExecResult:
        calls.append((command, user))
        if "sha256sum" in command:
            return ExecResult(stdout=sha256 + "\n", return_code=0)
        return ExecResult(stdout=f"{version} (Claude Code)\n", return_code=0)

    monkeypatch.setattr(env, "exec", exec_)
    if not stub_harbor_start:
        return calls

    async def harbor_start(self: DockerEnvironment, force_build: bool) -> None:
        return None

    monkeypatch.setattr(DockerEnvironment, "start", harbor_start)
    return calls


@pytest.mark.parametrize(
    ("version", "sha256", "message"),
    [
        ("2.1.300", BINARY_SHA256, "reports Claude Code '2.1.300'"),
        (VERSION, "cd" * 32, "binary has sha256"),
    ],
    ids=["version-drift", "binary-drift"],
)
def test_start_refuses_trial_container_off_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, sha256: str, message: str
) -> None:
    env = _environment(tmp_path, FakeDocker(), monkeypatch)
    _fake_trial_exec(env, monkeypatch, version=version, sha256=sha256)
    with pytest.raises(PreinstallError, match=message):
        asyncio.run(env.start(force_build=False))


def test_cached_image_without_hash_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    env = _environment(tmp_path, fake, monkeypatch)
    record = asyncio.run(env.prepare_image(force_build=False))
    fake.labels[record.image].pop("trajlab.preinstall.agent_sha256")

    with pytest.raises(PreinstallError, match="agent_sha256"):
        asyncio.run(_environment(tmp_path / "again", fake, monkeypatch).prepare_image(False))


def test_separate_verifier_environment_starts_as_harbor_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDocker()
    env = _environment(tmp_path, fake, monkeypatch)
    # Harbor builds a task's separate verifier environment from tests/, with the same class.
    tests_dir = env.environment_dir.parent / "tests"
    tests_dir.mkdir()
    env.environment_dir = tests_dir
    seen: dict[str, Any] = {}

    async def harbor_start(self: DockerEnvironment, force_build: bool) -> None:
        seen.update(force_build=force_build, docker_image=self.task_env_config.docker_image)

    monkeypatch.setattr(DockerEnvironment, "start", harbor_start)
    asyncio.run(env.start(force_build=False))

    assert seen == {"force_build": False, "docker_image": TASK_IMAGE}
    assert fake.calls == []
    assert not (env.trial_paths.trial_dir / PREINSTALL_RECORD_FILENAME).exists()
