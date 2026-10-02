"""A Harbor Docker environment that runs each trial on a derived image with the agent installed.

ADR-0008. Harbor installs Claude Code into the running trial container, so the install sits in
the container's writable layer and every `docker commit` checkpoint re-captures it. This
environment builds or pulls the task image exactly as Harbor would, runs Harbor's own
`ClaudeCode.install` once against a temporary container of it, commits that container as a
cached derived image, and starts the trial from the derived image through Harbor's prebuilt
path. Harbor's install then finds the pinned version already present and does nothing.

Select it in a job config with
`"environment": {"import_path": "trajlab.capture.preinstall:PreinstalledDockerEnvironment"}`.
"""

import asyncio
import hashlib
import json
import logging
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, ClassVar, cast, override

import yaml
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.definition import should_use_prebuilt_docker_image
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.agent.name import AgentName
from harbor.models.task.config import TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.trial.config import TrialConfig
from harbor.utils.import_path import import_symbol

from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.contracts import PREINSTALL_RECORD_FILENAME, PreinstallRecord

logger = logging.getLogger(__name__)

REPOSITORY = "trajlab-preinstalled"
# Bump when the way derived images are made changes; it is part of every derived tag.
# 2: derived images carry the agent binary's sha256 (ADR-0009).
RECIPE_VERSION = 2
SHA256_LABEL = "trajlab.preinstall.agent_sha256"
# Run as the agent user, with the PATH Harbor's own version check uses.
AGENT_SHA256_COMMAND = (
    'export PATH="$HOME/.local/bin:$PATH"; '
    'sha256sum "$(readlink -f "$(command -v claude)")" | cut -d " " -f 1'
)
# Harbor's compose files run the main service with this command (docker-compose-*.yaml).
KEEPALIVE_COMMAND = ("sh", "-c", "sleep infinity")
INSTALL_EXEC_TIMEOUT_S = 900.0
MAIN_SERVICE = "main"
_TAG_INVALID = re.compile(r"[^A-Za-z0-9_.-]")


