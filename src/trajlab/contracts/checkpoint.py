"""The record of one checkpoint, as the watcher writes it to `checkpoints.jsonl` and `.ack`.

Spec: `docs/checkpoint-protocol.md`, "CheckpointRecord".
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class CheckpointRecord(BaseModel):
    """The environment state captured after one state-mutating tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str = Field(min_length=1, description="Image id for docker_commit.")
    # Not trial_id: Harbor writes the trial id only when the trial ends, after every checkpoint
    # is taken (ADR-0006). TrialRecord joins trial_name to trial_id.
    trial_name: str = Field(min_length=1, description="The trial dir name, from config.json.")
    tool_call_id: str = Field(min_length=1, description="Claude Code's tool_use_id; the join key.")
    seq: int = Field(ge=1, description="Capture order within the trial, from 1.")
    tool_name: str = Field(min_length=1)
    # Only one backend and only physical snapshots exist (ADR-0002, ADR-0004); both stay fields
    # so each record carries its own provenance. A new value needs an ADR.
    backend: Literal["docker_commit"] = "docker_commit"
    physical: Literal[True] = True
    capture_ms: int = Field(ge=0, description="Wall time of the snapshot call.")
    bytes: int | None = Field(
        default=None, ge=0, description="Writable-layer size captured, for docker_commit."
    )
    path: str | None = Field(default=None, description="On-disk location, if any.")
    requested_at: AwareDatetime = Field(description="The .req file's mtime.")
    captured_at: AwareDatetime = Field(description="When the snapshot call returned.")
