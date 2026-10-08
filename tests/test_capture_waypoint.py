import asyncio
import json
import os
import subprocess
import tarfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from harbor.models.task.config import TaskConfig
from harbor.models.trial.paths import TrialPaths

from tests.conftest import FIXTURE_TRIAL_NAME, assemble_job_dir
from trajlab.capture import waypoint as wp
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.capture.preinstall import Docker, PreinstalledDockerEnvironment
from trajlab.capture.repair_launcher import (
    INPUTS_DIRNAME,
    Launcher,
    default_arms,
    repair_job_config,
    save_keep_reason,
)
from trajlab.capture.waypoint import (
    GUEST_PROCESSES,
    KILL_AGENT,
    CommandResult,
    Limits,
    Network,
    Waypoint,
    WaypointEnvironment,
    WaypointError,
    WaypointForkEnvironment,
    check_sessions_dir,
    create_network,
    fork_name,
    nameservers,
    parse_processes,
    runner_script,
)
from trajlab.contracts import (
    PREINSTALL_RECORD_FILENAME,
    REPAIR_ARMS,
    REPAIR_SOURCE_FILENAME,
    WAYPOINT_ARMS,
    WAYPOINT_RECORD_FILENAME,
    PreinstallRecord,
    RepairSource,
    RunningProcess,
    WaypointRecord,
    WaypointSave,
)

SESSION = "0123456789abcdef"
# Never touched: every host command goes to FakeHost. Short, as Waypoint's socket path requires.
STATE = Path("/srv/tl-test")
IMAGE = "trajlab-preinstalled:regex-chess-claude-code-x"
BINARY_SHA256 = "ab" * 32
LEFTOVER = "17\tpython3 -m http.server 8000\n"