class PreinstallError(RuntimeError):
    """The derived image cannot be made for this trial; the trial does not start."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], float | None], Awaitable[CommandResult]]


async def run_command(args: list[str], timeout: float | None) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise PreinstallError(f"{' '.join(args[:3])} timed out after {timeout} s") from error
    return CommandResult(
        process.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


class Docker:
    """Async `docker` CLI calls; the runner is swappable for tests."""

    def __init__(self, runner: Runner = run_command, binary: str = "docker") -> None:
        self.runner = runner
        self.binary = binary

    async def run(self, *args: str, timeout: float | None = None) -> CommandResult:
        return await self.runner([self.binary, *args], timeout)

    async def __call__(self, *args: str, timeout: float | None = None) -> str:
        result = await self.run(*args, timeout=timeout)
        if result.returncode != 0:
            raise PreinstallError(f"docker {' '.join(args[:2])}: {result.stderr.strip()}")
        return result.stdout.strip()

    async def image_id(self, image: str) -> str | None:
        result = await self.run("image", "inspect", "--format", "{{.Id}}", image)
        return result.stdout.strip() if result.returncode == 0 else None

    async def content_key(self, image: str) -> str:
        """A hash of the image's filesystem layers and runtime config.

        Not the image id: BuildKit attaches provenance with a build timestamp, so rebuilding an
        unchanged Dockerfile yields a new id over identical layers (checked 2026-09-30).
        """
        content = await self(
            "image", "inspect", "--format", "{{json .RootFS.Layers}}{{json .Config}}", image
        )
        return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class AgentSpec:
    agent_class: type[ClaudeCode]
    name: str
    version: str


def agent_spec(config: TrialConfig) -> AgentSpec:
    """The agent a trial will run, which must be Claude Code at a pinned version."""
    agent = config.agent
    if agent.import_path:
        agent_class = import_symbol(agent.import_path)
    elif agent.name == AgentName.CLAUDE_CODE.value:
        agent_class = ClaudeCode
    else:
        raise PreinstallError(f"pre-install supports Claude Code only, not agent {agent.name!r}")
    if not (isinstance(agent_class, type) and issubclass(agent_class, ClaudeCode)):
        raise PreinstallError(f"{agent.import_path} is not a subclass of Harbor's ClaudeCode")
    version = agent.kwargs.get("version")
    if not isinstance(version, str) or not version:
        raise PreinstallError(
            "pre-install needs a pinned agent version: set agents[0].kwargs.version"
        )
    if version != CLAUDE_CODE_VERSION:
        raise PreinstallError(
            f"agent version {version!r} is not the pinned Claude Code {CLAUDE_CODE_VERSION} "
            "(trajlab.capture.pins, ADR-0009)"
        )
    return AgentSpec(agent_class=agent_class, name=agent_class.name(), version=version)


def derived_image(task_image: str, task_content_key: str, spec: AgentSpec, harbor: str) -> str:
    """`trajlab-preinstalled:<readable stem>-<hash>`; the hash names everything that matters."""
    key = json.dumps([task_content_key, spec.name, spec.version, harbor, RECIPE_VERSION])
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    stem = _TAG_INVALID.sub("_", task_image.rsplit("/", 1)[-1])
    suffix = f"-{spec.name}-{spec.version}-{digest}"
    stem = stem[: 128 - len(suffix)]
    if not stem or not (stem[0].isalnum() or stem[0] == "_"):
        stem = f"_{stem}"[: 128 - len(suffix)]
    return f"{REPOSITORY}:{_TAG_INVALID.sub('_', stem + suffix)}"


def main_service_override(compose_files: list[Path]) -> str | None:
    """The first task compose file that gives `main` its own image or build, if any.

    Compose would then build or pull that instead of the derived image, or tag a task build
    with the derived image's name.
    """
    for path in compose_files:
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        main = (data.get("services") or {}).get(MAIN_SERVICE) or {}
        if "build" in main or "image" in main:
            return str(path)
    return None


class ContainerExec:
    """The slice of Harbor's environment interface that `ClaudeCode.install` uses: `exec`.

    Runs commands the way Harbor's Docker environment does: `bash -c`, in the task's working
    directory, as the task's agent user unless a user is given, with the task's environment.
    """

    def __init__(
        self,
        docker: Docker,
        container: str,
        *,
        default_user: str | int | None,
        workdir: str | None,
        env: Mapping[str, str],
    ) -> None:
        self.docker = docker
        self.container = container
        self.default_user = default_user
        self.workdir = workdir
        self.env = dict(env)

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        args = ["exec"]
        if cwd or self.workdir:
            args += ["-w", cwd or self.workdir or ""]
        for key, value in (self.env | (env or {})).items():
            args += ["-e", f"{key}={value}"]
        user = user if user is not None else self.default_user
        if user is not None:
            args += ["-u", str(user)]
        args += [self.container, "bash", "-c", command]
        result = await self.docker.run(*args, timeout=timeout_sec or INSTALL_EXEC_TIMEOUT_S)
        return ExecResult(stdout=result.stdout, stderr=result.stderr, return_code=result.returncode)


class PreinstalledDockerEnvironment(DockerEnvironment):
    """Harbor's Docker environment, started from a derived image holding the agent (ADR-0008)."""

    docker: ClassVar[Docker] = Docker()
    _agent_user: str | int | None = None  # the task's agent user, set by prepare_image
    # Harbor runs a job's trials as tasks in one event loop; one build or pull per image.
    # Keyed by loop as well, since an asyncio.Lock belongs to the loop that first uses it.
    _locks: ClassVar[dict[tuple[int, str], asyncio.Lock]] = {}

    @classmethod
    def _lock(cls, name: str) -> asyncio.Lock:
        key = (id(asyncio.get_running_loop()), name)
        return cls._locks.setdefault(key, asyncio.Lock())

    def is_agent_environment(self) -> bool:
        """False for a task's separate verifier environment, which Harbor builds from `tests/`.

        Harbor creates that environment from the trial's own environment config, so it is an
        instance of this class too; it must start exactly as Harbor would start it.
        """
        task_dir = self.environment_dir.parent
        return self.environment_dir.resolve() == TaskPaths(task_dir).environment_dir.resolve()

    @override
    async def start(self, force_build: bool) -> None:
        if not self.is_agent_environment():
            await DockerEnvironment.start(self, force_build=force_build)
            return
        record = await self.prepare_image(force_build)
        # A copy: Harbor's verifier environment reads the task's own config object.
        self.task_env_config = self.task_env_config.model_copy(
            update={"docker_image": record.image}
        )
        self._env_vars.prebuilt_image_name = record.image
        (self.trial_paths.trial_dir / PREINSTALL_RECORD_FILENAME).write_text(
            record.model_dump_json(indent=2) + "\n"
        )
        await super().start(force_build=False)
        await self.verify_trial_agent(record)

    async def verify_trial_agent(self, record: PreinstallRecord) -> None:
        """Check the trial container runs exactly the pinned agent binary (ADR-0009).

        Harbor would otherwise reinstall silently on a version mismatch, putting the install
        back into the writable layer and the agent off the pin.
        """
        user = self._agent_user
        result = await self.exec(ClaudeCode._INSTALL_VERSION_COMMAND, user=user)
        match = re.search(r"\d+\.\d+\.\d+", result.stdout or "")
        found = match.group(0) if match else None
        if result.return_code != 0 or found != record.agent_version:
            raise PreinstallError(
                f"trial container reports Claude Code {found!r} "
                f"(exit {result.return_code}); expected {record.agent_version}"
            )
        result = await self.exec(AGENT_SHA256_COMMAND, user=user)
        digest = (result.stdout or "").strip()
        if result.return_code != 0 or digest != record.agent_sha256:
            raise PreinstallError(
                f"trial container's Claude Code binary has sha256 {digest!r}; "
                f"the derived image recorded {record.agent_sha256}"
            )

    async def prepare_image(self, force_build: bool) -> PreinstallRecord:
        """Make sure the derived image exists, building it if needed. Starts no trial container."""
        if self._is_windows_container:
            raise PreinstallError("pre-install supports Linux containers only")
        compose_files = [self._environment_docker_compose_path, *self.extra_docker_compose_paths]
        if conflict := main_service_override(compose_files):
            raise PreinstallError(
                f"{conflict} sets image or build on the main service; pre-install cannot "
                "replace the main image for this task"
            )
        spec = agent_spec(TrialConfig.model_validate_json(self.trial_paths.config_path.read_text()))
        task_config = TaskConfig.model_validate_toml(
            TaskPaths(self.environment_dir.parent).config_path.read_text()
        )
        self._agent_user = task_config.agent.user
        harbor = package_version("harbor")
        task_image = await self._task_image(force_build)
        task_image_id = await self.docker.image_id(task_image)
        if task_image_id is None:
            raise PreinstallError(f"task image {task_image} is not available locally")
        task_content_key = await self.docker.content_key(task_image)
        image = derived_image(task_image, task_content_key, spec, harbor)
        async with self._lock(image):
            image_id = await self.docker.image_id(image)
            build_seconds = None
            if image_id is None:
                start = time.monotonic()
                image_id = await self._build_derived(
                    task_image, image, spec, user=task_config.agent.user, harbor=harbor
                )
                build_seconds = round(time.monotonic() - start, 1)
                logger.info("built %s from %s in %.0f s", image, task_image, build_seconds)
            agent_sha256 = await self.docker(
                "image",
                "inspect",
                "--format",
                f'{{{{index .Config.Labels "{SHA256_LABEL}"}}}}',
                image,
            )
            if not re.fullmatch(r"[0-9a-f]{64}", agent_sha256):
                raise PreinstallError(f"{image} has no valid {SHA256_LABEL} label")
        return PreinstallRecord(
            task_image=task_image,
            task_image_id=task_image_id,
            task_content_key=task_content_key,
            image=image,
            image_id=image_id,
            agent_name=spec.name,
            agent_version=spec.version,
            agent_sha256=agent_sha256,
            harbor_version=harbor,
            recipe_version=RECIPE_VERSION,
            cache_hit=build_seconds is None,
            build_seconds=build_seconds,
            recorded_at=datetime.now(UTC),
        )

    async def _task_image(self, force_build: bool) -> str:
        """The task image, pulled or built as Harbor's Docker environment would."""
        docker_image = self.task_env_config.docker_image
        timeout = self.task_env_config.build_timeout_sec
        if should_use_prebuilt_docker_image(
            self.environment_dir, docker_image=docker_image, force_build=force_build
        ):
            assert docker_image is not None
            async with self._lock(docker_image):
                if await self.docker.image_id(docker_image) is None:
                    await self.docker("pull", docker_image, timeout=timeout)
            return docker_image
        tag = self._main_image_name
        async with self._lock(tag):
            await self.docker("build", "-t", tag, str(self.environment_dir), timeout=timeout)
        return tag

    async def _build_derived(
        self,
        task_image: str,
        image: str,
        spec: AgentSpec,
        *,
        user: str | int | None,
        harbor: str,
    ) -> str:
        container = await self.docker(
            "run", "-d", "--label", "trajlab.preinstall.builder=1", task_image, *KEEPALIVE_COMMAND
        )
        try:
            shim = ContainerExec(
                self.docker,
                container,
                default_user=user,
                workdir=self.task_env_config.workdir,
                env=self._compose_task_env,
            )
            environment = cast(BaseEnvironment, shim)
            # What Harbor's InstalledAgent.setup does before install().
            result = await shim.exec(
                "[ -d /installed-agent ] || mkdir -p /installed-agent", user="root"
            )
            if result.return_code != 0:
                raise PreinstallError(f"cannot create /installed-agent: {result.stderr}")
            with tempfile.TemporaryDirectory(prefix="trajlab-preinstall-") as logs_dir:
                agent = spec.agent_class(logs_dir=Path(logs_dir), version=spec.version)
                try:
                    await agent.install(environment)
                except Exception as error:
                    raise PreinstallError(
                        f"Harbor's {spec.name} {spec.version} install failed on {task_image}: "
                        f"{error}"
                    ) from error
                # The exact test Harbor's install() runs at trial time to decide to skip.
                if not await agent._installed_claude_satisfies_version(environment):
                    raise PreinstallError(
                        f"{spec.name} {spec.version} installed but Harbor would not detect it"
                    )
            result = await shim.exec(AGENT_SHA256_COMMAND)
            agent_sha256 = (result.stdout or "").strip()
            if result.return_code != 0 or not re.fullmatch(r"[0-9a-f]{64}", agent_sha256):
                raise PreinstallError(f"cannot hash the installed agent binary: {result.stderr}")
            changes = await self._commit_changes(task_image, spec, harbor, agent_sha256)
            return await self.docker("commit", *changes, container, image)
        finally:
            await self.docker.run("rm", "--force", container)

    async def _commit_changes(
        self, task_image: str, spec: AgentSpec, harbor: str, agent_sha256: str
    ) -> list[str]:
        """Keep the task image's CMD (the builder ran a keepalive) and label the provenance."""
        cmd = await self.docker("image", "inspect", "--format", "{{json .Config.Cmd}}", task_image)
        labels: dict[str, Any] = {
            "trajlab.preinstall.task_image": task_image,
            "trajlab.preinstall.agent": spec.name,
            "trajlab.preinstall.agent_version": spec.version,
            "trajlab.preinstall.harbor_version": harbor,
            "trajlab.preinstall.recipe_version": str(RECIPE_VERSION),
            SHA256_LABEL: agent_sha256,
        }
        changes = [f"--change=CMD {cmd if cmd != 'null' else '[]'}"]
        changes += [f"--change=LABEL {key}={json.dumps(value)}" for key, value in labels.items()]
        return changes
