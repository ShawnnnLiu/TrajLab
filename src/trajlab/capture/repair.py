"""Claude Code with a fixed note before the task instruction, for repair trials.

A repair trial retries a task whose earlier trial failed its verifier. Every repair arm runs this
agent with the same note, so arms differ only in the environment they start from and the
conversation they load, not in what the instruction says. Use it in a job config as

    {"import_path": "trajlab.capture.repair:RepairClaudeCode",
     "kwargs": {"version": "...", "repair_note": "..."}}

The `traj-text` arm also passes `repair_transcript`, the host path of the failed trial rendered
as text (`trajlab.capture.transcript`); its prompt is the note, the transcript, and the
instruction. Harbor hands the prompt to `claude` through one environment variable, which Linux
caps at 128 KiB, so this agent delivers every `traj-text` prompt through a file instead
(`exec_as_agent`; docs/upstream-notes.md).
"""

import re
import tempfile
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.claude_code import ClaudeCode, ClaudeCodeOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

REPAIR_NOTE = "A previous attempt at this task did not pass the task's tests."

# Round 2's note (ADR-0012 amendment of 2026-10-03, round 2): the same opening line, then a fixed
# diagnose, fix, verify protocol. /logs/agent is the trial's agent dir on the host, so the
# diagnosis is captured with the trial and stays out of the task's own files.
DIAGNOSIS_PATH = "/logs/agent/diagnosis.md"
REPAIR_PROTOCOL = f"""\
{REPAIR_NOTE} The tests are not visible to you and may check cases beyond the examples \
in the environment.

Work in this order:
1. Diagnose. From whatever evidence you have (the files in the environment, and your earlier \
work on this task if you can see it), work out the most likely reasons the previous attempt \
failed. Before changing any other file, write them to {DIAGNOSIS_PATH}: each suspected \
cause, the evidence for it, and how you will check it.
2. List every requirement in the task, each with a check you can run that would fail if the \
requirement were not met. Include inputs other than the ones provided.
3. Fix, run the checks, and repeat until they all pass.
4. Before finishing, run every check again. If you would have to say a requirement is \
untested, test it instead."""
REPAIR_NOTES = {"naive": REPAIR_NOTE, "protocol": REPAIR_PROTOCOL}

# Harbor's `ClaudeCode.run` puts the prompt in an environment variable named with this prefix
# and a random hex suffix, copies it into a shell variable of the same name in lower case, and
# pipes that into `claude`. The variable is one `docker exec -e` argument.
INSTRUCTION_VAR_PREFIX = "HARBOR_CLAUDE_CODE_INSTRUCTION_"
PROMPT_FILE = "/tmp/trajlab-prompt-{}.txt"


def repair_instruction(note: str, instruction: str, transcript: str | None = None) -> str:
    """The note, the transcript if there is one, and the instruction, apart by blank lines."""
    if transcript is None:
        return f"{note}\n\n{instruction}"
    return f"{note}\n\n{transcript}\n\n{instruction}"


def harbor_prompt_read(env_var: str) -> str:
    """The part of Harbor's command that reads the prompt from `env_var`."""
    shell_var = env_var.lower()
    return f'{shell_var}="${env_var}"; unset {env_var}; '


def file_prompt_read(env_var: str, path: str) -> str:
    """The same read from the file at `path`, which it then removes, before `claude` starts.

    `$(...)` drops trailing newlines, so a sentinel `x` is appended and then stripped: the shell
    variable holds exactly what the environment variable would have held.
    """
    shell_var = env_var.lower()
    return (
        f'{shell_var}="$(cat {path}; printf x)"; {shell_var}="${{{shell_var}%x}}"; rm -f {path}; '
    )


def deliver_by_file(command: str, env_var: str, path: str) -> str:
    """Harbor's run command with the prompt read from `path` instead of `env_var`.

    Raises if the command does not read the prompt exactly as Harbor 0.23.0 does, so a changed
    Harbor fails loudly instead of sending the prompt through the environment again.
    """
    harbor_read = harbor_prompt_read(env_var)
    if command.count(harbor_read) != 1:
        raise RuntimeError(
            f"Harbor's claude command does not read the prompt as {harbor_read!r}; "
            "trajlab.capture.repair cannot deliver it through a file"
        )
    return command.replace(harbor_read, file_prompt_read(env_var, path))


class RepairClaudeCodeOptions(ClaudeCodeOptions):
    """Claude Code's options plus the note and the transcript. Harbor validates job kwargs
    against this model before a job starts and rejects unknown ones; with no `Cli` or `Env`
    annotation, neither option reaches Claude Code's command line or environment."""

    repair_note: str = REPAIR_NOTE
    repair_transcript: str | None = None


class RepairClaudeCode(ClaudeCode):
    """Harbor's Claude Code agent; the instruction it pipes in starts with `repair_note`."""

    options_model = RepairClaudeCodeOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        assert isinstance(self.options, RepairClaudeCodeOptions)
        self.repair_note = self.options.repair_note
        self.repair_transcript = self.options.repair_transcript

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        transcript = None
        if self.repair_transcript is not None:
            # Bytes, not read_text: universal newlines would turn the CRs that tool outputs carry
            # (CRLF files, progress bars) into LFs.
            transcript = Path(self.repair_transcript).read_bytes().decode("utf-8")
        prompt = repair_instruction(self.repair_note, instruction, transcript)
        await super().run(prompt, environment, context)

    @override
    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        """With a transcript, deliver the prompt as a file rather than an environment variable.

        Applies to every `traj-text` run, whatever the prompt's size, so the arm has one
        delivery path. The agent still receives the prompt on stdin as its first user message.
        """
        names = [key for key in env or {} if key.startswith(INSTRUCTION_VAR_PREFIX)]
        if self.repair_transcript is not None and env is not None and names:
            (name,) = names
            path = PROMPT_FILE.format(name.removeprefix(INSTRUCTION_VAR_PREFIX).lower())
            command = deliver_by_file(command, name, path)
            await self.upload_prompt(environment, env[name], path)
            env = {key: value for key, value in env.items() if key != name}
        return await super().exec_as_agent(
            environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec
        )

    async def upload_prompt(self, environment: BaseEnvironment, prompt: str, path: str) -> None:
        """Copy the prompt to `path` in the container, owned by the agent's user, mode 600.

        Harbor's Docker `upload_file` (`docker compose cp`) keeps the host user's uid and gid,
        and its tar fallback makes the file root's; neither is in general the agent user, and
        /tmp is sticky, so without the chown the agent could not remove the file. The agent
        user is whoever `exec_as_agent` runs as (the task's agent user, else the image's default
        user), so ask the container.
        """
        with tempfile.TemporaryDirectory(prefix="trajlab-prompt-") as directory:
            local = Path(directory) / "prompt.txt"
            local.write_bytes(prompt.encode("utf-8"))
            await environment.upload_file(local, path)
        found = await super().exec_as_agent(
            environment, command='echo "trajlab-agent-user $(id -u):$(id -g)"'
        )
        ids = re.search(r"trajlab-agent-user (\d+):(\d+)", found.stdout or "")
        if ids is None:
            raise RuntimeError(f"cannot tell the agent user's uid and gid from {found.stdout!r}")
        await self.exec_as_root(
            environment, command=f"chown {ids[1]}:{ids[2]} {path} && chmod 600 {path}"
        )