def strip_wrappers(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    """The command under sudo, systemd-run, nsenter, and env, and what wrapped it."""
    seen: dict[str, Any] = {}
    if argv[:2] == ["sudo", "-n"]:
        argv = argv[2:]
    if argv and argv[0] == "systemd-run":
        end = argv.index("--collect") + 1
        while end < len(argv) and argv[end] == "-p":
            end += 2
        seen["scope"] = argv[1:end]
        argv = argv[end:]
    if argv and argv[0] == "nsenter":
        seen["network"] = argv[1].removeprefix("--net=/run/netns/")
        argv = argv[2:]
    if argv and argv[0] == "env":
        argv = [a for a in argv[1:] if not a.startswith("WAYPOINT_")]
        argv = argv[1:]  # the waypoint binary
        seen["waypoint"] = True
    return argv, seen


class FakeHost:
    """Waypoint, ip, and the other root commands, in memory. Records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.staged: dict[str, str] = {}  # guest scratch dir -> the command it runs
        self.taken_networks: set[str] = set()
        self.exec_error: BaseException | None = None
        self.killed_agent: list[int] = []

    def waypoint_calls(self, name: str) -> list[tuple[list[str], dict[str, Any]]]:
        return [(a, s) for a, s in self.calls if s.get("waypoint") and a[:1] == [name]]

    def output_for(self, command: str) -> str:
        if "PPid" in command:
            return LEFTOVER
        if "claude --version" in command:
            return f"{CLAUDE_CODE_VERSION} (Claude Code)\n"
        if "sha256sum" in command:
            return BINARY_SHA256 + "\n"
        return ""

    async def __call__(self, argv: list[str], timeout: float | None, stdin: str | None):
        args, seen = strip_wrappers(argv)
        self.calls.append((args, seen))
        if seen.get("waypoint"):
            return self.waypoint(args)
        match args:
            case ["ip", "netns", "add", name]:
                if name in self.taken_networks:
                    return CommandResult(1, "", "File exists")
                self.taken_networks.add(name)
            case ["python3", "-I", "-c", _, _]:
                return CommandResult(0, json.dumps(self.killed_agent), "")
            case ["du", "-sbL", path]:
                return CommandResult(0, f"4096\t{path}\n", "")
            case ["grep", *_]:
                return CommandResult(1, "", "")
            case ["waypoint", "version"]:
                return CommandResult(0, "waypoint version v0.7.0\n", "")
        return CommandResult(0, "", "")

    def waypoint(self, args: list[str]) -> CommandResult:
        match args:
            case ["init", _, "--quiet", "--shell"]:
                return CommandResult(0, f"{SESSION},/srv/x/sessions/{SESSION}/work\n", "")
            case ["cp", _, source, target] if ":" in target and not target.startswith("/"):
                guest = target.split(":", 1)[1]
                cmd = Path(source) / "cmd"
                if cmd.is_file():
                    self.staged[guest] = cmd.read_text()
            case ["cp", _, source, target]:
                guest = source.split(":", 1)[1]
                if guest.endswith(".tar"):
                    member = Path(target).with_name("trajectory.json")
                    member.write_text("{}")
                    with tarfile.open(target, "w") as tar:
                        tar.add(member, arcname="./trajectory.json")
                    return CommandResult(0, "", "")
                scratch, _, stream = guest.rpartition("/")
                text = self.output_for(self.staged.get(scratch, "")) if stream == "out" else ""
                Path(target).write_text(text)
            case ["exec", _, _, "--", command] if command.endswith("run.sh"):
                if self.exec_error is not None:
                    raise self.exec_error
        return CommandResult(0, "", "")


def make_task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "environment" / "Dockerfile").write_text("FROM debian:bookworm\n")
    (task / "task.toml").write_text(
        'version = "1.0"\n[environment]\ndocker_image = "regex-chess:1"\ncpus = 2\n'
        "memory_mb = 4096\n"
    )
    return task


class FakeDocker:
    async def __call__(self, args: list[str], timeout: float | None) -> Any:
        from trajlab.capture.preinstall import CommandResult as DockerResult

        config = {"Env": ["PATH=/opt/bin:/usr/bin", "LANG=C.UTF-8"], "WorkingDir": "/app"}
        return DockerResult(0, json.dumps(config), "")


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    fake = FakeHost()
    monkeypatch.setattr(WaypointEnvironment, "waypoint_runner", fake)
    monkeypatch.setattr(PreinstalledDockerEnvironment, "docker", Docker(runner=FakeDocker()))
    monkeypatch.setattr(wp, "root_prefix", lambda: ["sudo", "-n"])
    return fake


def make_env(
    tmp_path: Path, cls: type[WaypointEnvironment] = WaypointEnvironment, **kwargs: Any
) -> WaypointEnvironment:
    task = make_task(tmp_path)
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    paths.config_path.write_text(
        json.dumps(
            {
                "task": {"name": "terminal-bench/regex-chess", "ref": "latest"},
                "trial_name": "regex-chess__Ab12Cd3",
                "agent": {"name": "claude-code", "kwargs": {"version": CLAUDE_CODE_VERSION}},
            }
        )
    )
    task_config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
    return cls(
        environment_dir=task / "environment",
        environment_name="regex-chess",
        session_id="regex-chess__Ab12Cd3__env",
        trial_paths=paths,
        task_env_config=task_config.environment,
        mounts=[
            {"type": "bind", "source": str(paths.agent_dir), "target": "/logs/agent"},
            {"type": "bind", "source": str(paths.verifier_dir), "target": "/logs/verifier"},
        ],
        state_dir=str(STATE),
        waypoint_bin="waypoint",
        **kwargs,
    )


def preinstall_record() -> PreinstallRecord:
    return PreinstallRecord(
        task_image="regex-chess:1",
        task_image_id="sha256:task",
        task_content_key="sha256:key",
        image=IMAGE,
        image_id="sha256:" + "c" * 64,
        agent_name="claude-code",
        agent_version=CLAUDE_CODE_VERSION,
        agent_sha256=BINARY_SHA256,
        harbor_version="0.23.0",
        recipe_version=2,
        cache_hit=True,
        build_seconds=None,
        recorded_at=datetime.now(UTC),
    )


def started_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WaypointEnvironment:
    env = make_env(tmp_path)

    async def prepare_image(force_build: bool) -> PreinstallRecord:
        return preinstall_record()

    monkeypatch.setattr(env, "prepare_image", prepare_image)
    asyncio.run(env.start(force_build=False))
    return env


# -- the source environment --------------------------------------------------------------------- #


def test_start_inits_a_session_in_its_own_network_and_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    env = started_source(tmp_path, monkeypatch)
    assert not env.capabilities.mounted
    [(args, seen)] = host.waypoint_calls("init")
    assert args[1] == str(STATE / "rootfs" / ("c" * 16))
    assert seen["network"] == env.network_name and seen["network"].startswith("tlwp-")
    assert seen["scope"][-4:] == ["-p", "CPUQuota=200%", "-p", "MemoryMax=4096M"]
    record = WaypointRecord.model_validate_json(
        (env.trial_paths.trial_dir / WAYPOINT_RECORD_FILENAME).read_text()
    )
    assert (record.role, record.session, record.fork, record.save) == (
        "source",
        SESSION,
        "main",
        None,
    )
    assert record.image == IMAGE and record.network == env.network_name
    assert (env.trial_paths.trial_dir / PREINSTALL_RECORD_FILENAME).is_file()
    assert any("/etc/resolv.conf" in command for command in host.staged.values())


def test_exec_sends_command_and_environment_as_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    env = started_source(tmp_path, monkeypatch)
    result = asyncio.run(env.exec("claude --version", env={"ANTHROPIC_API_KEY": "sk-ant-x"}))
    assert result.return_code == 0 and CLAUDE_CODE_VERSION in (result.stdout or "")
    for args, _ in host.calls:
        assert not any("sk-ant-x" in arg for arg in args), args


def test_runner_script_applies_cwd_env_and_user() -> None:
    root = runner_script("/run/x/1", cwd="/app", user=None, has_env=True)
    assert ". /run/x/1/env; rm -f /run/x/1/env" in root
    assert "cd /app || exit 125" in root and "  bash /run/x/1/cmd" in root
    other = runner_script("/run/x/1", cwd=None, user="agent", has_env=False)
    assert "runuser -u agent -- bash /run/x/1/cmd" in other


def test_download_of_agent_logs_saves_first_then_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    env = started_source(tmp_path, monkeypatch)
    asyncio.run(env.download_dir("/logs/verifier", tmp_path / "v"))
    assert not host.waypoint_calls("snapshot")
    asyncio.run(env.download_dir("/logs/agent", tmp_path / "agent"))
    asyncio.run(env.download_dir("/logs/agent", tmp_path / "agent"))
    [(args, seen)] = host.waypoint_calls("snapshot")
    assert args == ["snapshot", SESSION, "main", "final"]
    assert seen["network"] == env.network_name and "scope" in seen
    assert (tmp_path / "agent" / "trajectory.json").is_file()
    record = WaypointRecord.model_validate_json(
        (env.trial_paths.trial_dir / WAYPOINT_RECORD_FILENAME).read_text()
    )
    assert record.save is not None
    assert record.save.agent_stopped == "ended"
    assert record.save.processes == (RunningProcess(pid=17, command="python3 -m http.server 8000"),)
    assert record.save.files_bytes == 4096 and record.save.memory_bytes == 4096


def test_save_after_timeout_kills_the_agent_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    env = started_source(tmp_path, monkeypatch)
    host.killed_agent = [4242]
    asyncio.run(env.download_dir("/logs/agent", tmp_path / "agent"))
    record = WaypointRecord.model_validate_json(
        (env.trial_paths.trial_dir / WAYPOINT_RECORD_FILENAME).read_text()
    )
    assert record.save is not None
    assert record.save.agent_stopped == "timeout_or_error"
    assert record.save.killed_agent_pids == (4242,)


def test_timed_out_command_kills_agent_then_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    env = started_source(tmp_path, monkeypatch)
    host.exec_error = TimeoutError()
    with pytest.raises(WaypointError, match="timed out"):
        asyncio.run(env.exec("claude -p hi", timeout_sec=5))
    names = [args[0] for args, _ in host.calls[-6:]]
    assert names.index("python3") < names.index("pkill")


@pytest.mark.parametrize(("saved", "expected"), [(True, "suspend"), (False, "cleanup")])
def test_stop_keeps_a_save_and_removes_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost, saved: bool, expected: str
) -> None:
    env = started_source(tmp_path, monkeypatch)
    if saved:
        asyncio.run(env.download_dir("/logs/agent", tmp_path / "agent"))
    network = env.network_name
    asyncio.run(env.stop(delete=True))
    assert host.waypoint_calls(expected)
    assert not host.waypoint_calls("cleanup" if saved else "suspend")
    assert any(a[:2] == ["sh", "-c"] and f"ip netns del {network}" in a[2] for a, _ in host.calls)


def test_repair_trial_on_task_image_makes_no_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    env = make_env(tmp_path, save_final="false")

    async def prepare_image(force_build: bool) -> PreinstallRecord:
        return preinstall_record()

    monkeypatch.setattr(env, "prepare_image", prepare_image)
    asyncio.run(env.start(force_build=False))
    asyncio.run(env.download_dir("/logs/agent", tmp_path / "agent"))
    asyncio.run(env.stop(delete=True))
    assert not host.waypoint_calls("snapshot")
    assert host.waypoint_calls("cleanup")


def test_task_with_other_services_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
) -> None:
    env = make_env(tmp_path)
    (env.environment_dir / "docker-compose.yaml").write_text(
        "services:\n  main: {}\n  db: {image: postgres}\n"
    )
    with pytest.raises(WaypointError, match="one container"):
        asyncio.run(env.start(force_build=False))


# -- the fork environment ----------------------------------------------------------------------- #


def write_source_record(trial_dir: Path, sessions_dir: Path, *, saved: bool = True) -> None:
    save = WaypointSave(
        path=str(sessions_dir / SESSION / "checkpoints" / "final"),
        agent_stopped="ended",
        processes=(RunningProcess(pid=17, command="python3 -m http.server 8000"),),
        capture_ms=900,
        saved_at=datetime.now(UTC),
    )
    record = WaypointRecord(
        role="source",
        waypoint_version="v0.7.0",
        sessions_dir=str(sessions_dir),
        session=SESSION,
        fork="main",
        image=IMAGE,
        save=save if saved else None,
        recorded_at=datetime.now(UTC),
    )
    (trial_dir / WAYPOINT_RECORD_FILENAME).write_text(record.model_dump_json())


@pytest.mark.parametrize("stop_processes", [False, True])
def test_fork_opens_the_save_in_its_own_network(
    tmp_path: Path, host: FakeHost, stop_processes: bool
) -> None:
    source = tmp_path / "source-trial"
    source.mkdir()
    write_source_record(source, STATE / "sessions")
    env = make_env(
        tmp_path,
        WaypointForkEnvironment,
        source_trial=str(source),
        stop_processes=str(stop_processes).lower(),
    )
    asyncio.run(env.start(force_build=False))
    [(args, seen)] = host.waypoint_calls("fork")
    fork = fork_name("regex-chess__Ab12Cd3__env")
    assert args == ["fork", SESSION, "final", "--id", fork]
    assert seen["network"] == env.network_name
    kills = [c for c in host.staged.values() if "PPid" in c and c.startswith("KILL=1")]
    assert len(kills) == int(stop_processes)
    # The failed attempt's logs, its conversation included, are emptied before the agent runs.
    assert any("/logs/agent" in c and "rm -rf" in c for c in host.staged.values())
    record = WaypointRecord.model_validate_json(
        (env.trial_paths.trial_dir / WAYPOINT_RECORD_FILENAME).read_text()
    )
    assert record.role == "fork" and record.fork == fork
    assert record.opened_from == f"{SESSION}/final"
    expected = (RunningProcess(pid=17, command="python3 -m http.server 8000"),)
    assert record.stopped_processes == (expected if stop_processes else None)
    asyncio.run(env.download_dir("/logs/agent", tmp_path / "agent"))
    asyncio.run(env.stop(delete=True))
    assert not host.waypoint_calls("snapshot")
    assert host.waypoint_calls("destroy") and not host.waypoint_calls("cleanup")


def test_fork_refuses_a_trial_without_a_save(tmp_path: Path, host: FakeHost) -> None:
    source = tmp_path / "source-trial"
    source.mkdir()
    write_source_record(source, STATE / "sessions", saved=False)
    env = make_env(tmp_path, WaypointForkEnvironment, source_trial=str(source))
    with pytest.raises(WaypointError, match="no save"):
        asyncio.run(env.start(force_build=False))


# -- helpers ------------------------------------------------------------------------------------ #


def test_network_addresses_and_scripts() -> None:
    assert (Network(0).host_ip, Network(0).guest_ip, Network(0).subnet) == (
        "10.213.0.1",
        "10.213.0.2",
        "10.213.0.0/30",
    )
    assert Network(64).host_ip == "10.213.1.1"
    assert Network(16383).guest_ip == "10.213.255.254"
    create, delete = Network(5).create_script(), Network(5).delete_script()
    assert "ip -n tlwp-5 route add default via 10.213.0.21" in create
    assert "-t nat -A POSTROUTING -s 10.213.0.20/30 ! -o tlwp5h -j MASQUERADE" in create
    assert "-t nat -D POSTROUTING -s 10.213.0.20/30 ! -o tlwp5h -j MASQUERADE" in delete
    assert "ip netns del tlwp-5" in delete
    assert len(Network(16383).host_link) <= 15


def test_create_network_takes_the_next_free_index(host: FakeHost) -> None:
    first = asyncio.run(create_network(Waypoint(Path("/w"), runner=host), "same-key"))
    second = asyncio.run(create_network(Waypoint(Path("/w"), runner=host), "same-key"))
    assert second.index == (first.index + 1) % wp.NETWORK_COUNT


def test_waypoint_argv_wraps_in_scope_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp, "root_prefix", lambda: ["sudo", "-n"])
    argv = Waypoint(Path("/srv/w"), "waypoint").argv(
        "fork", "s", "final", network="tlwp-3", limits=Limits(cpus=4, memory_mb=8192)
    )
    assert argv == [
        "sudo", "-n",
        "systemd-run", "--scope", "--quiet", "--collect",
        "-p", "CPUQuota=400%", "-p", "MemoryMax=8192M",
        "nsenter", "--net=/run/netns/tlwp-3",
        "env", "WAYPOINT_SESSIONS_DIR=/srv/w/sessions", "WAYPOINT_SESSION_INFO_DIR=/srv/w/info",
        "waypoint", "fork", "s", "final",
    ]  # fmt: skip


def test_sessions_dir_must_fit_the_socket_path() -> None:
    check_sessions_dir(Path("/srv/trajlab/waypoint/sessions"))
    with pytest.raises(WaypointError, match="socket path"):
        check_sessions_dir(Path("/srv/" + "x" * 60))
    with pytest.raises(WaypointError, match="OverlayFS"):
        check_sessions_dir(Path("/srv/a:b"))


def test_nameservers_skip_loopback(tmp_path: Path) -> None:
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\nnameserver 172.31.0.2\nsearch ec2.internal\n")
    assert nameservers([resolv]) == ["172.31.0.2"]


def test_fork_name_is_a_valid_waypoint_id() -> None:
    name = fork_name("very-long-task-name-" * 5 + "__Ab12Cd3__env")
    assert len(name) <= 64 and name[0].isalnum()
    assert all(c.isalnum() or c in "._-" for c in name)
    assert fork_name("a__x__env") != fork_name("a__y__env")


def test_guest_process_scan_skips_its_own_chain() -> None:
    """Runs the listing (never the kill) on this machine's /proc."""
    done = subprocess.run(["bash", "-c", GUEST_PROCESSES], capture_output=True, text=True)
    assert done.returncode == 0
    processes = parse_processes(done.stdout)
    assert processes and os.getpid() not in {p.pid for p in processes}


@pytest.fixture
def agent_tree(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], Path]]:
    """A parent with two children: one named `claude`, one standing in for a server."""
    parent = subprocess.Popen(
        ["bash", "-c", "(exec -a claude sleep 60) & (exec -a server sleep 60) & wait"]
    )
    fork_json = tmp_path / "fork.json"
    fork_json.write_text(json.dumps({"pid": parent.pid}))
    time.sleep(0.5)
    yield parent, fork_json
    subprocess.run(["pkill", "-P", str(parent.pid)])
    parent.kill()
    parent.wait()


