"""Run a trial inside a Waypoint session instead of a Docker container (ADR-0013).

Waypoint saves a running Linux environment: its files and every program running in it. A first
attempt runs here so that, when its agent stops and before the tests run, its environment can
be saved (`final`). A repair can then open a copy of that save with the programs still running.

Two Harbor environments, selected in a job config like the Docker ones:

    "environment": {"import_path": "trajlab.capture.waypoint:WaypointEnvironment",
                    "kwargs": {"state_dir": "/srv/trajlab/waypoint"}}
    "environment": {"import_path": "trajlab.capture.waypoint:WaypointForkEnvironment",
                    "kwargs": {"source_trial": "<failed trial dir>", "stop_processes": false}}

`WaypointEnvironment` starts from the same pre-installed image as `PreinstalledDockerEnvironment`
(ADR-0008): the image is exported to a directory once and `waypoint init` copies it into each new
session. `WaypointForkEnvironment` opens a copy of a first attempt's save instead.

Both are Docker environments underneath, for two reasons: the pre-installed image is made with
Docker, and a task's separate verifier environment (Harbor builds it from the trial's own
environment config) runs in Docker exactly as Harbor would run it. Only the agent environment
runs on Waypoint.

What each trial gets that Waypoint does not provide (decision 6): its own network namespace
with outbound NAT, so copies of one save can all reopen the same server port, and a systemd scope
with the task's CPU and memory limits. Every Waypoint command that starts or restores programs
runs inside both; restored programs stay in the namespace and cgroup of the command that
restored them, since Waypoint opens no network namespace and runs CRIU with
`--manage-cgroups=ignore`.

Everything Waypoint does needs root; every host command here goes through `sudo -n` unless the
process already runs as root.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, override

import yaml
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.paths import EnvironmentPaths

from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.preinstall import PreinstalledDockerEnvironment, PreinstallError
from trajlab.contracts import (
    FINAL_CHECKPOINT,
    PREINSTALL_RECORD_FILENAME,
    WAYPOINT_RECORD_FILENAME,
    RunningProcess,
    WaypointRecord,
    WaypointSave,
)

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/srv/trajlab/waypoint"
# Per-command scratch space inside the environment, removed after every command, so nothing of
# it is in a save.
GUEST_SCRATCH = "/run/trajlab-waypoint"
AGENT_LOGS = EnvironmentPaths.agent_dir.as_posix()
# Waypoint dials /proc/<pid>/root/<sessions dir>/<16 hex>/temp/shell_<16 hex>.sock, which must fit
# in 107 bytes (Waypoint's config.go, validateSessionsDir): 18 + len(dir) + 50.
MAX_SESSIONS_DIR = (
    107 - len("/proc/1234567/root") - len("/0123456789abcdef/temp/shell_0123456789abcdef.sock")
)
NETWORK_PREFIX = "tlwp"
# Each trial's network is one /30 in 10.213.0.0/16: index i covers 10.213.0.0 + 4i.
NETWORK_COUNT = 16384
# Environment variables whose values must not be in a save (decision 8).
SECRET_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENROUTER_API_KEY",
)
MIN_SECRET_LENGTH = 20


class WaypointError(PreinstallError):
    """A Waypoint step failed; the trial does not run on a half-made environment."""


# --------------------------------------------------------------------------------------------- #
# Host commands
# --------------------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], float | None, str | None], Awaitable[CommandResult]]


def root_prefix() -> list[str]:
    """Waypoint, CRIU, ip, and iptables need root: go through sudo unless already root."""
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


async def run_host(args: list[str], timeout: float | None, stdin: str | None) -> CommandResult:
    """Run a host command; on timeout or cancellation, end it and re-raise."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except BaseException:
        if process.returncode is None:
            try:
                process.terminate()
            except (ProcessLookupError, PermissionError):
                pass
        raise
    return CommandResult(
        process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    )


@dataclass(frozen=True)
class Limits:
    """The task's CPU and memory limits, applied as a systemd scope around Waypoint."""

    cpus: int | None = None
    memory_mb: int | None = None

    def scope(self) -> list[str]:
        properties = []
        if self.cpus:
            properties += ["-p", f"CPUQuota={self.cpus * 100}%"]
        if self.memory_mb:
            properties += ["-p", f"MemoryMax={self.memory_mb}M"]
        return ["systemd-run", "--scope", "--quiet", "--collect", *properties]


