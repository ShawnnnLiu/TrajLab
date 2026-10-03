import asyncio
from pathlib import Path
from typing import Any

import pytest
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.models.trial.config import TrialConfig

from trajlab.capture.preinstall import agent_spec
from trajlab.capture.repair import (
    DIAGNOSIS_PATH,
    REPAIR_NOTE,
    REPAIR_NOTES,
    REPAIR_PROTOCOL,
    RepairClaudeCode,
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


def test_protocol_keeps_the_note_and_names_the_diagnosis_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def harbor_run(
        self: ClaudeCode, instruction: str, environment: Any, context: Any
    ) -> None:
        seen.append(instruction)

    monkeypatch.setattr(ClaudeCode, "run", harbor_run)
    agent = RepairClaudeCode(
        logs_dir=tmp_path, model_name="anthropic/claude-sonnet-5-5", repair_note=REPAIR_PROTOCOL
    )
    asyncio.run(agent.run("Fix the parser.", environment=None, context=None))  # type: ignore[arg-type]

    (instruction,) = seen
    assert instruction.startswith(REPAIR_NOTE)
    assert instruction.endswith("\n\nFix the parser.")
    assert DIAGNOSIS_PATH in instruction
    assert REPAIR_NOTES == {"naive": REPAIR_NOTE, "protocol": REPAIR_PROTOCOL}