def children(pid: int) -> dict[int, str]:
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
            argv0 = (entry / "cmdline").read_bytes().split(b"\0")[0].decode()
        except OSError:
            continue
        if f"\nPPid:\t{pid}\n" in status:
            found[int(entry.name)] = argv0
    return found


def test_kill_agent_kills_claude_and_leaves_the_rest(
    agent_tree: tuple[subprocess.Popen[bytes], Path],
) -> None:
    parent, fork_json = agent_tree
    before = children(parent.pid)
    assert sorted(before.values()) == ["claude", "server"]
    done = subprocess.run(
        ["python3", "-I", "-c", KILL_AGENT, str(fork_json)], capture_output=True, text=True
    )
    killed = json.loads(done.stdout)
    claude = [pid for pid, name in before.items() if name == "claude"]
    assert killed == claude
    time.sleep(0.3)
    assert "server" in children(parent.pid).values()


# -- the launcher and the contracts -------------------------------------------------------------- #


def waypoint_source_config() -> dict[str, Any]:
    return {
        "job_name": "tb40-sonnet-wp1",
        "jobs_dir": "corpus/jobs",
        "n_attempts": 3,
        "n_concurrent_trials": 6,
        "agents": [
            {
                "name": "claude-code",
                "model_name": "anthropic/claude-sonnet-5-5",
                "kwargs": {"version": CLAUDE_CODE_VERSION},
            }
        ],
        "datasets": [
            {
                "name": "terminal-bench/terminal-bench",
                "ref": "4.0.0",
                "task_names": ["terminal-bench/regex-chess"],
            }
        ],
        "environment": {
            "import_path": "trajlab.capture.waypoint:WaypointEnvironment",
            "kwargs": {"state_dir": "/srv/trajlab/waypoint"},
        },
    }