@dataclass
class Waypoint:
    """The `waypoint` CLI, with every call pointed at the same durable state directory."""

    state_dir: Path
    binary: str = "waypoint"
    runner: Runner = run_host

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def info_dir(self) -> Path:
        return self.state_dir / "info"

    def argv(
        self, *args: str, network: str | None = None, limits: Limits | None = None
    ) -> list[str]:
        """`sudo [systemd-run --scope] [nsenter --net=<ns>] env WAYPOINT_...=... waypoint ...`."""
        argv = [*root_prefix()]
        if limits is not None:
            argv += limits.scope()
        if network is not None:
            argv += ["nsenter", f"--net=/run/netns/{network}"]
        return [
            *argv,
            "env",
            f"WAYPOINT_SESSIONS_DIR={self.sessions_dir}",
            f"WAYPOINT_SESSION_INFO_DIR={self.info_dir}",
            self.binary,
            *args,
        ]

    async def __call__(
        self,
        *args: str,
        timeout: float | None = None,
        check: bool = True,
        network: str | None = None,
        limits: Limits | None = None,
    ) -> CommandResult:
        result = await self.runner(self.argv(*args, network=network, limits=limits), timeout, None)
        if check and result.returncode != 0:
            output = (result.stdout + result.stderr).strip()[-2000:]
            raise WaypointError(f"waypoint {' '.join(args[:3])} failed: {output}")
        return result

    async def root(
        self, *args: str, timeout: float | None = None, stdin: str | None = None
    ) -> CommandResult:
        """Any other host command, as root."""
        return await self.runner([*root_prefix(), *args], timeout, stdin)

    async def version(self) -> str:
        result = await self.runner([self.binary, "version"], 30, None)
        match = re.search(r"v?\d+\.\d+\.\d+\S*", result.stdout)
        return match.group(0) if match else result.stdout.strip() or "unknown"


def check_sessions_dir(sessions_dir: Path) -> None:
    if ":" in str(sessions_dir) or "," in str(sessions_dir):
        raise WaypointError(f"{sessions_dir} contains ':' or ',', which OverlayFS cannot mount")
    if len(str(sessions_dir)) > MAX_SESSIONS_DIR:
        raise WaypointError(
            f"{sessions_dir} is {len(str(sessions_dir))} characters; Waypoint's socket path "
            f"limit allows at most {MAX_SESSIONS_DIR}. Use a shorter state_dir."
        )


# --------------------------------------------------------------------------------------------- #
# Networks: one namespace per trial, NAT to the outside (decision 6)
# --------------------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Network:
    index: int

    @property
    def name(self) -> str:
        return f"{NETWORK_PREFIX}-{self.index}"

    @property
    def host_link(self) -> str:
        return f"{NETWORK_PREFIX}{self.index}h"

    def _address(self, offset: int) -> str:
        value = self.index * 4 + offset
        return f"10.213.{value // 256}.{value % 256}"

    @property
    def subnet(self) -> str:
        return f"{self._address(0)}/30"

    @property
    def host_ip(self) -> str:
        return self._address(1)

    @property
    def guest_ip(self) -> str:
        return self._address(2)

    def create_script(self) -> str:
        """Shell commands, run as root, that make the namespace and route it out through NAT."""
        name, host, guest = self.name, self.host_link, f"{NETWORK_PREFIX}{self.index}g"
        return "\n".join(
            [
                "set -e",
                f"ip link add {host} type veth peer name {guest}",
                f"ip link set {guest} netns {name}",
                f"ip -n {name} link set {guest} name eth0",
                f"ip addr add {self.host_ip}/30 dev {host}",
                f"ip link set {host} up",
                f"ip -n {name} addr add {self.guest_ip}/30 dev eth0",
                f"ip -n {name} link set eth0 up",
                f"ip -n {name} link set lo up",
                f"ip -n {name} route add default via {self.host_ip}",
                "sysctl -qw net.ipv4.ip_forward=1",
                *(f"iptables {rule}" for rule in self._rules("-I")),
            ]
        )

    def delete_script(self) -> str:
        """The reverse of `create_script`; every step runs even if an earlier one fails."""
        return "\n".join(
            [
                *(f"iptables {rule} 2>/dev/null" for rule in self._rules("-D")),
                f"ip link del {self.host_link} 2>/dev/null",
                f"ip netns del {self.name} 2>/dev/null",
                "true",
            ]
        )

    def _rules(self, action: str) -> list[str]:
        host = self.host_link
        nat_action = "-A" if action == "-I" else action
        return [
            f"-t nat {nat_action} POSTROUTING -s {self.subnet} ! -o {host} -j MASQUERADE",
            f"{action} FORWARD -i {host} -j ACCEPT",
            f"{action} FORWARD -o {host} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
        ]


