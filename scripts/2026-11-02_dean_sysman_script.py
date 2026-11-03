#!/usr/bin/env python3
"""Run Terminal-Bench trials with harbor inside Waypoint sessions and checkpoint
each session after its trial, saving the container's filesystem and process memory.

Usage:

    /path/to/venv/bin/python run_trial.py trials.json [--output results.json]
    /path/to/venv/bin/python run_trial.py --check      # host check only

The input JSON lists the trials to run and the model for each:

    {
      "dataset": "terminal-bench/terminal-bench@4.0.0",
      "agent": "claude-code",
      "trials": [
        {"task": "roy-polymorph-cn", "model": "anthropic/claude-opus-5-5"}
      ]
    }

`dataset` and `agent` are optional defaults (shown above) and may also be set per
trial. Each trial needs `task` and `model`, and may set `api-key` (an Anthropic,
OpenRouter or OpenAI key for that trial, told apart by prefix; default: the
matching *_API_KEY variable from the environment; any other prefix is an error),
`context` (text placed in the agent's prompt before the task instruction; at most
one per trial), `context_files` (a list of local files and directories copied
into that trial's container before the agent starts, directories recursively;
neither is supported with terminus-2),
`endpoint-url` (the API endpoint to use; default: the provider's), `dockerfile_dir` (the
agent environment's Dockerfile directory, relative to the JSON file; default: the
task's own environment definition) and `restore_check_command` (run in a fork
restored from the checkpoint). Trials run one after another.

An optional top-level `passk` object turns the run into Pass@k:

    "passk": {"iterations": 3, "stopOnSuccess": true,
              "passContext": "Files from your previous attempt are in /context/previous.",
              "passContextFilesDest": "/context/previous"}

The input is run up to `iterations` times. From the second iteration on, each
trial gets the upper directory of its own previous checkpoint copied to
`passContextFilesDest` and `passContext` as its context. The copy leaves out what
that attempt was itself given there, unless `"passNested": true`, and an attempt
in which the agent never ran is not passed on. If the trial has an `analyzer`, its
answer about that attempt is appended to the context after "Analyzer output:".
With `stopOnSuccess`, a trial that scored 1.0 is not run again.

A trial may also set `analyzer` ({"agent", "model", "endpoint-url", "api-key",
"prompt"}): after the trial is checkpointed, that agent (claude-code or codex)
runs on this host, read-only, with the checkpoint folder as its working
directory and `prompt` as its task; its answer is stored as the trial's
`analysis`.

The output JSON holds the input under `input` (API keys redacted), the tool
versions and host under `environment`, and, per trial, the reward, cost, duration
and per-phase times, start/finish timestamps and the Waypoint checkpoint path.

This file plays two roles:

* Run as a script, it drives the workflow per trial: `harbor run` -> `waypoint
  checkpoint` -> restore check -> `waypoint suspend` -> secret scan.
* Imported by harbor (`--env run_trial:WaypointEnvironment`), it provides the
  harbor environment that runs the task container as a Waypoint session instead
  of a Docker container. That is what makes the trial state checkpointable.

Requirements: Linux, passwordless sudo (or run as root), an installed `waypoint`
(with CRIU and Buildah), an Anthropic API key for Claude Code trials, and a Python
environment with `harbor` installed; run the script with that Python.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import functools
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from harbor.environments.base import BaseEnvironment, ExecResult
except ImportError:  # pragma: no cover - reported to the user, nothing to recover
    sys.exit(
        "harbor is not importable from this Python interpreter.\n"
        "Install it (pip install harbor) and run this script with that Python."
    )

HERE = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = "/var/tmp/waypoint-harbor"
DEFAULT_DATASET = "terminal-bench/terminal-bench@4.0.0"
DEFAULT_AGENT = "claude-code"
DEFAULT_RESTORE_CHECK = "ls -la --color=never / /logs"
SESSION_FILE = "waypoint-session.json"

# Per-exec scratch space inside the fork. Removed after every command, so it is
# empty when the checkpoint is taken.
GUEST_SCRATCH = "/run/harbor-waypoint"


# --------------------------------------------------------------------------- #
# Shared helpers (used by both the harbor environment and the driver script)
# --------------------------------------------------------------------------- #


def root_prefix() -> list[str]:
    """Waypoint, CRIU and Buildah need root; go through sudo unless already root."""
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def waypoint_argv(state_dir: str | Path, waypoint_bin: str, *args: str) -> list[str]:
    """Build a waypoint command line.

    Every call, creation and cleanup alike, carries the same registry/session
    overrides, so the session is always found where it was created. Both
    directories live under `state_dir` on durable storage: Waypoint's defaults
    are under /tmp, which is commonly a tmpfs.
    """
    state = Path(state_dir)
    return [
        *root_prefix(),
        "env",
        f"WAYPOINT_SESSIONS_DIR={state / 'sessions'}",
        f"WAYPOINT_SESSION_INFO_DIR={state / 'sessions-info'}",
        waypoint_bin,
        *args,
    ]


def qualify_image(ref: str) -> str:
    """Make a Docker-style short image name explicit for Buildah (docker.io/...)."""
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return ref
    return f"docker.io/{ref}" if "/" in ref else f"docker.io/library/{ref}"


# --------------------------------------------------------------------------- #
# Harbor environment backed by a Waypoint session
# --------------------------------------------------------------------------- #


class WaypointEnvironment(BaseEnvironment):
    """Harbor environment whose "container" is the `main` fork of a Waypoint session.

    start()    `waypoint build <Dockerfile dir>`: Buildah builds the rootfs, Waypoint
               stages it as the session's read-only lower layer and starts the
               supervised shell (`main` fork) on a writable overlay.
    exec()     `waypoint exec <session> main -- 'bash <runner>'`.
    upload/    `waypoint cp` (directories travel as a tar archive so symlinks and
    download   host-side ownership behave).
    stop()     `waypoint cleanup`, except for the agent environment when
               keep_agent_session is set: that session is left running so the
               driver can checkpoint it after the trial.

    Harbor creates this class twice for tasks with a separate verifier: once for
    the agent environment (session id `<trial>__env`) and once for the verifier
    (`<trial>__verifier__...`). Only the agent environment is kept.

    Not provided: CPU/memory limits, network isolation, non-root exec users and
    Docker Compose. Waypoint shares host networking and is not a security boundary.

    Environment kwargs (harbor `--ek key=value`):
      state_dir           Parent of Waypoint's sessions/ and sessions-info/.
      waypoint_bin        Path to the waypoint binary.
      dockerfile_dir      Directory with the Dockerfile for the agent environment.
                          Default: the task's environment/ definition.
      keep_agent_session  Leave the agent session running in stop().
      prune_images        Remove the Buildah images a build created or pulled once
                          Waypoint has staged its own copy of the rootfs.
      context_files       [[host path, container path(, [excluded paths])], ...]
                          copied into the agent environment right after it starts.
                          A host directory is copied recursively, leaving out the
                          excluded paths (relative to it) and everything below them.
    """

    def __init__(
        self,
        *args,
        state_dir: str = DEFAULT_STATE_DIR,
        waypoint_bin: str | None = None,
        dockerfile_dir: str | None = None,
        keep_agent_session: bool = False,
        prune_images: bool = False,
        context_files: list | None = None,
        **kwargs,
    ):
        self._state_dir = Path(state_dir)
        self._waypoint_bin = waypoint_bin or shutil.which("waypoint") or "waypoint"
        self._dockerfile_dir = Path(dockerfile_dir) if dockerfile_dir else None
        self._keep_agent_session = bool(keep_agent_session)
        self._prune_images = bool(prune_images)
        self._context_files = [
            (str(item[0]), str(item[1]), [str(path) for path in (item[2] if len(item) > 2 else [])])
            for item in context_files or []
        ]
        self._wp_session: str | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "waypoint"

    @property
    def _is_agent_env(self) -> bool:
        return "__verifier__" not in self.session_id

    def _validate_definition(self):
        if self._is_agent_env and self._dockerfile_dir is not None:
            if not (self._dockerfile_dir / "Dockerfile").is_file():
                raise FileNotFoundError(f"No Dockerfile in {self._dockerfile_dir}")
            return
        if self.task_env_config.docker_image:
            return
        if not (self.environment_dir / "Dockerfile").is_file():
            raise FileNotFoundError(
                f"{self.environment_dir} has no Dockerfile and the task declares no "
                "docker_image; the waypoint environment needs one of them."
            )

    # -- subprocess plumbing ------------------------------------------------ #

    async def _run(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        check: bool = True,
        kill_marker: str | None = None,
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except BaseException:
            # Timeout or trial cancellation. The client runs as root under sudo,
            # so this user cannot signal it directly. Terminating the client
            # makes the fork's shell supervisor interrupt the running command.
            if proc.returncode is None:
                if os.geteuid() == 0 or kill_marker is None:
                    try:
                        proc.terminate()
                    except (ProcessLookupError, PermissionError):
                        pass
                else:
                    killer = await asyncio.create_subprocess_exec(
                        *root_prefix(), "pkill", "-TERM", "-f", kill_marker,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
            raise
        text = out.decode(errors="replace")
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv[-4:])} failed with exit code {proc.returncode}: "
                f"{text.strip()[-2000:]}"
            )
        return proc.returncode or 0, text

    async def _wp(self, *args: str, **kwargs) -> tuple[int, str]:
        return await self._run(
            waypoint_argv(self._state_dir, self._waypoint_bin, *args), **kwargs
        )

    async def _image_ids(self) -> set[str]:
        _, out = await self._run(
            [*root_prefix(), "buildah", "images", "--all", "--quiet", "--no-trunc"]
        )
        return set(out.split())

    # -- lifecycle ---------------------------------------------------------- #

    def _build_context(self) -> tuple[Path, Path | None]:
        """Return (Dockerfile directory, temporary directory to remove afterwards)."""
        if self._is_agent_env and self._dockerfile_dir is not None:
            return self._dockerfile_dir, None
        if self.task_env_config.docker_image:
            # Prebuilt task image: a one-line Dockerfile lets `waypoint build`
            # pull it and stage its rootfs.
            tmp = Path(tempfile.mkdtemp(prefix="hbwp-image-"))
            image = qualify_image(self.task_env_config.docker_image)
            (tmp / "Dockerfile").write_text(f"FROM {image}\n")
            return tmp, tmp
        return self.environment_dir, None

    async def start(self, force_build: bool) -> None:
        context, tmp = self._build_context()
        before = await self._image_ids() if self._prune_images else set()
        try:
            _, out = await self._wp("build", str(context), "--quiet")
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        # --quiet prints "<session id>,<work dir>,<bash pid>" as the last line.
        self._wp_session = out.strip().splitlines()[-1].split(",")[0].strip()
        self.logger.info(
            f"Waypoint session {self._wp_session} started for {self.session_id}"
        )

        if self._is_agent_env:
            record = {
                "session_id": self._wp_session,
                "fork_id": "main",
                "harbor_session_id": self.session_id,
                "state_dir": str(self._state_dir),
                "waypoint_bin": self._waypoint_bin,
                "dockerfile_dir": str(context) if tmp is None else None,
                "docker_image": self.task_env_config.docker_image,
            }
            path = self.trial_paths.trial_dir / SESSION_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2) + "\n")

        if self._prune_images:
            # Waypoint copied the rootfs into the session's original/ directory,
            # so the image store copy is redundant from here on.
            created = sorted(await self._image_ids() - before)
            if created:
                await self._run(
                    [*root_prefix(), "buildah", "rmi", "--force", *created],
                    check=False,
                )

        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()
        if self._is_agent_env:
            # Before the agent is installed or prompted, so the files are simply
            # part of the container it starts in. The verifier never gets them.
            for source, destination, exclude in self._context_files:
                if Path(source).is_dir():
                    # The whole tree, recursively; its contents land in
                    # `destination`, merged with anything already there.
                    await self.upload_dir(source, destination, exclude=exclude)
                else:
                    await self.upload_file(source, destination)

    async def stop(self, delete: bool):
        if self._wp_session is None:
            return
        if self._is_agent_env and self._keep_agent_session:
            self.logger.info(
                f"Leaving Waypoint session {self._wp_session} running for checkpointing"
            )
            return
        if not delete:
            return
        code, out = await self._wp("cleanup", self._wp_session, check=False)
        if code != 0:
            self.logger.warning(f"waypoint cleanup failed, forcing: {out.strip()}")
            await self._wp("cleanup", self._wp_session, "--force")
        self._wp_session = None

    # -- exec --------------------------------------------------------------- #

    def _session(self) -> str:
        if self._wp_session is None:
            raise RuntimeError("Waypoint environment is not started")
        return self._wp_session

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        """Run `command` with bash in the fork, like `docker exec ... bash -c`.

        The command, its environment and a small runner are copied into the fork
        and only `bash <runner>` is sent to the fork's persistent shell. Secrets
        such as the API key therefore never appear on a host command line or in
        the shell's history, and the environment file is deleted before the
        command starts. Each command runs in a child bash, so `cd`/`export` do not
        leak into the persistent shell. stdout and stderr go to files rather than
        the PTY, which keeps them separate and outside the PTY's 16 MiB cap.
        """
        session = self._session()
        user = self._resolve_user(user)
        if user not in (None, "root", 0, "0"):
            raise NotImplementedError(
                f"waypoint environment only runs commands as root (requested: {user!r})"
            )
        merged_env = self._merge_env(env) or {}
        cwd = cwd or self.task_env_config.workdir
        exec_id = uuid.uuid4().hex
        guest = f"{GUEST_SCRATCH}/{exec_id}"

        runner = ["#!/bin/bash", "{"]
        if merged_env:
            runner.append(f'  . {guest}/env; rm -f {guest}/env')
        if cwd:
            runner.append(f"  cd {shlex.quote(cwd)} || exit 125")
        runner += [f"  bash {guest}/cmd", f"}} </dev/null >{guest}/out 2>{guest}/err", ""]

        with tempfile.TemporaryDirectory(prefix="hbwp-") as tmp:
            stage = Path(tmp) / "in"
            stage.mkdir()
            (stage / "cmd").write_text(command)
            (stage / "run.sh").write_text("\n".join(runner))
            if merged_env:
                env_path = stage / "env"
                env_path.touch(mode=0o600)
                env_path.write_text(
                    "".join(
                        f"export {key}={shlex.quote(str(value))}\n"
                        for key, value in merged_env.items()
                        if key.isidentifier()
                    )
                )
            await self._wp("cp", session, str(stage), f"main:{guest}")
            try:
                try:
                    code, _ = await self._wp(
                        "exec", session, "main", "--", f"bash {guest}/run.sh",
                        timeout=timeout_sec,
                        check=False,
                        kill_marker=f"exec {session} main -- bash {guest}/run.sh",
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"Command timed out after {timeout_sec} seconds"
                    ) from None
                streams = {}
                for name in ("out", "err"):
                    host = Path(tmp) / name
                    await self._wp("cp", session, f"main:{guest}/{name}", str(host))
                    streams[name] = host.read_text(errors="replace")
            finally:
                await asyncio.shield(
                    self._wp(
                        "exec", session, "main", "--", f"rm -rf {guest}", check=False
                    )
                )
        return ExecResult(
            stdout=streams["out"], stderr=streams["err"], return_code=code
        )

    # -- file transfer ------------------------------------------------------ #

    async def _check(self, result: ExecResult, what: str) -> None:
        if result.return_code != 0:
            raise RuntimeError(f"{what} failed: {(result.stderr or '').strip()}")

    async def upload_file(self, source_path: Path | str, target_path: str):
        await self._wp("cp", self._session(), str(source_path), f"main:{target_path}")

    def _staging_dir(self) -> tempfile.TemporaryDirectory:
        """Temporary directory for file transfers, on the trial's own disk.

        The system temp directory is often a small RAM-backed tmpfs; a
        gigabyte-sized transfer staged there fails or eats memory.
        """
        parent = self.trial_paths.trial_dir
        parent.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=".hbwp-", dir=parent)

    async def upload_dir(
        self, source_dir: Path | str, target_dir: str, exclude: list[str] | None = None
    ):
        """Copy a host directory's contents into `target_dir` in the guest.

        `exclude` lists paths relative to `source_dir` that are left out together
        with everything below them.
        """
        guest_tar = f"{GUEST_SCRATCH}/up-{uuid.uuid4().hex}.tar"
        skipped: list[str] = []
        excluded = sorted({"./" + path.strip("/") for path in exclude or [] if path.strip("/")})

        def is_excluded(name: str) -> bool:
            return any(name == path or name.startswith(path + "/") for path in excluded)

        def ordinary_only(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
            if is_excluded(member.name):
                return None
            # Device nodes and pipes are not content, and one kind cannot be
            # recreated at all: OverlayFS records a deleted file as a 0,0
            # character device, which the kernel refuses to create in a fork.
            if member.isreg() or member.isdir() or member.issym() or member.islnk():
                return member
            skipped.append(member.name)
            return None

        with self._staging_dir() as tmp:
            host_tar = Path(tmp) / "up.tar"
            try:
                with tarfile.open(host_tar, "w") as tar:
                    tar.add(str(source_dir), arcname=".", filter=ordinary_only)
            except PermissionError:
                # A root-owned tree such as a checkpoint layer. Pack it as root
                # instead of loosening its permissions: a saved layer whose
                # modes change no longer restores.
                if not root_prefix():
                    raise
                host_tar.unlink(missing_ok=True)
                # List the tree as root, choose the members here, and let tar
                # pack exactly that list. Every argument is passed as argv.
                _, listing = await self._run(
                    [*root_prefix(), "find", str(source_dir), "-mindepth", "1",
                     "-printf", "%y %P\\0"]  # fmt: skip
                )
                members: list[str] = []
                for line in filter(None, listing.split("\0")):
                    kind, name = line[0], "./" + line[2:]
                    if is_excluded(name):
                        continue
                    (members if kind in "fdl" else skipped).append(name)
                member_list = Path(tmp) / "members"
                member_list.write_text("".join(f"{name}\0" for name in members))
                await self._run(
                    [*root_prefix(), "tar", "--null", "--no-recursion", "-C", str(source_dir),
                     "-T", str(member_list), "-cf", str(host_tar)]  # fmt: skip
                )
            if skipped:
                self.logger.warning(
                    f"Skipped {len(skipped)} special file(s) (devices, pipes, OverlayFS "
                    f"deletion markers) under {source_dir}: {', '.join(skipped[:5])}"
                    + (" ..." if len(skipped) > 5 else "")
                )
            await self._wp("cp", self._session(), str(host_tar), f"main:{guest_tar}")
        target = shlex.quote(target_dir)
        result = await self.exec(
            f"mkdir -p {target} && tar -xf {guest_tar} -C {target} --no-same-owner; "
            f"rc=$?; rm -f {guest_tar}; exit $rc",
            user="root",
        )
        await self._check(result, f"upload to {target_dir}")

    async def download_file(self, source_path: str, target_path: Path | str):
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._staging_dir() as tmp:
            # Copy through a temp file so the result is owned by this user, not root.
            host = Path(tmp) / "file"
            await self._wp("cp", self._session(), f"main:{source_path}", str(host))
            shutil.copyfile(host, target)
            shutil.copymode(host, target)

    async def download_dir(self, source_dir: str, target_dir: Path | str):
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        guest_tar = f"{GUEST_SCRATCH}/down-{uuid.uuid4().hex}.tar"
        result = await self.exec(
            f"mkdir -p {GUEST_SCRATCH} && "
            f"tar -cf {guest_tar} -C {shlex.quote(source_dir)} .",
            user="root",
        )
        try:
            await self._check(result, f"download of {source_dir}")
            with self._staging_dir() as tmp:
                host_tar = Path(tmp) / "down.tar"
                await self._wp(
                    "cp", self._session(), f"main:{guest_tar}", str(host_tar)
                )
                with tarfile.open(host_tar) as tar:
                    for member in tar:
                        try:
                            tar.extract(member, target, filter="data")
                        except tarfile.FilterError as exc:
                            # The archive comes from the guest; skip anything that
                            # would escape the target instead of trusting it.
                            self.logger.warning(f"Skipped {member.name}: {exc}")
        finally:
            await self.exec(f"rm -f {guest_tar}", user="root")


# --------------------------------------------------------------------------- #
# Driver script
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    print(f"[run_trial] {message}", flush=True)


ANTHROPIC_ENDPOINT = "https://api.anthropic.com"
OPENAI_ENDPOINT = "https://api.openai.com/v1"
# OpenRouter serves the Anthropic API (what Claude Code speaks) and an
# OpenAI-style API (what Codex and LiteLLM-based agents use) under different paths.
OPENROUTER_ANTHROPIC_ENDPOINT = "https://openrouter.ai/api"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
# Inherited endpoint overrides are dropped so a trial's endpoint depends only on
# its input.
ENDPOINT_ENV_VARS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE", "OPENROUTER_API_BASE", "OPENROUTER_BASE_URL",
    "OPENAI_BASE_URL", "OPENAI_API_BASE",
)  # fmt: skip

# Key prefix -> provider. OpenAI's legacy bare "sk-..." keys are not accepted:
# other providers use the same shape, so the prefix alone cannot identify them.
KEY_PREFIXES = {
    "sk-or-": "openrouter",
    "sk-ant-": "anthropic",
    "sk-proj-": "openai",
    "sk-svcacct-": "openai",
}
# Providers whose keys an agent can use; agents not listed take any of them.
AGENT_PROVIDERS = {
    "claude-code": ("anthropic", "openrouter"),
    "codex": ("openai", "openrouter"),
}
# Environment variables consulted, in order, when a trial has no "api-key".
PROVIDER_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def key_provider(api_key: str) -> str | None:
    """Which service issued `api_key`, judged by its prefix; None if unrecognized."""
    for prefix, provider in KEY_PREFIXES.items():
        if api_key.startswith(prefix):
            return provider
    return None


def require_usable_key(api_key: str, agent: str, where: str) -> None:
    """Exit unless the key's provider is recognized and `agent` can use it."""
    provider = key_provider(api_key)
    if provider is None:
        # Guessing a provider would send the key to a service it is not for.
        sys.exit(
            f"{where}: unrecognized API key; it must start with one of "
            + ", ".join(f'"{prefix}" ({name})' for prefix, name in KEY_PREFIXES.items())
        )
    allowed = AGENT_PROVIDERS.get(agent)
    if allowed and provider not in allowed:
        sys.exit(
            f"{where}: agent \"{agent}\" cannot use an {provider} key; "
            f"it needs one from: {', '.join(allowed)}"
        )


