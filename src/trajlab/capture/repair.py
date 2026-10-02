"""Claude Code with a fixed note before the task instruction, for repair trials.

A repair trial retries a task whose earlier trial failed its verifier. Every repair arm runs this
agent with the same note, so arms differ only in the environment they start from and the
conversation they load, not in what the instruction says. Use it in a job config as

    {"import_path": "trajlab.capture.repair:RepairClaudeCode",
     "kwargs": {"version": "...", "repair_note": "..."}}
"""

from typing import Any, override

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

REPAIR_NOTE = "A previous attempt at this task did not pass the task's tests."


def repair_instruction(note: str, instruction: str) -> str:
    return f"{note}\n\n{instruction}"


class RepairClaudeCode(ClaudeCode):
    """Harbor's Claude Code agent; the instruction it pipes in starts with `repair_note`."""

    def __init__(self, *args: Any, repair_note: str = REPAIR_NOTE, **kwargs: Any) -> None:
        self.repair_note = repair_note
        super().__init__(*args, **kwargs)

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        await super().run(repair_instruction(self.repair_note, instruction), environment, context)