async def create_network(waypoint: Waypoint, key: str) -> Network:
    """Claim a free index (`ip netns add` fails if the name is taken) and build the network."""
    start = int(hashlib.sha256(key.encode()).hexdigest(), 16) % NETWORK_COUNT
    for step in range(NETWORK_COUNT):
        network = Network((start + step) % NETWORK_COUNT)
        claimed = await waypoint.root("ip", "netns", "add", network.name, timeout=30)
        if claimed.returncode != 0:
            continue
        built = await waypoint.root("sh", "-c", network.create_script(), timeout=60)
        if built.returncode != 0:
            await delete_network(waypoint, network)
            raise WaypointError(f"cannot set up network {network.name}: {built.stderr.strip()}")
        return network
    raise WaypointError("no free network index")


async def delete_network(waypoint: Waypoint, network: Network) -> None:
    await waypoint.root("sh", "-c", network.delete_script(), timeout=60)


async def export_rootfs(waypoint: Waypoint, image: str, image_id: str) -> Path:
    """`<state>/rootfs/<image id>/`: a Docker image's files, exported once and shared by every
    session made from it (`waypoint init` copies it; OverlayFS never uses it directly)."""
    key = image_id.removeprefix("sha256:")[:16]
    root = waypoint.state_dir / "rootfs"
    target = root / key
    script = "\n".join(
        [
            "set -e",
            f"mkdir -p {shlex.quote(str(root))}",
            f"exec 9>{shlex.quote(str(root / (key + '.lock')))}",
            "flock 9",
            f"[ -d {shlex.quote(str(target))} ] && exit 0",
            f"tmp={shlex.quote(str(target))}.tmp",
            'rm -rf "$tmp"; mkdir "$tmp"',
            f"cid=$(docker create {shlex.quote(image)} /bin/true)",
            "trap 'docker rm -f \"$cid\" >/dev/null 2>&1' EXIT",
            'docker export "$cid" | tar -x -C "$tmp" --numeric-owner',
            f'mv "$tmp" {shlex.quote(str(target))}',
        ]
    )
    result = await waypoint.root("sh", "-c", script, timeout=3600)
    if result.returncode != 0:
        raise WaypointError(f"cannot export {image}: {result.stderr.strip()[-2000:]}")
    return target


def nameservers(paths: Sequence[Path] = (Path("/run/systemd/resolve/resolv.conf"),)) -> list[str]:
    """The host's upstream DNS servers. A loopback resolver (systemd-resolved's 127.0.0.53) is
    unreachable from inside a network namespace, so those are skipped."""
    for path in [*paths, Path("/etc/resolv.conf")]:
        if not path.is_file():
            continue
        found = [
            line.split()[1]
            for line in path.read_text().splitlines()
            if line.startswith("nameserver") and len(line.split()) > 1
        ]
        found = [server for server in found if not server.startswith(("127.", "::1"))]
        if found:
            return found
    return ["1.1.1.1"]


# --------------------------------------------------------------------------------------------- #
# Scripts run inside the environment or on the host
# --------------------------------------------------------------------------------------------- #

# Lists (and with KILL=1, kills with SIGKILL) every program in the environment except Waypoint's
# own shell and this script's chain of parents. Only bash builtins, so the scan starts no process
# of its own. Prints "<pid>\t<command line>" per program.
GUEST_PROCESSES = r"""
keep=" 1 "
p=$$
while [ "$p" -gt 1 ]; do
  keep="$keep$p "
  pp=1
  while IFS= read -r line; do
    case $line in PPid:*) pp=${line#PPid:}; pp=${pp//[[:space:]]/}; break ;; esac
  done < /proc/$p/status
  p=$pp
done
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  case "$keep" in *" $p "*) continue ;; esac
  args=()
  while IFS= read -r -d '' a; do args+=("$a"); done < "$d/cmdline" 2>/dev/null
  [ ${#args[@]} -gt 0 ] || continue
  c="${args[*]}"
  printf '%s\t%s\n' "$p" "${c:0:200}"
  if [ "${KILL:-0}" = 1 ]; then kill -9 "$p" 2>/dev/null; fi
done
"""

# Run on the host as root: kill (SIGKILL) every Claude Code process under one fork's init, so
# the programs it started are not shut down with it. Prints the killed host pids as JSON.
KILL_AGENT = r"""
import json, os, signal, sys
fork_json = sys.argv[1]
try:
    root = json.load(open(fork_json))["pid"]
except (OSError, ValueError, KeyError):
    print("[]"); sys.exit(0)
children = {}
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open(f"/proc/{entry}/status") as f:
            ppid = next(int(l.split()[1]) for l in f if l.startswith("PPid:"))
    except (OSError, StopIteration, ValueError):
        continue
    children.setdefault(ppid, []).append(int(entry))
def is_agent(pid):
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        return False
    names = [os.path.basename(a.decode(errors="replace")) for a in args[:2]]
    runtime = names[0] in ("node", "bun") and names[1:2] in (["claude"], ["cli.js"])
    return names[0] == "claude" or runtime
killed, todo = [], list(children.get(root, []))
while todo:
    pid = todo.pop()
    todo.extend(children.get(pid, []))
    if is_agent(pid):
        try:
            os.kill(pid, signal.SIGKILL); killed.append(pid)
        except OSError:
            pass
print(json.dumps(killed))
"""