# Hosts whose service is known, so a key from another service is never sent there.
ENDPOINT_HOST_PROVIDERS = {
    "openrouter.ai": "openrouter",
    "api.anthropic.com": "anthropic",
    "api.openai.com": "openai",
}


def endpoint_provider(endpoint: str | None) -> str | None:
    """The service a known endpoint URL belongs to; None for other hosts."""
    if not endpoint:
        return None
    host = endpoint.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return ENDPOINT_HOST_PROVIDERS.get(host)


def env_fallback_key(agent: str, endpoint: str | None = None) -> tuple[str, str]:
    """(variable name, key) from the environment for a trial with no "api-key".

    With an endpoint on a known service, only that service's variable is used.
    """
    providers = AGENT_PROVIDERS.get(agent) or tuple(PROVIDER_ENV_KEYS)
    if (service := endpoint_provider(endpoint)) in providers:
        providers = (service,)
    for provider in providers:
        name = PROVIDER_ENV_KEYS[provider]
        if value := os.environ.get(name, "").strip():
            return name, value
    return " or ".join(PROVIDER_ENV_KEYS[provider] for provider in providers), ""


def api_env(
    api_key: str, agent: str, endpoint: str | None
) -> tuple[dict[str, str], list[str], str]:
    """Environment for `harbor run` that makes `agent` use `api_key`.

    `endpoint` is the trial's "endpoint-url", or None for the provider's default.
    Returns (variables to set, variables to remove, endpoint URL used). The key
    is only ever passed through the environment.
    """
    provider = key_provider(api_key)
    unset = list(ENDPOINT_ENV_VARS)
    if agent == "codex":
        # Codex speaks the OpenAI Responses API. Harbor hands it OPENAI_API_KEY
        # and writes OPENAI_BASE_URL into its config, whichever service that is.
        endpoint = endpoint or (
            OPENAI_ENDPOINT if provider == "openai" else OPENROUTER_ENDPOINT
        )
        env = {"OPENAI_API_KEY": api_key}
        if endpoint != OPENAI_ENDPOINT:
            env["OPENAI_BASE_URL"] = endpoint
        return env, unset, endpoint
    if provider == "openai":
        env = {"OPENAI_API_KEY": api_key}
        if endpoint and endpoint != OPENAI_ENDPOINT:
            env["OPENAI_BASE_URL"] = env["OPENAI_API_BASE"] = endpoint
        return env, unset, endpoint or OPENAI_ENDPOINT
    if provider == "anthropic":
        env = {"ANTHROPIC_API_KEY": api_key}
        if endpoint and endpoint != ANTHROPIC_ENDPOINT:
            env["ANTHROPIC_BASE_URL"] = endpoint
        return env, unset, endpoint or ANTHROPIC_ENDPOINT
    if agent == "claude-code":
        # An inherited Anthropic key would be sent to OpenRouter instead of this
        # one, so drop it.
        endpoint = endpoint or OPENROUTER_ANTHROPIC_ENDPOINT
        env = {
            "OPENROUTER_API_KEY": api_key,
            "ANTHROPIC_BASE_URL": endpoint,
            "ANTHROPIC_AUTH_TOKEN": api_key,
        }
        return env, [*unset, "ANTHROPIC_API_KEY"], endpoint
    # LiteLLM-based agents (terminus-2 and others) read these for
    # `openrouter/...` models.
    env = {"OPENROUTER_API_KEY": api_key}
    if endpoint and endpoint != OPENROUTER_ENDPOINT:
        env["OPENROUTER_API_BASE"] = env["OPENROUTER_BASE_URL"] = endpoint
    return env, unset, endpoint or OPENROUTER_ENDPOINT


OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


def openrouter_usage(api_key: str) -> float | None:
    """Total USD OpenRouter has billed to `api_key` so far; None if unavailable."""
    request = urllib.request.Request(
        OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return float(json.load(response)["data"]["usage"])
    except Exception as exc:  # reporting only; never fail a trial over it
        log(f"WARNING: could not read OpenRouter usage: {type(exc).__name__}")
        return None


def openrouter_billed(api_key: str, usage_before: float | None) -> float | None:
    """USD billed to `api_key` since `usage_before` was read.

    OpenRouter's total trails the calls by about a minute, so wait until it has
    not moved for 90 seconds (giving up after five minutes) before reading it.
    """
    if usage_before is None:
        return None
    log("waiting for OpenRouter's usage total to settle")
    usage, unchanged = openrouter_usage(api_key), 0
    for _ in range(20):
        if usage is None:
            return None
        if unchanged == 6:
            break
        time.sleep(15)
        latest = openrouter_usage(api_key)
        unchanged = unchanged + 1 if latest == usage else 0
        usage = latest
    return None if usage is None else round(usage - usage_before, 6)


def run_checked(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed ({proc.returncode}):\n{proc.stdout}{proc.stderr}"
        )
    return proc


def find_secret(paths: list[Path], secret: str) -> list[str]:
    """Return files under `paths` that contain `secret`.

    The secret is passed to grep on stdin, never on a command line.
    """
    proc = subprocess.run(
        [*root_prefix(), "grep", "-rlF", "-D", "skip", "-f", "-", "--", *map(str, paths)],
        input=secret + "\n",
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise RuntimeError(f"secret scan failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


# --------------------------------------------------------------------------- #
# Analyzer: an agent run on the host over a saved checkpoint
# --------------------------------------------------------------------------- #

ANALYZER_KEYS = {"agent", "model", "prompt", *("api-key", "api_key"),
                 *("endpoint-url", "endpoint_url")}  # fmt: skip
ANALYZER_AGENTS = ("claude-code", "codex")
ANALYZER_TIMEOUT_SEC = 1800
# Where setup.sh --with-codex installs Node.js and Codex for the analyzer.
ANALYZER_HOME = Path.home() / ".cache" / "wp-harbor" / "analyzer"
# Read-only tools only. A checkpoint whose files change no longer restores.
CLAUDE_ANALYZER_TOOLS = "Read,Grep,Glob"


def analyzer_binary(agent: str) -> tuple[str | None, list[str]]:
    """(path of the agent's CLI or None, extra PATH entries it needs)."""
    if agent == "claude-code":
        found = shutil.which("claude")
        local = Path.home() / ".local" / "bin" / "claude"
        return found or (str(local) if local.exists() else None), []
    found = shutil.which("codex")
    if found:
        return found, []
    local = ANALYZER_HOME / "codex" / "node_modules" / ".bin" / "codex"
    node_bins = sorted(ANALYZER_HOME.glob("node-v*/bin"))
    return (str(local) if local.exists() else None), [str(b) for b in node_bins[-1:]]


def analyzer_version(agent: str) -> str | None:
    """The analyzer CLI's version line, or None if it is not installed."""
    binary, extra_path = analyzer_binary(agent)
    if binary is None:
        return None
    path = os.pathsep.join([*extra_path, os.environ.get("PATH", "")])
    try:
        done = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": path},
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def codex_sandbox_problem() -> str | None:
    """None if Codex's sandbox can run commands on this host, else why not.

    The analyzer only runs Codex inside its read-only sandbox, which needs
    bubblewrap and unprivileged user namespaces.
    """
    binary, extra_path = analyzer_binary("codex")
    if binary is None:
        return "the codex CLI was not found"
    ANALYZER_HOME.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wp-analyzer-", dir=ANALYZER_HOME) as home:
        path = os.pathsep.join([*extra_path, os.environ.get("PATH", "")])
        done = subprocess.run(
            [binary, "sandbox", "--", "true"], capture_output=True, text=True,
            env={**os.environ, "PATH": path, "CODEX_HOME": home}, timeout=60,
        )  # fmt: skip
    if done.returncode == 0:
        return None
    lines = [line for line in done.stderr.splitlines() if line.strip() and "WARNING" not in line]
    return (lines[0] if lines else f"exit code {done.returncode}").strip()


def scrub(text: str, secret: str) -> str:
    return text.replace(secret, REDACTED) if secret else text


def run_analyzer(analyzer: dict, api_key: str, checkpoint_path: str, out_dir: Path) -> dict:
    """Run the analyzer agent with the checkpoint folder as its working directory.

    The agent gets `prompt` as its task, can only read (Claude Code: Read, Grep
    and Glob, confined to the working directory; Codex: its read-only sandbox),
    and runs as this user, not root, with a throwaway configuration directory.
    Returns the record stored under "analysis".
    """
    agent, model = analyzer["agent"], analyzer["model"]
    result: dict = {
        "agent": agent,
        "model": model,
        "endpoint_url": analyzer["endpoint_url"],
        "api_key_provider": key_provider(api_key),
        "cwd": checkpoint_path,
        "prompt": analyzer["prompt"],
        "started_at": utc_now(),
        "finished_at": None,
        "duration_sec": None,
        "exit_code": None,
        "output": None,
        "error": None,
    }
    binary, extra_path = analyzer_binary(agent)
    if binary is None:
        result["error"] = f"the {agent} CLI was not found"
        return result
    set_vars, unset_vars, endpoint = api_env(api_key, agent, analyzer["endpoint_url"])
    result["endpoint_url"] = endpoint
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in unset_vars
        and not name.startswith(("ANTHROPIC_", "OPENAI_", "OPENROUTER_", "CLAUDE_", "CODEX_"))
    }
    env.update(set_vars)
    if extra_path:
        env["PATH"] = os.pathsep.join([*extra_path, env.get("PATH", "")])
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript = out_dir / "analyzer-transcript.jsonl"
    started = time.monotonic()
    # The configuration directory may hold the key (Codex's auth.json), so it
    # lives in a private temporary directory and is removed afterwards.
    ANALYZER_HOME.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wp-analyzer-", dir=ANALYZER_HOME) as home:
        if agent == "claude-code":
            if key_provider(api_key) == "anthropic" and model.startswith("anthropic/"):
                model = model.split("/", 1)[1]
            if key_provider(api_key) != "anthropic":
                env["ANTHROPIC_API_KEY"] = ""
            env.update(
                CLAUDE_CONFIG_DIR=home,
                DISABLE_AUTOUPDATER="1",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            )
            argv = [
                binary, "-p", "--model", model, "--output-format", "json",
                "--tools", CLAUDE_ANALYZER_TOOLS, "--permission-mode", "dontAsk",
                "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            ]  # fmt: skip
        else:
            # Same configuration harbor gives Codex in a trial: the endpoint in
            # config.toml and the key in auth.json.
            last_message = Path(home) / "last-message.txt"
            auth = Path(home) / "auth.json"
            auth.touch(mode=0o600)
            auth.write_text(json.dumps({"OPENAI_API_KEY": api_key}))
            if endpoint != OPENAI_ENDPOINT:
                config = f"openai_base_url = {json.dumps(endpoint)}\n"
                (Path(home) / "config.toml").write_text(config)
            env["CODEX_HOME"] = home
            argv = [
                binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "--cd", checkpoint_path, "--model", model.rsplit("/", 1)[-1],
                "--json", "--output-last-message", str(last_message), "-",
            ]  # fmt: skip
        try:
            done = subprocess.run(
                argv, input=analyzer["prompt"], cwd=checkpoint_path, env=env,
                capture_output=True, text=True, errors="replace",
                timeout=ANALYZER_TIMEOUT_SEC,
            )  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            result["error"] = f"timed out after {ANALYZER_TIMEOUT_SEC} s"
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            transcript.write_text(scrub(stdout, api_key))
            done = None
        if done is not None:
            result["exit_code"] = done.returncode
            stdout, stderr = scrub(done.stdout, api_key), scrub(done.stderr, api_key)
            transcript.write_text(stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else ""))
            if agent == "claude-code":
                try:
                    reply = json.loads(stdout)
                except json.JSONDecodeError:
                    reply = {}
                result.update(
                    output=reply.get("result"),
                    cost_usd=reply.get("total_cost_usd"),
                    num_turns=reply.get("num_turns"),
                    usage=reply.get("usage"),
                    permission_denials=len(reply.get("permission_denials") or []),
                )
                if reply.get("is_error") or not reply:
                    result["error"] = (reply.get("result") or stderr.strip() or "no reply")[:2000]
            else:
                if last_message.exists():
                    result["output"] = scrub(last_message.read_text(), api_key).strip() or None
                usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
                warnings: list[str] = []
                for line in stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "turn.completed":
                        for name in usage:
                            usage[name] += (event.get("usage") or {}).get(name) or 0
                    elif event.get("type") == "turn.failed":
                        message = (event.get("error") or {}).get("message") or event
                        result["error"] = str(message)[:2000]
                    elif event.get("type") == "error":
                        # e.g. "Reconnecting..." while Codex falls back from
                        # WebSockets to HTTP; fatal only if the turn fails.
                        warnings.append(str(event.get("message"))[:300])
                result["usage"] = usage
                if warnings:
                    result["warnings"] = warnings
                if done.returncode != 0 and not result["error"]:
                    result["error"] = (stderr.strip() or f"exit code {done.returncode}")[-2000:]
    result["finished_at"] = utc_now()
    result["duration_sec"] = round(time.monotonic() - started, 1)
    if result["output"]:
        (out_dir / "analysis.md").write_text(result["output"] + "\n")
    result["transcript"] = str(transcript)
    return result




# Run in the fork just before the checkpoint: end any `asciinema rec` process and
# print how many were found. CRIU cannot dump a session whose terminal is held
# by the recorder.
STOP_RECORDERS = (
    'n=0; for p in /proc/[0-9]*; do '
    'if tr "\\0" " " < $p/cmdline 2>/dev/null | grep -q "asciinem[a] rec"; then '
    'kill ${p#/proc/} 2>/dev/null && n=$((n+1)); fi; done; '
    'if [ $n -gt 0 ]; then sleep 2; fi; echo $n'
)

TOP_LEVEL_KEYS = {"dataset", "agent", "trials", "passk"}
API_KEY_FIELDS = ("api-key", "api_key")
ENDPOINT_FIELDS = ("endpoint-url", "endpoint_url")
TRIAL_KEYS = {
    "task", "model", "dataset", "agent", "dockerfile_dir", "restore_check_command",
    *API_KEY_FIELDS, *ENDPOINT_FIELDS, "context", "context_files", "analyzer",
}  # fmt: skip
# What ends a Jinja `{% raw %}` block early; the context is rendered inside one.
RAW_END = re.compile(r"\{%[-+]?\s*endraw")
# Heads an analyzer's answer when Pass@k passes it on in the next "context".
ANALYZER_CONTEXT_PREFIX = "Analyzer output:"
# Where a context file lands when the input gives no "dest".
CONTEXT_DIR = "/context"
# Agents the context fields are not offered for.
NO_CONTEXT_AGENTS = ("terminus", "terminus-1", "terminus-2")


class TrialConfigError(ValueError):
    """The input JSON breaks a rule of the context fields or repeats a key."""


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """json.loads hook: plain parsing silently keeps only the last of two
    identical keys, which would hide e.g. a second "context" in a trial."""
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise TrialConfigError(f'"{key}" appears more than once in the same object')
        seen[key] = value
    return seen


def resolve_context(entry: dict, agent: str, base_dir: Path, where: str):
    """Validate a trial's "context" and "context_files".

    Returns (context text or None, [{"path": host file or directory, "dest":
    container path, and "exclude": [paths inside a directory to leave out]
    when given}]).
    """
    context = entry.get("context")
    files = entry.get("context_files", [])
    if agent in NO_CONTEXT_AGENTS and ("context" in entry or "context_files" in entry):
        raise TrialConfigError(
            f'{where}: "context" and "context_files" are not supported with agent "{agent}"'
        )
    if context is not None:
        if not isinstance(context, str) or not context.strip():
            raise TrialConfigError(f'{where}: "context" must be a non-empty string')
        if RAW_END.search(context):
            raise TrialConfigError(f'{where}: "context" must not contain "{{% endraw %}}"')
    if not isinstance(files, list):
        raise TrialConfigError(f'{where}: "context_files" must be a list')
    resolved, destinations = [], set()
    for item in files:
        if isinstance(item, str):
            item = {"path": item}
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or set(item) - {"path", "dest", "exclude"}
        ):
            raise TrialConfigError(
                f'{where}: each "context_files" entry must be a path string or '
                '{"path": ..., "dest": ..., "exclude": [...]}'
            )
        exclude = item.get("exclude", [])
        if not isinstance(exclude, list) or not all(
            isinstance(path, str) and path.strip("/") and ".." not in Path(path).parts
            for path in exclude
        ):
            raise TrialConfigError(
                f'{where}: "exclude" must be a list of paths inside the context directory'
            )
        # Relative paths are relative to the input file, not the cwd.
        source = (base_dir / item["path"]).resolve()
        if not source.is_file() and not source.is_dir():
            raise TrialConfigError(
                f'{where}: context file or directory not found: {source}'
            )
        destination = item.get("dest") or f"{CONTEXT_DIR}/{source.name}"
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise TrialConfigError(
                f'{where}: "dest" must be an absolute container path, got {destination!r}'
            )
        if destination in destinations:
            raise TrialConfigError(
                f'{where}: two "context_files" entries would be copied to {destination}'
            )
        destinations.add(destination)
        resolved.append({"path": str(source), "dest": destination})
        if exclude:
            if not source.is_dir():
                raise TrialConfigError(f'{where}: "exclude" needs a directory, not {source}')
            resolved[-1]["exclude"] = exclude
    return context, resolved

REDACTED = "<redacted>"


PASSK_KEYS = {
    "iterations", "stopOnSuccess", "passContext", "passContextFilesDest", "passNested",
}  # fmt: skip
# Harbor's trial phases, as named in its result.json.
PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")


def read_config(config_path: Path) -> dict:
    """Read the input JSON as written, API keys included."""
    try:
        return json.loads(
            config_path.read_text(), object_pairs_hook=reject_duplicate_keys
        )
    except TrialConfigError as exc:
        raise TrialConfigError(f"{config_path}: {exc}") from None
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Cannot read {config_path}: {exc}")


def parse_passk(config: dict, where: str) -> dict | None:
    """Validate the optional top-level "passk" object and apply its defaults."""
    if "passk" not in config:
        return None
    passk = config["passk"]
    if not isinstance(passk, dict):
        raise TrialConfigError(f'{where}: "passk" must be an object')
    if unknown := set(passk) - PASSK_KEYS:
        raise TrialConfigError(f'{where}: unknown "passk" keys {sorted(unknown)}')
    iterations = passk.get("iterations")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise TrialConfigError(f'{where}: "passk.iterations" must be a whole number >= 1')
    stop = passk.get("stopOnSuccess", False)
    if not isinstance(stop, bool):
        raise TrialConfigError(f'{where}: "passk.stopOnSuccess" must be true or false')
    text = passk.get("passContext")
    if not isinstance(text, str) or not text.strip():
        raise TrialConfigError(f'{where}: "passk.passContext" must be a non-empty string')
    nested = passk.get("passNested", False)
    if not isinstance(nested, bool):
        raise TrialConfigError(f'{where}: "passk.passNested" must be true or false')
    dest = passk.get("passContextFilesDest")
    if not isinstance(dest, str) or not dest.startswith("/") or dest.rstrip("/") == "":
        raise TrialConfigError(
            f'{where}: "passk.passContextFilesDest" must be an absolute container directory'
        )
    for index, entry in enumerate(config.get("trials") or []):
        agent = (isinstance(entry, dict) and entry.get("agent")) or config.get("agent")
        if iterations > 1 and agent in NO_CONTEXT_AGENTS:
            raise TrialConfigError(
                f'{where}: trials[{index}]: "passk" passes context between iterations, '
                f'which agent "{agent}" does not support'
            )
    return {
        "iterations": iterations,
        "stopOnSuccess": stop,
        "passContext": text,
        "passContextFilesDest": dest.rstrip("/"),
        "passNested": nested,
    }


def parse_analyzer(entry: dict, where: str) -> tuple[dict | None, str]:
    """Validate a trial's "analyzer" object.

    Returns ({"agent", "model", "endpoint_url", "prompt"} or None, its API key or
    ""). The key is replaced by a marker in `entry`, which goes to the output.
    """
    if "analyzer" not in entry:
        return None, ""
    analyzer = entry["analyzer"]
    if not isinstance(analyzer, dict):
        raise TrialConfigError(f'{where}: "analyzer" must be an object')
    if unknown := set(analyzer) - ANALYZER_KEYS:
        raise TrialConfigError(f'{where}: unknown "analyzer" keys {sorted(unknown)}')
    if analyzer.get("agent") not in ANALYZER_AGENTS:
        raise TrialConfigError(
            f'{where}: "analyzer.agent" must be one of {", ".join(ANALYZER_AGENTS)}'
        )
    for field in ("model", "prompt"):
        if not isinstance(analyzer.get(field), str) or not analyzer[field].strip():
            raise TrialConfigError(f'{where}: "analyzer.{field}" must be a non-empty string')
    endpoint = None
    for field in ENDPOINT_FIELDS:
        if field in analyzer:
            endpoint = analyzer[field]
            if not isinstance(endpoint, str) or not re.fullmatch(
                r"https?://[^\s/]+(/\S*)?", endpoint
            ):
                raise TrialConfigError(f'{where}: "analyzer.{field}" must be an http(s) URL')
            endpoint = endpoint.rstrip("/")
    api_key = ""
    for field in API_KEY_FIELDS:
        if field in analyzer:
            if not isinstance(analyzer[field], str) or not analyzer[field].strip():
                raise TrialConfigError(f'{where}: "analyzer.{field}" must be a non-empty string')
            api_key = analyzer[field].strip()
            analyzer[field] = REDACTED
    agent = analyzer["agent"]
    if api_key:
        require_usable_key(api_key, agent, f'{where}: "analyzer.api-key"')
    if agent == "claude-code" and endpoint and endpoint.endswith("/v1"):
        raise TrialConfigError(
            f'{where}: with claude-code, "analyzer.endpoint-url" must not end in /v1'
        )
    return {
        "agent": agent,
        "model": analyzer["model"],
        "endpoint_url": endpoint,
        "prompt": analyzer["prompt"],
    }, api_key


def load_trials(
    config_path: Path, raw: dict | None = None
) -> tuple[dict, list[dict], list[str], list[str]]:
    """Parse an input: the file at `config_path`, or `raw` if given (then
    `config_path` only names it in messages and anchors relative paths).

    Returns the input for the output file, the resolved trial specs, each
    trial's API key and each trial's analyzer API key. Keys are returned
    separately and replaced by a marker in the other two, so they cannot reach
    the output JSON or the logs.
    """
    config = copy.deepcopy(raw) if raw is not None else read_config(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("trials"), list):
        sys.exit(f'{config_path}: expected an object with a "trials" list')
    if not config["trials"]:
        sys.exit(f"{config_path}: \"trials\" is empty")
    if unknown := set(config) - TOP_LEVEL_KEYS:
        sys.exit(f"{config_path}: unknown keys {sorted(unknown)}")

    trials = []
    api_keys: list[str] = []
    analyzer_keys: list[str] = []
    for index, entry in enumerate(config["trials"]):
        where = f"{config_path}: trials[{index}]"
        if not isinstance(entry, dict):
            sys.exit(f"{where}: expected an object")
        if unknown := set(entry) - TRIAL_KEYS:
            sys.exit(f"{where}: unknown keys {sorted(unknown)}")
        for key in ("task", "model"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                sys.exit(f'{where}: "{key}" is required')
        api_key = ""
        for field in API_KEY_FIELDS:
            if field in entry:
                if not isinstance(entry[field], str) or not entry[field].strip():
                    sys.exit(f'{where}: "{field}" must be a non-empty string')
                api_key = entry[field].strip()
                entry[field] = REDACTED
        api_keys.append(api_key)
        endpoint = None
        for field in ENDPOINT_FIELDS:
            if field in entry:
                endpoint = entry[field]
                if not isinstance(endpoint, str) or not re.fullmatch(
                    r"https?://[^\s/]+(/\S*)?", endpoint
                ):
                    sys.exit(f'{where}: "{field}" must be an http(s) URL')
                endpoint = endpoint.rstrip("/")
        task = entry["task"]
        spec = {
            "index": index,
            "task": task if "/" in task else f"terminal-bench/{task}",
            "model": entry["model"],
            "dataset": entry.get("dataset") or config.get("dataset") or DEFAULT_DATASET,
            "agent": entry.get("agent") or config.get("agent") or DEFAULT_AGENT,
            "dockerfile_dir": None,
            "endpoint_url": endpoint,
            "context": None,
            "context_files": [],
            "restore_check_command": entry.get("restore_check_command")
            or DEFAULT_RESTORE_CHECK,
        }
        if entry.get("dockerfile_dir"):
            # Relative paths are relative to the input file, not the cwd.
            dockerfile_dir = (config_path.parent / entry["dockerfile_dir"]).resolve()
            if not (dockerfile_dir / "Dockerfile").is_file():
                sys.exit(f"{where}: no Dockerfile in {dockerfile_dir}")
            spec["dockerfile_dir"] = str(dockerfile_dir)
            short_name = spec["task"].rsplit("/", 1)[-1]
            if short_name not in (dockerfile_dir / "Dockerfile").read_text():
                # A Dockerfile written for another task gives the agent the wrong
                # container and the verifier nothing to grade.
                log(
                    f"WARNING: {where}: {dockerfile_dir}/Dockerfile does not mention "
                    f"'{short_name}'; make sure it builds this task's environment"
                )
        spec["context"], spec["context_files"] = resolve_context(
            entry, spec["agent"], config_path.parent, where
        )
        spec["analyzer"], analyzer_key = parse_analyzer(entry, where)
        analyzer_keys.append(analyzer_key)
        if api_key:
            require_usable_key(api_key, spec["agent"], f'{where}: "api-key"')
        if spec["agent"] == "claude-code" and endpoint and endpoint.endswith("/v1"):
            # Claude Code appends /v1/messages itself, so this would request
            # .../v1/v1/messages and every model call would fail with 404.
            sys.exit(
                f'{where}: with claude-code, "endpoint-url" must not end in /v1; '
                f'use "{endpoint[: -len("/v1")]}"'
            )
        if key_provider(api_key) == "openrouter":
            model = spec["model"]
            if spec["agent"] == "claude-code" and model.startswith("openrouter/"):
                sys.exit(
                    f'{where}: with an OpenRouter key, claude-code takes the OpenRouter '
                    f'model ID itself, e.g. "anthropic/claude-opus-5.5", not "{model}"'
                )
            # codex is exempt: harbor passes it only the last segment of the
            # model name, and the endpoint decides where the call goes.
            if spec["agent"] not in ("claude-code", "codex") and not model.startswith(
                "openrouter/"
            ):
                log(
                    f'WARNING: {where}: the key is an OpenRouter key but model "{model}" '
                    'has no "openrouter/" prefix; the agent may not route it to OpenRouter'
                )
        trials.append(spec)
    return config, trials, api_keys, analyzer_keys


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Run Terminal-Bench trials with harbor inside Waypoint sessions "
        "and checkpoint each session after its trial.",
        epilog="Arguments after `--` are passed to every `harbor run` unchanged.",
    )
    parser.add_argument("config", nargs="?", help="input JSON listing the trials to run")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check that this host has what the trials need, then exit",
    )
    parser.add_argument(
        "--output", help="output JSON path (default: <output-dir>/results.json)"
    )
    parser.add_argument(
        "--run-name", default=time.strftime("%Y%m%d-%H%M%S"), help="names the output directory"
    )
    parser.add_argument(
        "--output-dir", help="default: ./runs/<run-name> beside this script"
    )
    parser.add_argument(
        "--state-dir",
        default=DEFAULT_STATE_DIR,
        help="durable directory for Waypoint sessions and the session registry",
    )
    parser.add_argument("--checkpoint-id", default="post-trial")
    parser.add_argument("--waypoint-bin", default=shutil.which("waypoint"))
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="leave each session's live fork running instead of suspending it after the checkpoint",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="keep the Buildah images built/pulled for the sessions (costs disk)",
    )
    parser.add_argument("--skip-restore-check", action="store_true")
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1 :]
    args = parser.parse_args(argv)
    if not args.config and not args.check:
        parser.error("an input JSON is required (or pass --check)")
    return args, extra


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def command_output(argv: list[str]) -> str | None:
    """First line of a command's output, or None if it cannot run."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (done.stdout or done.stderr).strip().splitlines()
    return lines[0].strip() if done.returncode == 0 and lines else None


@functools.cache
def provenance(harbor: str, waypoint_bin: str) -> dict:
    """What this run was made with, so a result can be traced and repeated."""
    from importlib import metadata

    def version(package: str) -> str | None:
        try:
            return metadata.version(package)
        except metadata.PackageNotFoundError:
            return None

    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, _, value = line.partition("=")
            os_release[key] = value.strip('"')
    except OSError:
        pass
    try:
        mem_kb = int(Path("/proc/meminfo").read_text().split()[1])
    except (OSError, ValueError, IndexError):
        mem_kb = 0
    git = ["git", "-C", str(HERE)]
    commit = command_output([*git, "rev-parse", "HEAD"])
    return {
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "git_commit": commit,
        "git_dirty": bool(commit) and bool(
            subprocess.run(
                [*git, "status", "--porcelain", "--", str(HERE)],
                capture_output=True, text=True,
            ).stdout.strip()
        ),  # fmt: skip
        "harbor": version("harbor"),
        "harbor_cli": harbor,
        "python": platform.python_version(),
        "waypoint": command_output([waypoint_bin, "version"]),
        "criu": command_output([*root_prefix(), "criu", "--version"]),
        "buildah": command_output(["buildah", "--version"]),
        "kernel": platform.release(),
        "os": os_release.get("PRETTY_NAME"),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "memory_gb": round(mem_kb / 1e6, 1),
    }


def phase_seconds(phase: dict | None) -> float | None:
    """Length of one phase of harbor's trial result, or None if it never ran."""
    if not phase or not phase.get("started_at") or not phase.get("finished_at"):
        return None
    begin = datetime.fromisoformat(phase["started_at"])
    end = datetime.fromisoformat(phase["finished_at"])
    return round((end - begin).total_seconds(), 1)