@pytest.mark.parametrize("arm", WAYPOINT_ARMS)
def test_waypoint_repair_jobs_run_on_waypoint_without_hooks(arm: str, tmp_path: Path) -> None:
    config = repair_job_config(
        waypoint_source_config(),
        task_name="terminal-bench/regex-chess",
        arm=arm,  # type: ignore[arg-type]
        job_name=f"rep-{arm}",
        jobs_dir=tmp_path,
        attempts=3,
        checkpoint_image=None,
        session_file=tmp_path / "s.jsonl" if arm in ("traj", "state-live-traj") else None,
        source_trial=tmp_path / "trial",
    )
    agent, environment = config["agents"][0], config["environment"]
    assert "config" not in agent["kwargs"]
    assert environment["kwargs"]["state_dir"] == "/srv/trajlab/waypoint"
    if arm in ("fresh", "traj"):
        assert environment["import_path"].endswith(":WaypointEnvironment")
        assert environment["kwargs"]["save_final"] is False
    else:
        assert environment["import_path"].endswith(":WaypointForkEnvironment")
        assert environment["kwargs"]["source_trial"] == str((tmp_path / "trial").resolve())
        assert environment["kwargs"]["stop_processes"] is (arm == "state-files")


def test_docker_and_waypoint_arms_do_not_mix(tmp_path: Path) -> None:
    common: dict[str, Any] = {
        "task_name": "terminal-bench/regex-chess",
        "job_name": "rep",
        "jobs_dir": tmp_path,
        "attempts": 3,
        "session_file": None,
        "source_trial": tmp_path,
    }
    with pytest.raises(ValueError, match="Docker checkpoint"):
        repair_job_config(
            waypoint_source_config(), arm="state", checkpoint_image="trajlab-checkpoint:x", **common
        )
    docker = waypoint_source_config() | {"environment": {"import_path": "x:Docker"}}
    with pytest.raises(ValueError, match="Waypoint save"):
        repair_job_config(docker, arm="state-live", checkpoint_image=None, **common)


