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