def run_trial(
    spec: dict,
    *,
    args: argparse.Namespace,
    harbor: str,
    harbor_extra: list[str],
    out_dir: Path,
    state_dir: Path,
    api_key: str,
) -> dict:
    """Run one trial, checkpoint its session, and return its output record."""

    def wp(*wp_args: str) -> list[str]:
        return waypoint_argv(state_dir, args.waypoint_bin, *wp_args)

    label = f"{spec['index']:02d}-{spec['task'].rsplit('/', 1)[-1]}"
    job_name = f"{args.run_name}-{label}"
    trial_out = out_dir / label
    jobs_dir = trial_out / "jobs"
    trial_out.mkdir(parents=True, exist_ok=True)

    # The record starts with the resolved inputs; results are filled in below.
    record: dict = {
        **spec,
        "reward": None,
        "cost_usd": None,
        "started_at": utc_now(),
        "finished_at": None,
        "duration_sec": None,
        "checkpoint_path": None,
        "output_dir": str(trial_out),
        "api_key_provider": key_provider(api_key) if api_key else None,
    }
    # Replaced below by the endpoint actually used when the trial has a key.
    endpoint = spec["endpoint_url"]
    started = time.monotonic()

    # 1. Run the trial. The API key reaches harbor only through the environment,
    #    in the variables that fit the key's provider and the agent, never on a
    #    command line.
    harbor_cmd = [
        harbor, "run",
        "--dataset", spec["dataset"],
        "--include-task-name", spec["task"],
        "--agent", spec["agent"],
        "--model", spec["model"],
        "--env", "run_trial:WaypointEnvironment",
        "--ek", f"state_dir={state_dir}",
        "--ek", f"waypoint_bin={args.waypoint_bin}",
        "--ek", "keep_agent_session=true",
        "--ek", f"prune_images={'false' if args.keep_images else 'true'}",
        "--jobs-dir", str(jobs_dir),
        "--job-name", job_name,
        "--n-concurrent", "1",
        "--n-attempts", "1",
        "--yes",
        *harbor_extra,
    ]  # fmt: skip
    if spec["dockerfile_dir"]:
        harbor_cmd += ["--ek", f"dockerfile_dir={spec['dockerfile_dir']}"]
    if spec["context"]:
        # Harbor renders the agent's prompt from this template, so the context
        # text comes first and the task instruction follows it. `raw` keeps
        # Jinja from interpreting braces in the user's text.
        template = trial_out / "prompt-template.j2"
        template.write_text(
            "{% raw %}" + spec["context"] + "{% endraw %}\n\n{{ instruction }}\n"
        )
        harbor_cmd += ["--ak", f"prompt_template_path={template}"]
    if spec["context_files"]:
        pairs = [
            [item["path"], item["dest"], item.get("exclude", [])]
            for item in spec["context_files"]
        ]
        harbor_cmd += ["--ek", f"context_files={json.dumps(pairs)}"]
    if spec["agent"] == "terminus-2":
        # terminus-2 records its tmux pane with asciinema by default. CRIU cannot
        # dump that recorder, and a failed dump leaves the fork unusable.
        harbor_cmd += ["--ak", "record_terminal_session=false"]
    env = dict(os.environ)
    if api_key:
        set_vars, unset_vars, endpoint = api_env(api_key, spec["agent"], endpoint)
        for name in unset_vars:
            env.pop(name, None)
        env.update(set_vars)
        record["endpoint_url"] = endpoint
        log(
            f"API key provider: {record['api_key_provider']}, endpoint: {endpoint} "
            f"(passed as {', '.join(sorted(set_vars))})"
        )
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH")]))
    log("running: " + shlex.join(harbor_cmd))
    with open(trial_out / "harbor-run.log", "w") as harbor_log:
        proc = subprocess.Popen(
            harbor_cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            harbor_log.write(line)
        record["harbor_exit_code"] = proc.wait()
    log(f"harbor exited with code {record['harbor_exit_code']}")

    session_files = sorted((jobs_dir / job_name).glob(f"*/{SESSION_FILE}"))
    if not session_files:
        record["finished_at"] = utc_now()
        record["duration_sec"] = round(time.monotonic() - started, 1)
        record["error"] = "harbor did not start a Waypoint session; nothing to checkpoint"
        return record
    trial_dir = session_files[0].parent
    session = json.loads(session_files[0].read_text())["session_id"]
    session_dir = state_dir / "sessions" / session
    record.update(trial_dir=str(trial_dir), waypoint_session=session)

    # Reward, cost and timing come from harbor's own trial result.
    result_path = trial_dir / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        agent_result = result.get("agent_result") or {}
        record.update(
            reward=rewards.get("reward"),
            rewards=rewards,
            cost_usd=agent_result.get("cost_usd"),
            n_input_tokens=agent_result.get("n_input_tokens"),
            n_output_tokens=agent_result.get("n_output_tokens"),
            agent_version=(result.get("agent_info") or {}).get("version"),
            exception=(result.get("exception_info") or {}).get("exception_type"),
            phase_sec={phase: phase_seconds(result.get(phase)) for phase in PHASES},
        )
        # False when the trial ended before the agent got to work, e.g. on an
        # environment start timeout: its checkpoint then holds no attempt.
        record["agent_ran"] = record["phase_sec"]["agent_execution"] is not None
        if result.get("started_at") and result.get("finished_at"):
            begin = datetime.fromisoformat(result["started_at"])
            end = datetime.fromisoformat(result["finished_at"])
            record.update(
                started_at=begin.isoformat(timespec="seconds"),
                finished_at=end.isoformat(timespec="seconds"),
                duration_sec=round((end - begin).total_seconds(), 1),
            )
    if record["finished_at"] is None:
        record["finished_at"] = utc_now()
        record["duration_sec"] = round(time.monotonic() - started, 1)
    log(
        f"reward: {record['reward']}  cost: {record['cost_usd']}  "
        f"duration: {record['duration_sec']}s"
    )

    # 2. Save the container state: seal main's filesystem delta and dump its
    #    process tree with CRIU as an immutable checkpoint.
    record["session_path"] = str(session_dir)
    stopped = subprocess.run(
        wp("exec", session, "main", "--", STOP_RECORDERS), capture_output=True, text=True
    ).stdout.strip()
    if stopped not in ("", "0"):
        log(f"stopped {stopped} asciinema recorder process(es) that CRIU cannot dump")
    log(f"checkpointing session {session} as '{args.checkpoint_id}'")
    checkpoint = subprocess.run(
        wp("checkpoint", session, args.checkpoint_id), capture_output=True, text=True
    )
    if checkpoint.returncode != 0:
        # The trial's result is still valid, so keep it and report why the state
        # could not be saved. Waypoint marks the fork failed; end it.
        dump_log = session_dir / "checkpoints" / args.checkpoint_id / "criu" / "dump.log"
        criu_errors = subprocess.run(
            [*root_prefix(), "grep", "-m", "5", "Error", str(dump_log)],
            capture_output=True,
            text=True,
        ).stdout.strip()
        record["error"] = (
            "waypoint checkpoint failed: "
            + (checkpoint.stdout + checkpoint.stderr).strip()
            + (f"\nCRIU: {criu_errors}" if criu_errors else "")
        )
        log(record["error"])
        subprocess.run(wp("suspend", session), capture_output=True)
        return record
    (trial_out / "waypoint-checkpoint.json").write_text(
        run_checked(wp("info", session, args.checkpoint_id)).stdout
    )
    record.update(
        checkpoint_id=args.checkpoint_id,
        checkpoint_path=str(session_dir / "checkpoints" / args.checkpoint_id),
    )

    # 3. Prove the checkpoint restores: materialize a fork from it, run a
    #    command there, and discard the fork. The checkpoint is unaffected.
    if not args.skip_restore_check:
        fork_id = "restore-check"
        command = spec["restore_check_command"]
        try:
            run_checked(wp("fork", session, args.checkpoint_id, "--id", fork_id))
            try:
                check = subprocess.run(
                    wp("exec", session, fork_id, "--", command), capture_output=True, text=True
                )
            finally:
                run_checked(wp("destroy", session, fork_id))
            (trial_out / "restore-check.txt").write_text(
                f"$ {command}\n{check.stdout}{check.stderr}[exit code {check.returncode}]\n"
            )
            record["restore_check_exit_code"] = check.returncode
            log(f"restore check exit code: {check.returncode}")
        except RuntimeError as exc:
            # The checkpoint itself was saved; keep the record and say what failed.
            record["error"] = f"restore check: {exc}"
            log(record["error"])

    # 4. End live compute but keep the checkpoint history.
    if not args.keep_running:
        log("suspending session (checkpoints are retained)")
        run_checked(wp("suspend", session))
    (trial_out / "waypoint-session.json").write_text(
        run_checked(wp("list", session, "--json")).stdout
    )
    record["session_live"] = args.keep_running

    # 5. The checkpoint holds process memory and files, so make sure the API key
    #    did not end up in it (or in the collected logs).
    if api_key:
        record["api_key_found_in"] = find_secret([session_dir, trial_out], api_key)
        if record["api_key_found_in"]:
            log(
                "WARNING: the API key was found in saved state:\n  "
                + "\n  ".join(record["api_key_found_in"])
            )
        else:
            log("secret scan: the API key is not present in the session or outputs")

    env_prefix = shlex.join(wp()[:-1])
    record["resume_command"] = (
        f"{env_prefix} waypoint fork {session} {args.checkpoint_id} --id resume"
    )
    record["cleanup_command"] = f"{env_prefix} waypoint cleanup {session}"
    return record


def analyze(spec: dict, record: dict, api_key: str, out_dir: Path) -> dict:
    """Run a trial's analyzer over its checkpoint and return the "analysis"."""
    analyzer = spec["analyzer"]
    checkpoint = record.get("checkpoint_path")
    if not checkpoint:
        log("analyzer skipped: the trial saved no checkpoint")
        return {**analyzer, "output": None, "error": "the trial saved no checkpoint"}
    log(f"analyzer: {analyzer['agent']} with {analyzer['model']} in {checkpoint}")
    usage_before = (
        openrouter_usage(api_key) if key_provider(api_key) == "openrouter" else None
    )
    trial_out = Path(record.get("output_dir") or out_dir)
    analysis = run_analyzer(analyzer, api_key, checkpoint, trial_out)
    analysis["openrouter_billed_usd"] = openrouter_billed(api_key, usage_before)
    if leaks := find_secret([trial_out], api_key):
        analysis["error"] = "the analyzer's API key was found in: " + ", ".join(leaks)
    log(
        f"analyzer finished in {analysis['duration_sec']}s"
        + (f", error: {analysis['error']}" if analysis.get("error") else "")
    )
    return analysis


def run_input(
    raw: dict,
    config_path: Path,
    *,
    args: argparse.Namespace,
    harbor: str,
    harbor_extra: list[str],
    out_dir: Path,
    output_path: Path,
    state_dir: Path,
) -> tuple[dict, int]:
    """Run every trial of one input and write its output JSON.

    Returns (the output, the exit code these trials call for).
    """
    config, trials, api_keys, analyzer_keys = load_trials(config_path, raw)

    # A trial's own "api-key" wins; a key from the environment is the fallback.
    for position, spec in enumerate(trials):
        if api_keys[position]:
            continue
        name, value = env_fallback_key(spec["agent"], spec["endpoint_url"])
        if value:
            require_usable_key(value, spec["agent"], f"{name} in the environment")
            api_keys[position] = value
        elif spec["agent"] in AGENT_PROVIDERS:
            sys.exit(
                f"trials[{spec['index']}] has no \"api-key\" and {name} is not set; "
                f"{spec['agent']} needs a key."
            )

    for position, spec in enumerate(trials):
        analyzer = spec["analyzer"]
        if analyzer is None:
            continue
        if not analyzer_keys[position]:
            name, value = env_fallback_key(analyzer["agent"], analyzer["endpoint_url"])
            if not value:
                sys.exit(
                    f"trials[{spec['index']}]: the analyzer has no \"api-key\" and {name} "
                    "is not set"
                )
            require_usable_key(value, analyzer["agent"], f"{name} in the environment")
            analyzer_keys[position] = value
        service = endpoint_provider(analyzer["endpoint_url"])
        if service and service != key_provider(analyzer_keys[position]):
            sys.exit(
                f"trials[{spec['index']}]: the analyzer's "
                f"{key_provider(analyzer_keys[position])} key does not belong to "
                f'"endpoint-url" {analyzer["endpoint_url"]} ({service})'
            )
        if analyzer_binary(analyzer["agent"])[0] is None:
            sys.exit(
                f"trials[{spec['index']}]: the analyzer needs the {analyzer['agent']} CLI "
                "on this host (see README: Analyzer)"
            )
        if analyzer["agent"] == "codex" and (problem := codex_sandbox_problem()):
            sys.exit(
                f"trials[{spec['index']}]: the codex analyzer runs only in Codex's read-only "
                f"sandbox, which does not work on this host: {problem} (see README: Analyzer)"
            )

    for spec, api_key in zip(trials, api_keys):
        service = endpoint_provider(spec["endpoint_url"])
        if api_key and service and service != key_provider(api_key):
            # e.g. an Anthropic key with an openrouter.ai endpoint: the key
            # would be sent to a service that did not issue it.
            sys.exit(
                f"trials[{spec['index']}]: its {key_provider(api_key)} key does not "
                f'belong to "endpoint-url" {spec["endpoint_url"]} ({service})'
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    output: dict = {
        "input": config,
        "input_file": str(config_path),
        "run_name": args.run_name,
        "started_at": utc_now(),
        "finished_at": None,
        "output_dir": str(out_dir),
        "state_dir": str(state_dir),
        "environment": provenance(harbor, args.waypoint_bin),
        "trials": [],
    }

    def write_output() -> None:
        output_path.write_text(json.dumps(output, indent=2) + "\n")

    exit_code = 0
    for spec, api_key, analyzer_key in zip(trials, api_keys, analyzer_keys):
        log(f"=== trial {spec['index'] + 1}/{len(trials)}: {spec['task']} with {spec['model']}")
        # cost_usd is the agent's or harbor's own estimate, which can be far off
        # for models they have no prices for. For OpenRouter keys, also measure
        # what was actually billed: the key's total usage after the trial minus
        # before. Any other use of the same key meanwhile is included.
        usage_before = (
            openrouter_usage(api_key) if key_provider(api_key) == "openrouter" else None
        )
        try:
            record = run_trial(
                spec,
                args=args,
                harbor=harbor,
                harbor_extra=harbor_extra,
                out_dir=out_dir,
                state_dir=state_dir,
                api_key=api_key,
            )
        except Exception as exc:  # keep going: later trials are independent
            record = {**spec, "error": f"{type(exc).__name__}: {exc}"}
            log(f"trial failed: {record['error']}")
        output["trials"].append(record)
        write_output()  # after every trial, so a later failure loses nothing
        record["openrouter_billed_usd"] = openrouter_billed(api_key, usage_before)
        if usage_before is not None:
            log(f"OpenRouter billed: {record['openrouter_billed_usd']}")
            write_output()
        if spec["analyzer"] is not None:
            # After the trial's own billing is measured, so neither amount
            # includes the other.
            record["analysis"] = analyze(spec, record, analyzer_key, out_dir)
            write_output()
            if record["analysis"].get("error"):
                exit_code = exit_code or 1
        if record.get("api_key_found_in"):
            exit_code = 3
        elif (
            record.get("error")
            or record.get("exception")
            or record.get("checkpoint_path") is None
            or record.get("harbor_exit_code")
            or record.get("restore_check_exit_code")
        ):
            exit_code = exit_code or 1

    output["finished_at"] = utc_now()
    write_output()
    log(f"results written to {output_path}")
    return output, exit_code


def passed(record: dict) -> bool:
    reward = record.get("reward")
    return isinstance(reward, (int, float)) and reward >= 1.0


def run_passk(
    raw: dict,
    passk: dict,
    config_path: Path,
    *,
    args: argparse.Namespace,
    out_dir: Path,
    output_path: Path,
    state_dir: Path,
    **run_kwargs,
) -> int:
    """Pass@k: run the input up to `iterations` times.

    Each later iteration is a generated input in which every trial gets the
    upper directory of its own latest attempt that reached the agent as a
    context directory (without the copy nested in it, unless `passNested`),
    plus the `passContext` text and, when the trial has an analyzer that answered,
    "Analyzer output:" and that answer about the same attempt. With
    `stopOnSuccess`, a trial that reached reward 1.0 is not run again, and the
    loop ends once no trial is left.
    """
    redacted, base_trials, _, _ = load_trials(config_path, raw)
    base = {key: value for key, value in raw.items() if key != "passk"}
    summary: dict = {
        "input": redacted,
        "input_file": str(config_path),
        "run_name": args.run_name,
        "started_at": utc_now(),
        "finished_at": None,
        "output_dir": str(out_dir),
        "state_dir": str(state_dir),
        "environment": provenance(run_kwargs["harbor"], args.waypoint_bin),
        "passk": {**passk, "iterations_run": 0},
        "iterations": [],
        "trials": [
            {
                "index": spec["index"],
                "task": spec["task"],
                "agent": spec["agent"],
                "model": spec["model"],
                "passed": False,
                "first_pass_iteration": None,
                "attempts": [],
            }
            for spec in base_trials
        ],
        "pass_at_k": None,
    }

    def write_summary() -> None:
        output_path.write_text(json.dumps(summary, indent=2) + "\n")

    exit_code = 0
    remaining = list(range(len(base_trials)))  # positions in the original input
    # Per trial: (iteration, upper directory) of the latest attempt to pass on,
    # and the analyzer's answer about that attempt, if it gave one.
    last_upper: dict[int, tuple[int, str]] = {}
    last_analysis: dict[int, str] = {}
    dest = passk["passContextFilesDest"]
    for iteration in range(1, passk["iterations"] + 1):
        # Build this iteration's input. From the second one on, each trial
        # carries its previous attempt's files and the text announcing them.
        iteration_raw = {**base, "trials": []}
        for origin in remaining:
            entry = copy.deepcopy(base["trials"][origin])
            if origin in last_upper:
                analysis = last_analysis.get(origin)
                entry["context"] = "\n\n".join(
                    filter(
                        None,
                        [
                            entry.get("context"),
                            passk["passContext"],
                            analysis and f"{ANALYZER_CONTEXT_PREFIX}\n{analysis}",
                        ],
                    )
                )
                passed_on = {"path": last_upper[origin][1], "dest": dest}
                if not passk["passNested"]:
                    # The upper holds the copy that attempt was itself given at
                    # `dest`. Leaving it out passes on one attempt, not the whole
                    # chain, so the copy does not grow with every iteration.
                    passed_on["exclude"] = [dest.lstrip("/")]
                entry["context_files"] = [*entry.get("context_files", []), passed_on]
            iteration_raw["trials"].append(entry)

        iteration_dir = out_dir / f"iter-{iteration:02d}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        # The saved copy has API keys redacted; fill them in to reuse it as an
        # input file by hand.
        input_copy = iteration_dir / "input.json"
        input_copy.write_text(
            json.dumps(load_trials(config_path, iteration_raw)[0], indent=2) + "\n"
        )
        log(
            f"##### pass@k iteration {iteration}/{passk['iterations']}: "
            f"{len(remaining)} trial(s)"
        )
        iteration_args = argparse.Namespace(**vars(args))
        iteration_args.run_name = f"{args.run_name}-iter{iteration:02d}"
        results_path = iteration_dir / "results.json"
        output, code = run_input(
            iteration_raw,
            config_path,
            args=iteration_args,
            out_dir=iteration_dir,
            output_path=results_path,
            state_dir=state_dir,
            **run_kwargs,
        )
        exit_code = max(exit_code, code)
        summary["passk"]["iterations_run"] = iteration
        summary["iterations"].append(
            {
                "iteration": iteration,
                "input_file": str(input_copy),
                "results_file": str(results_path),
                "trial_indexes": list(remaining),
            }
        )

        still_remaining = []
        for origin, record in zip(remaining, output["trials"]):
            trial = summary["trials"][origin]
            trial["attempts"].append(
                {
                    "iteration": iteration,
                    **{
                        key: record.get(key)
                        for key in (
                            "reward", "cost_usd", "openrouter_billed_usd", "duration_sec",
                            "started_at", "finished_at", "checkpoint_path", "context",
                            "context_files", "exception", "error", "output_dir",
                            "phase_sec", "agent_ran", "restore_check_exit_code",
                            "api_key_found_in", "analysis",
                        )
                    },
                    "context_from_iteration": last_upper.get(origin, (None,))[0],
                }  # fmt: skip
            )
            if passed(record) and not trial["passed"]:
                trial["passed"] = True
                trial["first_pass_iteration"] = iteration
            if record.get("checkpoint_path") and record.get("agent_ran"):
                last_upper[origin] = (
                    iteration, str(Path(record["checkpoint_path"]) / "upper")
                )
                # Only an answer about this attempt goes with its files; a
                # failed analysis leaves none rather than an older one.
                analysis = record.get("analysis") or {}
                if analysis.get("output") and not analysis.get("error"):
                    # Keep the text inside the prompt template's raw block.
                    last_analysis[origin] = RAW_END.sub(
                        lambda match: "{ %" + match.group(0)[2:], analysis["output"]
                    )
                else:
                    last_analysis.pop(origin, None)
            elif origin in last_upper:
                # Nothing was attempted (e.g. the environment did not start in
                # time); its layer is not a previous attempt. Keep the older one.
                log(
                    f"trial {origin}: iteration {iteration} has no agent attempt to pass "
                    f"on; the next iteration gets iteration {last_upper[origin][0]}'s files"
                )
            if not (passk["stopOnSuccess"] and passed(record)):
                still_remaining.append(origin)
        remaining = still_remaining
        summary["pass_at_k"] = round(
            sum(trial["passed"] for trial in summary["trials"]) / len(summary["trials"]), 4
        )
        write_summary()
        if not remaining:
            log("every trial has passed; stopping early")
            break

    summary["finished_at"] = utc_now()
    write_summary()
    log(f"pass@k summary written to {output_path}  (pass@k = {summary['pass_at_k']})")
    return exit_code


def find_harbor() -> str | None:
    harbor_bin = Path(sys.executable).parent / "harbor"
    return str(harbor_bin) if harbor_bin.exists() else shutil.which("harbor")


# The harbor release this script was written and validated against.
TESTED_HARBOR = "0.23.0"


def preflight(args: argparse.Namespace) -> int:
    """Report whether this host has everything a trial needs; 0 if it does."""
    results: list[tuple[str, str, str]] = []

    def note(status: str, what: str, detail: str = "") -> None:
        results.append((status, what, detail))

    machine = platform.machine()
    note(
        "ok" if sys.platform == "linux" else "FAIL", "Linux host",
        f"{platform.system()} {platform.release()} {machine}",
    )  # fmt: skip
    if machine not in ("x86_64", "AMD64"):
        note("warn", "x86-64", f"{machine}: the example Dockerfile installs an x86-64 Node.js")
    root_ok = os.geteuid() == 0 or subprocess.run(
        ["sudo", "-n", "true"], capture_output=True
    ).returncode == 0
    note(
        "ok" if root_ok else "FAIL", "root access",
        "root" if os.geteuid() == 0 else "passwordless sudo",
    )  # fmt: skip
    found = command_output([args.waypoint_bin, "version"]) if args.waypoint_bin else None
    note("ok" if found else "FAIL", "waypoint", found or "not found; pass --waypoint-bin")
    for tool, argv in (("criu", ["criu", "--version"]), ("buildah", ["buildah", "--version"])):
        found = command_output([*(root_prefix() if root_ok else []), *argv])
        note("ok" if found else "FAIL", tool, found or "not found")
    if root_ok:
        criu_check = subprocess.run(
            [*root_prefix(), "criu", "check"], capture_output=True, text=True
        )
        last_line = ((criu_check.stdout + criu_check.stderr).strip().splitlines() or [""])[-1]
        note("ok" if criu_check.returncode == 0 else "FAIL", "criu check", last_line)
    from importlib import metadata

    harbor_version = metadata.version("harbor")
    note(
        "ok" if harbor_version == TESTED_HARBOR else "warn", "harbor package",
        harbor_version
        + ("" if harbor_version == TESTED_HARBOR else f" (tested with {TESTED_HARBOR})"),
    )  # fmt: skip
    harbor = find_harbor()
    note("ok" if harbor else "FAIL", "harbor CLI", harbor or "not next to this Python or on PATH")
    for agent in ANALYZER_AGENTS:
        found = analyzer_version(agent)
        note(
            "ok" if found else "info", f"analyzer: {agent}",
            found or "not installed (needed only by trials with an analyzer using it)",
        )  # fmt: skip
        if agent == "codex" and found:
            problem = codex_sandbox_problem()
            note(
                "ok" if problem is None else "warn", "analyzer: codex sandbox",
                "works" if problem is None
                else f"unusable, so codex analyzers refuse to run: {problem}",
            )  # fmt: skip
    probe = Path(args.state_dir)
    while not probe.exists():
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / 1e9
    note(
        "ok" if free_gb >= 20 else "warn", "free disk",
        f"{free_gb:.0f} GB under {probe} (each saved session takes about 1-3 GB)",
    )  # fmt: skip
    for name in PROVIDER_ENV_KEYS.values():
        present = bool(os.environ.get(name, "").strip())
        note(
            "ok" if present else "info", name,
            "set" if present else 'not set (needed only by trials without an "api-key")',
        )  # fmt: skip

    width = max(len(what) for _, what, _ in results)
    for status, what, detail in results:
        print(f"[{status:>4}] {what:<{width}}  {detail}")
    failed = [what for status, what, _ in results if status == "FAIL"]
    print(
        "host check: "
        + ("FAILED: " + ", ".join(failed) if failed else "everything required is present")
    )
    return 1 if failed else 0


def main() -> int:
    args, harbor_extra = parse_args()
    if args.check:
        return preflight(args)
    config_path = Path(args.config).resolve()
    raw = read_config(config_path)
    load_trials(config_path, raw)  # fail on a bad input before touching anything
    passk = parse_passk(raw, str(config_path))

    if not args.waypoint_bin:
        sys.exit("waypoint was not found on PATH; pass --waypoint-bin.")
    harbor = find_harbor()
    if not harbor:
        sys.exit("The harbor CLI was not found next to this Python or on PATH.")
    if root_prefix() and subprocess.run(["sudo", "-n", "true"]).returncode != 0:
        sys.exit("Waypoint needs root: run as root or enable passwordless sudo.")

    out_dir = Path(args.output_dir or HERE / "runs" / args.run_name).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output).resolve() if args.output else out_dir / "results.json"
    state_dir = Path(args.state_dir).resolve()

    # A build briefly holds the image and Waypoint's staged copy of it, and
    # every trial keeps its session. Running out mid-build fails the trial.
    probe = state_dir if state_dir.exists() else state_dir.parent
    free_gb = shutil.disk_usage(probe).free / 1e9
    if free_gb < 3:
        log(f"WARNING: only {free_gb:.1f} GB free under {probe}; trials may run out of disk")

    common = dict(
        args=args,
        harbor=harbor,
        harbor_extra=harbor_extra,
        out_dir=out_dir,
        output_path=output_path,
        state_dir=state_dir,
    )
    if passk is None:
        return run_input(raw, config_path, **common)[1]
    return run_passk(raw, passk, config_path, **common)


if __name__ == "__main__":
    sys.exit(main())