def make_waypoint_job(tmp_path: Path, *, saved: bool = True) -> Path:
    job = assemble_job_dir(tmp_path / "jobs")
    config = json.loads((job / "config.json").read_text())
    config["environment"] = waypoint_source_config()["environment"]
    (job / "config.json").write_text(json.dumps(config))
    trial = job / FIXTURE_TRIAL_NAME
    result = json.loads((trial / "result.json").read_text())
    result["verifier_result"] = {"rewards": {"reward": 0.0}}
    result["exception_info"] = None
    (trial / "result.json").write_text(json.dumps(result))
    write_source_record(trial, STATE / "sessions", saved=saved)
    return job


def test_waypoint_round_plans_its_arms_on_the_save(tmp_path: Path) -> None:
    job = make_waypoint_job(tmp_path)
    assert default_arms([job]) == WAYPOINT_ARMS
    runner = Launcher(
        source_jobs=[job],
        prefix="wp1",
        attempts=3,
        max_running=6,
        jobs_dir=job.parent,
        manifests_dir=tmp_path / "manifests",
        env_file=None,
        arms=WAYPOINT_ARMS,
        round_arms=WAYPOINT_ARMS,
        watcher_running=lambda _: False,
    )
    assert not runner.reuses_draw
    assert runner.status_path.name == "wp1.status.json"
    trial = job / FIXTURE_TRIAL_NAME
    from trajlab.capture.repair_launcher import load_result

    result = load_result(trial)
    assert result is not None
    jobs = runner.plan_failure(trial, result)
    assert isinstance(jobs, list) and [j.source.arm for j in jobs] == list(WAYPOINT_ARMS)
    sources = {j.source.arm: j.source for j in jobs}
    assert sources["state-live"].waypoint_save == f"{SESSION}/final"
    assert sources["fresh"].waypoint_save is None
    # No watcher is needed for a Waypoint round.
    monkey_free = runner.hold_reason()
    assert monkey_free is None or "watcher" not in monkey_free


