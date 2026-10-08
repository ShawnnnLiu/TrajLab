import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.trial.config import TrialConfig

from trajlab.capture.preinstall import agent_spec
from trajlab.capture.repair import (
    INSTRUCTION_VAR_PREFIX,
    REPAIR_NOTE,
    RepairClaudeCode,
    file_prompt_read,
    harbor_prompt_read,
    repair_instruction,
)


def test_instruction_starts_with_the_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def harbor_run(
        self: ClaudeCode, instruction: str, environment: Any, context: Any
    ) -> None:
        seen.append(instruction)

    monkeypatch.setattr(ClaudeCode, "run", harbor_run)
    agent = RepairClaudeCode(logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5")
    asyncio.run(agent.run("Fix the parser.", environment=None, context=None))  # type: ignore[arg-type]

    assert seen == [f"{REPAIR_NOTE}\n\nFix the parser."]
    assert agent.name() == ClaudeCode.name()


def test_preinstall_accepts_the_repair_agent() -> None:
    config = TrialConfig.model_validate(
        {
            "task": {"name": "terminal-bench/regex-chess", "ref": "latest"},
            "trial_name": "regex-chess__Ab12Cd3",
            "agent": {
                "import_path": "trajlab.capture.repair:RepairClaudeCode",
                "kwargs": {"version": "2.1.278"},
            },
        }
    )
    assert agent_spec(config).agent_class is RepairClaudeCode


def test_harbor_accepts_the_note_as_an_option_and_keeps_it_off_the_command_line(
    tmp_path: Path,
) -> None:
    kwargs = {
        "version": "2.1.278",
        "repair_note": "A custom note.",
        "repair_transcript": "/srv/trajlab/jobs/_repair-inputs/j/transcript.txt",
    }
    RepairClaudeCode.preflight(kwargs)
    with pytest.raises(ValueError, match="Unknown option 'not_an_option'"):
        RepairClaudeCode.preflight(kwargs | {"not_an_option": 1})
    agent = RepairClaudeCode(logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5", **kwargs)
    plain = ClaudeCode(
        logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5", version="2.1.278"
    )
    assert agent.repair_note == "A custom note."
    assert agent.repair_transcript == kwargs["repair_transcript"]
    assert agent.build_cli_flags() == plain.build_cli_flags()
    assert agent.compile_env_vars() == plain.compile_env_vars()


# With the carriage returns tool outputs carry (CRLF files, progress bars), which must arrive.
TRANSCRIPT = (
    "=== TRANSCRIPT OF THE PREVIOUS ATTEMPT ===\n[user] Fix it.\n"
    "[tool output] 10%\r50%\r100%\nline a\r\nline b\n=== END OF TRANSCRIPT ==="
)


def test_transcript_goes_between_the_note_and_the_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def harbor_run(
        self: ClaudeCode, instruction: str, environment: Any, context: Any
    ) -> None:
        seen.append(instruction)

    monkeypatch.setattr(ClaudeCode, "run", harbor_run)
    transcript = tmp_path / "transcript.txt"
    transcript.write_bytes(TRANSCRIPT.encode())
    agent = RepairClaudeCode(
        logs_dir=tmp_path,
        model_name="anthropic/claude-sonnet-5-5",
        repair_transcript=str(transcript),
    )
    asyncio.run(agent.run("Fix the parser.", environment=None, context=None))  # type: ignore[arg-type]

    assert seen == [f"{REPAIR_NOTE}\n\n{TRANSCRIPT}\n\nFix the parser."]
    assert repair_instruction(REPAIR_NOTE, "Fix the parser.") == f"{REPAIR_NOTE}\n\nFix the parser."


class FakeEnvironment:
    """Records what Harbor's `ClaudeCode.run` asks of the container; runs nothing."""

    default_user: str | int | None = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        self.calls.append(("exec", {"command": command, "env": env or {}, "user": user}))
        if "trajlab-agent-user" in command:
            return ExecResult(stdout="trajlab-agent-user 1000:1001\n", return_code=0)
        return ExecResult(stdout="", return_code=0)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        text = Path(source_path).read_bytes().decode()
        self.calls.append(("upload", {"target": target_path, "text": text}))

    def execs(self) -> list[dict[str, Any]]:
        return [details for kind, details in self.calls if kind == "exec"]


def run_harbor(agent: RepairClaudeCode, environment: FakeEnvironment) -> None:
    """Harbor's own `ClaudeCode.run` (through `RepairClaudeCode.run`), against the fake."""
    asyncio.run(agent.run("Fix the parser.\n", environment, AgentContext()))  # type: ignore[arg-type]


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLAUDE_FORCE_OAUTH", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("no_credentials")
def test_traj_text_prompt_travels_as_a_file_not_an_environment_variable(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.txt"
    transcript.write_bytes(TRANSCRIPT.encode())
    agent = RepairClaudeCode(
        logs_dir=tmp_path,
        model_name="anthropic/claude-sonnet-5-5",
        version="2.1.278",
        repair_transcript=str(transcript),
    )
    environment = FakeEnvironment()
    run_harbor(agent, environment)

    prompt = f"{REPAIR_NOTE}\n\n{TRANSCRIPT}\n\nFix the parser.\n"
    (upload,) = [details for kind, details in environment.calls if kind == "upload"]
    path = upload["target"]
    assert re.fullmatch(r"/tmp/trajlab-prompt-[0-9a-f]{32}\.txt", path)
    assert upload["text"] == prompt
    # No exec carries the prompt in its environment, and the prompt is nowhere on a command line.
    for details in environment.execs():
        assert not any(key.startswith(INSTRUCTION_VAR_PREFIX) for key in details["env"])
        assert TRANSCRIPT not in details["command"]
    # Harbor 0.23.0's command still holds the exact read this agent rewrites; otherwise
    # deliver_by_file raised above. The rewritten command reads the file and removes it before
    # `claude` starts.
    (main,) = [d for d in environment.execs() if "claude --verbose" in d["command"]]
    command = main["command"]
    shell_var = f"harbor_claude_code_instruction_{path.removeprefix('/tmp/trajlab-prompt-')[:-4]}"
    read = (
        f'{shell_var}="$(cat {path}; printf x)"; {shell_var}="${{{shell_var}%x}}"; rm -f {path}; '
    )
    assert read in command
    assert command.index(f"rm -f {path}") < command.index(f'printf "%s" "${shell_var}" | claude ')
    assert INSTRUCTION_VAR_PREFIX not in command
    # The file belongs to the agent's user, who must be able to remove it from sticky /tmp:
    # upload, then ask the agent user's ids, then chown as root, then run.
    order = [kind if kind == "upload" else d["command"] for kind, d in environment.calls]
    lookup = next(i for i, c in enumerate(order) if "trajlab-agent-user" in c)
    chown = order.index(f"set -o pipefail; chown 1000:1001 {path} && chmod 600 {path}")
    assert order.index("upload") < lookup < chown < order.index(command)
    assert environment.execs()[chown - 1]["user"] == "root"
    assert environment.execs()[lookup - 1]["user"] is None


@pytest.mark.usefixtures("no_credentials")
def test_without_a_transcript_harbor_delivers_the_prompt_unchanged(tmp_path: Path) -> None:
    agent = RepairClaudeCode(
        logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5", version="2.1.278"
    )
    environment = FakeEnvironment()
    run_harbor(agent, environment)

    assert not [kind for kind, _ in environment.calls if kind == "upload"]
    (main,) = [d for d in environment.execs() if "claude --verbose" in d["command"]]
    (name,) = [key for key in main["env"] if key.startswith(INSTRUCTION_VAR_PREFIX)]
    assert main["env"][name] == f"{REPAIR_NOTE}\n\nFix the parser.\n"
    assert harbor_prompt_read(name) in main["command"]


def test_a_changed_harbor_command_fails_instead_of_using_the_environment(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.txt"
    transcript.write_bytes(TRANSCRIPT.encode())
    agent = RepairClaudeCode(
        logs_dir=tmp_path,
        model_name="anthropic/claude-sonnet-5-5",
        repair_transcript=str(transcript),
    )
    environment = FakeEnvironment()
    env = {f"{INSTRUCTION_VAR_PREFIX}AB12": "prompt", "IS_SANDBOX": "1"}
    with pytest.raises(RuntimeError, match="cannot deliver it through a file"):
        asyncio.run(agent.exec_as_agent(environment, 'cat "$OTHER" | claude --print', env=env))  # type: ignore[arg-type]
    assert environment.calls == []


@pytest.mark.parametrize(
    "prompt",
    [
        "Fix the parser.\n",
        "ends with blank lines\n\n\n",
        "no newline at the end",
        "quotes \" ' and `backticks` $HOME $(echo no) \\n %s %% * ? [x] ~ -- \t tab",
        "unicode: café — 東京 🙂\n",
    ],
    ids=["newline", "blank-lines", "no-newline", "shell-characters", "unicode"],
)
def test_file_read_gives_the_shell_exactly_what_the_variable_gave(
    tmp_path: Path, prompt: str
) -> None:
    """Runs both reads in a local bash (no Docker): same bytes on stdout, and the file is gone."""
    env_var = f"{INSTRUCTION_VAR_PREFIX}0F1E2D"
    shell_var = env_var.lower()
    pipe = f'printf "%s" "${shell_var}"'
    by_variable = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {harbor_prompt_read(env_var)}{pipe}"],
        env={"PATH": os.environ["PATH"], env_var: prompt},
        capture_output=True,
        check=True,
    ).stdout
    path = tmp_path / "prompt.txt"
    path.write_text(prompt, encoding="utf-8")
    by_file = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {file_prompt_read(env_var, str(path))}{pipe}"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        check=True,
    ).stdout
    assert by_file == by_variable == prompt.encode()
    assert not path.exists()
