"""The record of one checkpoint, as the watcher writes it to `checkpoints.jsonl` and `.ack`,
and the policy a trial's checkpoints were taken under.

Spec: `docs/checkpoint-protocol.md`, "CheckpointRecord"; ADR-0007 for every-N.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Backend = Literal["docker_commit"]
# Under the trial's agent/ dir; the hook, the watcher, and the manifest builder agree on these.
CHECKPOINTS_DIRNAME = "checkpoints"
POLICY_FILENAME = "policy.json"


class CheckpointPolicy(BaseModel):
    """How a trial's checkpoints are taken; the watcher writes it to `policy.json`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Backend = "docker_commit"
    every: int = Field(ge=1, description="Checkpoint every Nth state-mutating call (ADR-0007).")


class CheckpointRecord(BaseModel):
    """The environment state captured after one state-mutating tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str = Field(min_length=1, description="Image id for docker_commit.")
    # Not trial_id: Harbor writes the trial id only when the trial ends, after every checkpoint
    # is taken (ADR-0006). TrialRecord joins trial_name to trial_id.
    trial_name: str = Field(min_length=1, description="The trial dir name, from config.json.")
    tool_call_id: str = Field(min_length=1, description="Claude Code's tool_use_id; the join key.")
    seq: int = Field(ge=1, description="Capture order within the trial, from 1.")
    covered_tool_call_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Calls whose effects first appear in this checkpoint, in request order, "
        "ending with tool_call_id (ADR-0007).",
    )
    tool_name: str = Field(min_length=1)
    # Only one backend and only physical snapshots exist (ADR-0002, ADR-0004); both stay fields
    # so each record carries its own provenance. A new value needs an ADR.
    backend: Backend = "docker_commit"
    physical: Literal[True] = True
    capture_ms: int = Field(ge=0, description="Wall time of the snapshot call.")
    bytes: int | None = Field(
        default=None, ge=0, description="Writable-layer size captured, for docker_commit."
    )
    path: str | None = Field(default=None, description="On-disk location, if any.")
    requested_at: AwareDatetime = Field(description="The .req file's mtime.")
    captured_at: AwareDatetime = Field(description="When the snapshot call returned.")

    @model_validator(mode="after")
    def _covers_itself_last(self) -> Self:
        if self.covered_tool_call_ids[-1] != self.tool_call_id:
            raise ValueError("covered_tool_call_ids must end with tool_call_id")
        if len(set(self.covered_tool_call_ids)) != len(self.covered_tool_call_ids):
            raise ValueError("covered_tool_call_ids has duplicates")
        return self