def test_failure_without_a_save_is_not_repaired(tmp_path: Path) -> None:
    job = make_waypoint_job(tmp_path, saved=False)
    runner = Launcher(
        source_jobs=[job],
        prefix="wp1",
        attempts=3,
        max_running=6,
        jobs_dir=job.parent,
        manifests_dir=tmp_path / "manifests",
        env_file=None,
        arms=WAYPOINT_ARMS,
        round_arms=WAYPOINT_ARMS,
    )
    from trajlab.capture.repair_launcher import load_result

    trial = job / FIXTURE_TRIAL_NAME
    result = load_result(trial)
    assert result is not None
    assert runner.plan_failure(trial, result) == "no Waypoint save"


def test_docker_source_keeps_the_original_arms(tmp_path: Path) -> None:
    job = assemble_job_dir(tmp_path / "jobs")
    assert default_arms([job]) == REPAIR_ARMS


def test_save_is_kept_until_its_repairs_finish(tmp_path: Path) -> None:
    job = make_waypoint_job(tmp_path)
    trial = job / FIXTURE_TRIAL_NAME
    jobs_dir = job.parent
    assert "has not been drawn" in (save_keep_reason(trial, jobs_dir, "wp1") or "")
    inputs = jobs_dir / INPUTS_DIRNAME
    repair = inputs / f"wp1-{FIXTURE_TRIAL_NAME}-state-live"
    repair.mkdir(parents=True)
    source = RepairSource(
        source_job=job.name,
        source_trial=FIXTURE_TRIAL_NAME,
        task_name="hello-world/hello-world",
        failure_kind="ended_turn",
        arm="state-live",
        attempts=3,
        waypoint_save=f"{SESSION}/final",
        recorded_at=datetime.now(UTC),
    )
    (repair / REPAIR_SOURCE_FILENAME).write_text(source.model_dump_json())
    assert "repairs not finished" in (save_keep_reason(trial, jobs_dir, "wp1") or "")
    finished = jobs_dir / repair.name
    finished.mkdir()
    (finished / "result.json").write_text(
        json.dumps(
            {
                "id": "00000000-0000-0000-0000-000000000000",
                "started_at": "2026-10-08T00:00:00Z",
                "finished_at": "2026-10-08T01:00:00Z",
                "n_total_trials": 3,
                "stats": {},
            }
        )
    )
    assert save_keep_reason(trial, jobs_dir, "wp1") is None


def test_contracts_tie_fields_to_roles_and_arms() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="opened_from"):
        WaypointRecord(
            role="fork",
            waypoint_version="v0.7.0",
            sessions_dir="/s",
            session=SESSION,
            fork="r1",
            image=IMAGE,
            recorded_at=now,
        )
    with pytest.raises(ValueError, match="waypoint_save"):
        RepairSource(
            source_job="j",
            source_trial="t",
            task_name="x",
            failure_kind="ended_turn",
            arm="state-live",
            attempts=3,
            recorded_at=now,
        )
    record = WaypointRecord(
        role="source",
        waypoint_version="v0.7.0",
        sessions_dir="/s",
        session=SESSION,
        fork="main",
        image=IMAGE,
        recorded_at=now,
    )
    assert WaypointRecord.model_validate_json(record.model_dump_json()) == record


def test_waypoint_report_shows_the_save(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trajlab.cli import app

    job = make_waypoint_job(tmp_path)
    result = CliRunner().invoke(app, ["waypoint-report", str(job)])
    assert result.exit_code == 0, result.output
    [line] = [x for x in result.output.splitlines() if x.startswith(FIXTURE_TRIAL_NAME)]
    assert "\t0.0\t-\tfinal\tended\t1 (python3)\t" in line
