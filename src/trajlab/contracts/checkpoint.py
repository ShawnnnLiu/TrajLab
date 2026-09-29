"""The record of one checkpoint, as the watcher writes it to `checkpoints.jsonl` and `.ack`.

Spec: `docs/checkpoint-protocol.md`, "CheckpointRecord".
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class CheckpointRecord(BaseModel):
    """The environment state captured after one state-mutating tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str = Field(min_length=1, description="Image id for docker_commit.")
    trial_id: UUID = Field(description="Harbor's trial id, `id` in result.json.")
    tool_call_id: str = Field(min_length=1, description="Claude Code's tool_use_id; the join key.")
    seq: int = Field(ge=1, description="Capture order within the trial, from 1.")
    tool_name: str = Field(min_length=1)
    # Only one backend and only physical snapshots exist (ADR-0002, ADR-0004); both stay fields
    # so each record carries its own provenance. A new value needs an ADR.
    backend: Literal["docker_commit"] = "docker_commit"
    physical: Literal[True] = True
    capture_ms: int = Field(ge=0)
    bytes: int | None = Field(default=None, ge=0)
    path: str | None = Field(default=None, description="On-disk location, if any.")
    requested_at: AwareDatetime
    captured_at: AwareDatetime