def parse_processes(text: str) -> tuple[RunningProcess, ...]:
    found = []
    for line in text.splitlines():
        pid, _, command = line.partition("\t")
        if pid.strip().isdigit():
            found.append(RunningProcess(pid=int(pid), command=command))
    return tuple(found)


def secrets_in_environment() -> list[str]:
    return [
        value
        for name in SECRET_VARIABLES
        if len(value := os.environ.get(name, "").strip()) >= MIN_SECRET_LENGTH
    ]


def fork_name(session_id: str) -> str:
    """A Waypoint fork id from Harbor's environment session id: letters, digits, `._-`, <= 64."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", session_id.removesuffix("__env"))
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:8]
    return f"r{stem[:50]}-{digest}"


# --------------------------------------------------------------------------------------------- #
# The Harbor environments
# --------------------------------------------------------------------------------------------- #


class WaypointEnvironment(PreinstalledDockerEnvironment):
    """The agent environment runs as the `main` fork of a new Waypoint session.

    start()   export the pre-installed image to a directory once, `waypoint init` a session
              from it inside the trial's network and limits.
    exec()    the command and its environment are copied in as files, so secrets never appear
              on a host command line; stdout and stderr come back from files.
    save      at the first download of /logs/agent, which Harbor does right after the agent
              phase ends (also after a timeout) and before the tests (decision 2).
    stop()    `waypoint suspend` if the trial saved (the save stays for repairs), otherwise
              `waypoint cleanup`; then the network is removed.

    Environment kwargs (`--ek` or the job config's environment kwargs):
      state_dir       holds sessions/, info/, rootfs/ (default /srv/trajlab/waypoint).
      waypoint_bin    the waypoint binary.
      save_final      make the `final` save (first attempts: true; repairs: false).
      isolate_network give the trial its own network namespace (default true).
      apply_limits    apply the task's CPU and memory limits (default true).
    """

    waypoint_runner: Runner = run_host

    def __init__(
        self,
        *args: Any,
        state_dir: str = DEFAULT_STATE_DIR,
        waypoint_bin: str | None = None,
        save_final: bool | str = True,
        isolate_network: bool | str = True,
        apply_limits: bool | str = True,
        **kwargs: Any,
    ) -> None:
        self.waypoint = Waypoint(
            Path(state_dir),
            waypoint_bin or shutil.which("waypoint") or "waypoint",
            type(self).waypoint_runner,
        )
        self.save_final = as_bool(save_final)
        self.isolate_network = as_bool(isolate_network)
        self.apply_limits = as_bool(apply_limits)
        self.wp_session: str | None = None
        self.wp_fork = "main"
        self.network: Network | None = None
        self.image_env: dict[str, str] = {}
        self.image_workdir: str | None = None
        self.saved = False
        self.record: WaypointRecord | None = None
        super().__init__(*args, **kwargs)

    # -- what Harbor sees ------------------------------------------------------------------- #

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        if not self.is_agent_environment():
            return super().capabilities
        # Not mounted: Harbor downloads /logs/agent after the agent phase, which is the moment
        # the save is made.
        return EnvironmentCapabilities(mounted=False)

    @override
    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        if not self.is_agent_environment():
            await super()._apply_network_policy(network_policy)
            return
        if network_policy.network_mode != NetworkMode.PUBLIC:
            raise WaypointError("the Waypoint environment supports public networking only")

    @property
    def limits(self) -> Limits | None:
        if not self.apply_limits:
            return None
        return Limits(cpus=self._effective_cpus, memory_mb=self._effective_memory_mb)

    @property
    def network_name(self) -> str | None:
        return self.network.name if self.network is not None else None

    def _require_session(self) -> str:
        if self.wp_session is None:
            raise WaypointError("the Waypoint environment is not started")
        return self.wp_session

    # -- lifecycle -------------------------------------------------------------------------- #

    @override
    async def start(self, force_build: bool) -> None:
        if not self.is_agent_environment():
            await DockerEnvironment.start(self, force_build=force_build)
            return
        check_sessions_dir(self.waypoint.sessions_dir)
        self._check_single_container()
        record = await self.prepare_image(force_build)
        self._check_root_agent()
        (self.trial_paths.trial_dir / PREINSTALL_RECORD_FILENAME).write_text(
            record.model_dump_json(indent=2) + "\n"
        )
        await self._read_image_config(record.image)
        rootfs = await self.ensure_rootfs(record.image, record.image_id)
        await self._open_network()
        out = await self.waypoint(
            "init",
            str(rootfs),
            "--quiet",
            "--shell",
            timeout=self.task_env_config.build_timeout_sec,
            network=self.network_name,
            limits=self.limits,
        )
        # --quiet prints "<session>,<work dir>" as its last line.
        self.wp_session = out.stdout.strip().splitlines()[-1].split(",")[0].strip()
        logger.info("Waypoint session %s started for %s", self.wp_session, self.session_id)
        await self._write_record(role="source", image=record.image)
        await self._write_resolv_conf()
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()
        await self.verify_trial_agent(record)

    def _check_single_container(self) -> None:
        """Waypoint runs one container: refuse a task with other services; a compose file that
        only configures `main` is ignored (its settings would not apply)."""
        for path in [self._environment_docker_compose_path, *self.extra_docker_compose_paths]:
            if not path.is_file():
                continue
            services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
            if set(services) - {"main"}:
                raise WaypointError(
                    f"{path} defines services {sorted(set(services) - {'main'})}; "
                    "Waypoint runs one container only"
                )
            logger.warning("%s configures main; Waypoint ignores compose settings", path)

    def _check_root_agent(self) -> None:
        if self._agent_user not in (None, "root", 0, "0"):
            # Supported through runuser (exec below), but a non-root agent has not been checked
            # on Waypoint; gate G3 lists such tasks.
            logger.warning("task agent user is %r; commands run through runuser", self._agent_user)

    async def _read_image_config(self, image: str) -> None:
        """The image's ENV and WORKDIR, which Docker applies to every exec and Waypoint does not
        when it starts from a directory."""
        raw = await self.docker("image", "inspect", "--format", "{{json .Config}}", image)
        config = json.loads(raw) or {}
        self.image_env = dict(item.split("=", 1) for item in config.get("Env") or [] if "=" in item)
        self.image_workdir = config.get("WorkingDir") or None

    async def ensure_rootfs(self, image: str, image_id: str) -> Path:
        return await export_rootfs(self.waypoint, image, image_id)

    async def _open_network(self) -> None:
        if self.isolate_network:
            self.network = await create_network(self.waypoint, self.session_id)

    async def _write_resolv_conf(self) -> None:
        if self.network is None:
            return
        lines = "".join(f"nameserver {server}\n" for server in nameservers())
        await self.exec(
            f"rm -f /etc/resolv.conf; printf %s {shlex.quote(lines)} > /etc/resolv.conf",
            user="root",
        )

    async def _write_record(
        self,
        *,
        role: str,
        image: str,
        opened_from: str | None = None,
        stopped: tuple[RunningProcess, ...] | None = None,
        save: WaypointSave | None = None,
    ) -> None:
        self.record = WaypointRecord(
            role=role,  # type: ignore[arg-type]
            waypoint_version=await self.waypoint.version(),
            sessions_dir=str(self.waypoint.sessions_dir),
            session=self._require_session(),
            fork=self.wp_fork,
            opened_from=opened_from,
            stopped_processes=stopped,
            image=image,
            network=self.network_name,
            save=save,
            recorded_at=datetime.now(UTC),
        )
        (self.trial_paths.trial_dir / WAYPOINT_RECORD_FILENAME).write_text(
            self.record.model_dump_json(indent=2) + "\n"
        )

    @override
    async def stop(self, delete: bool) -> None:
        if not self.is_agent_environment():
            await super().stop(delete)
            return
        try:
            if self.wp_session is not None and delete:
                if self.saved:
                    # The save stays on disk for repairs; nothing is left running.
                    await self.waypoint("suspend", self.wp_session, check=False)
                else:
                    await self._remove_session()
        finally:
            if self.network is not None and delete:
                await delete_network(self.waypoint, self.network)
                self.network = None

    async def _remove_session(self) -> None:
        session = self._require_session()
        result = await self.waypoint("cleanup", session, check=False)
        if result.returncode != 0:
            await self.waypoint("cleanup", session, "--force", check=False)
        self.wp_session = None

    # -- the save --------------------------------------------------------------------------- #

    def _wants_save(self, source_dir: str) -> bool:
        return (
            self.save_final
            and not self.saved
            and self.wp_session is not None
            and self.is_agent_environment()
            and PurePosixPath(source_dir) == PurePosixPath(AGENT_LOGS)
        )

    async def kill_agent(self) -> tuple[int, ...]:
        """SIGKILL Claude Code if it still runs (a timeout), leaving what it started running."""
        fork_json = self.waypoint.sessions_dir / self._require_session() / "forks" / self.wp_fork
        result = await self.waypoint.root(
            "python3", "-I", "-c", KILL_AGENT, str(fork_json / "fork.json"), timeout=60
        )
        try:
            return tuple(json.loads(result.stdout or "[]"))
        except ValueError:
            return ()

    async def processes(self, *, kill: bool = False) -> tuple[RunningProcess, ...]:
        prefix = "KILL=1 " if kill else ""
        result = await self.exec(f"{prefix}bash -c {shlex.quote(GUEST_PROCESSES)}", user="root")
        return parse_processes(result.stdout or "")

    async def save(self) -> WaypointSave:
        """Save files and running programs as `final`, before Harbor runs the tests."""
        session = self._require_session()
        self.saved = True
        killed = await self.kill_agent()
        running = await self.processes()
        started = time.monotonic()
        await self.waypoint(
            "snapshot",
            session,
            self.wp_fork,
            FINAL_CHECKPOINT,
            timeout=1800,
            network=self.network_name,
            limits=self.limits,
        )
        capture_ms = round((time.monotonic() - started) * 1000)
        path = self.waypoint.sessions_dir / session / "checkpoints" / FINAL_CHECKPOINT
        save = WaypointSave(
            path=str(path),
            agent_stopped="timeout_or_error" if killed else "ended",
            killed_agent_pids=killed,
            processes=running,
            capture_ms=capture_ms,
            files_bytes=await self._size(path / "upper"),
            memory_bytes=await self._size(path / "criu"),
            secret_found_in=await self._secret_scan(path),
            saved_at=datetime.now(UTC),
        )
        assert self.record is not None
        await self._write_record(role="source", image=self.record.image, save=save)
        logger.info(
            "saved %s/%s in %d ms with %d programs running",
            session,
            FINAL_CHECKPOINT,
            capture_ms,
            len(running),
        )
        if save.secret_found_in:
            logger.warning("an API key or token is in the save: %s", save.secret_found_in)
        return save

    async def _size(self, path: Path) -> int | None:
        result = await self.waypoint.root("du", "-sbL", str(path), timeout=600)
        first = result.stdout.split()[:1]
        return int(first[0]) if result.returncode == 0 and first and first[0].isdigit() else None

    async def _secret_scan(self, path: Path) -> tuple[str, ...]:
        """Files of the save holding a key or token. The values go to grep on stdin, never on a
        command line."""
        secrets = secrets_in_environment()
        if not secrets:
            return ()
        result = await self.waypoint.root(
            "grep", "-rlF", "-D", "skip", "-f", "-", "--", str(path),
            timeout=1800, stdin="\n".join(secrets) + "\n",
        )  # fmt: skip
        return tuple(result.stdout.splitlines()) if result.returncode == 0 else ()

    # -- exec ------------------------------------------------------------------------------- #

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        if not self.is_agent_environment():
            return await super().exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)
        session = self._require_session()
        user = self._resolve_user(user)
        merged = {**self.image_env, **(self._merge_env(env) or {})}
        cwd = cwd or self.task_env_config.workdir or self.image_workdir
        guest = f"{GUEST_SCRATCH}/{uuid.uuid4().hex}"
        runner = runner_script(guest, cwd=cwd, user=user, has_env=bool(merged))
        with tempfile.TemporaryDirectory(prefix="trajlab-wp-") as tmp:
            stage = Path(tmp) / "in"
            stage.mkdir()
            (stage / "cmd").write_text(command)
            (stage / "run.sh").write_text(runner)
            if merged:
                env_path = stage / "env"
                env_path.touch(mode=0o600)
                env_path.write_text(env_file(merged))
            await self.waypoint("cp", session, str(stage), f"{self.wp_fork}:{guest}")
            try:
                code = await self._run_in_fork(session, guest, timeout_sec)
                streams = {}
                for name in ("out", "err"):
                    host = Path(tmp) / name
                    await self.waypoint("cp", session, f"{self.wp_fork}:{guest}/{name}", str(host))
                    streams[name] = host.read_text(errors="replace")
            finally:
                await asyncio.shield(
                    self.waypoint(
                        "exec", session, self.wp_fork, "--", f"rm -rf {guest}", check=False
                    )
                )
        return ExecResult(stdout=streams["out"], stderr=streams["err"], return_code=code)

    async def _run_in_fork(self, session: str, guest: str, timeout_sec: int | None) -> int:
        try:
            result = await self.waypoint(
                "exec",
                session,
                self.wp_fork,
                "--",
                f"bash {guest}/run.sh",
                timeout=timeout_sec,
                check=False,
            )
        except BaseException as error:
            # Timeout or cancellation (Harbor's agent timeout). Kill Claude Code first, so the
            # interrupt below cannot make it shut down what it started, then end the client:
            # the client runs as root under sudo, so it is ended by its command line.
            await asyncio.shield(self._interrupt(session, guest))
            if isinstance(error, TimeoutError):
                raise WaypointError(f"command timed out after {timeout_sec} seconds") from None
            raise
        return result.returncode

    async def _interrupt(self, session: str, guest: str) -> None:
        if self.save_final and not self.saved:
            await self.kill_agent()
        marker = f"exec {session} {self.wp_fork} -- bash {guest}/run.sh"
        await self.waypoint.root("pkill", "-TERM", "-f", marker, timeout=30)

    # -- files ------------------------------------------------------------------------------ #

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        if not self.is_agent_environment():
            await super().upload_file(source_path, target_path)
            return
        await self.waypoint(
            "cp", self._require_session(), str(source_path), f"{self.wp_fork}:{target_path}"
        )

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        if not self.is_agent_environment():
            await super().upload_dir(source_dir, target_dir)
            return
        guest_tar = f"{GUEST_SCRATCH}/up-{uuid.uuid4().hex}.tar"
        with tempfile.TemporaryDirectory(prefix="trajlab-wp-") as tmp:
            host_tar = Path(tmp) / "up.tar"
            with tarfile.open(host_tar, "w") as tar:
                tar.add(str(source_dir), arcname=".")
            await self.waypoint(
                "cp", self._require_session(), str(host_tar), f"{self.wp_fork}:{guest_tar}"
            )
        target = shlex.quote(target_dir)
        result = await self.exec(
            f"mkdir -p {target} && tar -xf {guest_tar} -C {target} --no-same-owner; "
            f"rc=$?; rm -f {guest_tar}; exit $rc",
            user="root",
        )
        if result.return_code != 0:
            raise WaypointError(f"upload to {target_dir} failed: {result.stderr}")

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        if not self.is_agent_environment():
            await super().download_file(source_path, target_path)
            return
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="trajlab-wp-") as tmp:
            # Through a temporary file, so the result is owned by this user, not root.
            host = Path(tmp) / "file"
            await self.waypoint(
                "cp", self._require_session(), f"{self.wp_fork}:{source_path}", str(host)
            )
            shutil.copyfile(host, target)
            shutil.copymode(host, target)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if not self.is_agent_environment():
            await super().download_dir(source_dir, target_dir)
            return
        if self._wants_save(source_dir):
            await self.save()
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        guest_tar = f"{GUEST_SCRATCH}/down-{uuid.uuid4().hex}.tar"
        result = await self.exec(
            f"mkdir -p {GUEST_SCRATCH} && tar -cf {guest_tar} -C {shlex.quote(source_dir)} .",
            user="root",
        )
        try:
            if result.return_code != 0:
                raise WaypointError(f"download of {source_dir} failed: {result.stderr}")
            with tempfile.TemporaryDirectory(prefix="trajlab-wp-") as tmp:
                host_tar = Path(tmp) / "down.tar"
                await self.waypoint(
                    "cp", self._require_session(), f"{self.wp_fork}:{guest_tar}", str(host_tar)
                )
                with tarfile.open(host_tar) as tar:
                    for member in tar:
                        try:
                            tar.extract(member, target, filter="data")
                        except tarfile.FilterError as error:
                            # The archive comes from the environment; skip anything that would
                            # land outside the target instead of trusting it.
                            logger.warning("skipped %s: %s", member.name, error)
        finally:
            await self.exec(f"rm -f {guest_tar}", user="root")

    @override
    async def download_dir_filtered(self, *, source_dir: str, **kwargs: Any) -> None:
        if self._wants_save(source_dir):
            await self.save()
        await super().download_dir_filtered(source_dir=source_dir, **kwargs)


class WaypointForkEnvironment(WaypointEnvironment):
    """The agent environment is a copy of a first attempt's save (`state-*` repair arms).

    start()   `waypoint fork <source session> final --id <fork>` inside the trial's own network
              and limits; the copy carries on where the save was made, programs included. The
              failed attempt's /logs is emptied first, as a Docker checkpoint never held it.
              With stop_processes (`state-files`), every program from the save is stopped
              before the agent starts, so only the files remain.
    stop()    `waypoint destroy`; the save itself is never changed.

    Environment kwargs, besides WaypointEnvironment's:
      source_trial    the failed trial's dir, whose trajlab-waypoint.json names the save.
      stop_processes  stop the save's programs before the agent starts.
    """

    def __init__(
        self, *args: Any, source_trial: str, stop_processes: bool | str = False, **kwargs: Any
    ) -> None:
        self.source_trial = Path(source_trial)
        self.stop_processes = as_bool(stop_processes)
        kwargs["save_final"] = False
        super().__init__(*args, **kwargs)

    def source_record(self) -> WaypointRecord:
        path = self.source_trial / WAYPOINT_RECORD_FILENAME
        if not path.is_file():
            raise WaypointError(f"{path} does not exist; the source trial did not run on Waypoint")
        record = WaypointRecord.model_validate_json(path.read_text())
        if record.save is None:
            raise WaypointError(f"{self.source_trial.name} made no save to open")
        if Path(record.sessions_dir) != self.waypoint.sessions_dir:
            raise WaypointError(
                f"the save is under {record.sessions_dir}, this environment uses "
                f"{self.waypoint.sessions_dir}"
            )
        return record

    @override
    async def start(self, force_build: bool) -> None:
        if not self.is_agent_environment():
            await DockerEnvironment.start(self, force_build=force_build)
            return
        check_sessions_dir(self.waypoint.sessions_dir)
        source = self.source_record()
        assert source.save is not None
        self.wp_session = source.session
        self.wp_fork = fork_name(self.session_id)
        await self._read_image_config(source.image)
        await self._open_network()
        await self.waypoint(
            "fork",
            source.session,
            source.save.checkpoint_id,
            "--id",
            self.wp_fork,
            timeout=1800,
            network=self.network_name,
            limits=self.limits,
        )
        logger.info("opened %s/%s as fork %s", source.session, FINAL_CHECKPOINT, self.wp_fork)
        stopped = await self.processes(kill=True) if self.stop_processes else None
        await self._write_record(
            role="fork",
            image=source.image,
            opened_from=f"{source.session}/{source.save.checkpoint_id}",
            stopped=stopped,
        )
        # The failed attempt's logs (its conversation included) are in the save; a repair
        # starts with them gone, as with a Docker checkpoint, whose /logs was a bind mount.
        await self.empty_dirs(self._mount_targets(writable_only=True))
        await self._check_agent_pin()

    async def _check_agent_pin(self) -> None:
        result = await self.exec(ClaudeCode._INSTALL_VERSION_COMMAND, user=self._resolve_user(None))
        match = re.search(r"\d+\.\d+\.\d+", result.stdout or "")
        if result.return_code != 0 or match is None or match.group(0) != CLAUDE_CODE_VERSION:
            raise WaypointError(
                f"the save runs Claude Code {match.group(0) if match else None!r}; "
                f"expected {CLAUDE_CODE_VERSION}"
            )

    @override
    async def stop(self, delete: bool) -> None:
        if not self.is_agent_environment():
            await super().stop(delete)
            return
        try:
            if self.wp_session is not None and delete:
                await self.waypoint("destroy", self.wp_session, self.wp_fork, check=False)
        finally:
            if self.network is not None and delete:
                await delete_network(self.waypoint, self.network)
                self.network = None


def runner_script(guest: str, *, cwd: str | None, user: str | int | None, has_env: bool) -> str:
    """The script the fork's shell runs for one command, like `docker exec ... bash -c`.

    Each command runs in a child bash, so `cd` and `export` do not leak into Waypoint's shell.
    The environment file is read and deleted before the command starts.
    """
    lines = ["#!/bin/bash", "{"]
    if has_env:
        lines.append(f"  . {guest}/env; rm -f {guest}/env")
    if cwd:
        lines.append(f"  cd {shlex.quote(cwd)} || exit 125")
    if user in (None, "root", 0, "0"):
        lines.append(f"  bash {guest}/cmd")
    else:
        name = shlex.quote(str(user))
        lines += [
            f"  chmod 755 {guest}; chmod 644 {guest}/cmd",
            f'  export HOME="$(getent passwd {name} | cut -d: -f6)"',
            f"  runuser -u {name} -- bash {guest}/cmd",
        ]
    lines += [f"}} </dev/null >{guest}/out 2>{guest}/err", ""]
    return "\n".join(lines)


def env_file(env: dict[str, str]) -> str:
    return "".join(
        f"export {key}={shlex.quote(str(value))}\n"
        for key, value in env.items()
        if key.isidentifier()
    )


def as_bool(value: bool | str) -> bool:
    """Harbor passes `--ek key=value` kwargs as strings."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
