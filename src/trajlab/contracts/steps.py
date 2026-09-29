"""Our entries in ATIF step `extra`, written by postprocess into the enriched trajectory.

Shape: ADR-0003. Every step of the enriched trajectory carries `extra["trajlab"]`; a compaction
step also carries `extra["context_management"]` (ATIF RFC 0001, section VII). Harbor's Step
forbids unknown keys, so everything we add lives in these dicts.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

TRAJLAB_EXTRA_KEY = "trajlab"
CONTEXT_MANAGEMENT_EXTRA_KEY = "context_management"
CHECKPOINT_STEP_MESSAGE = "checkpoint"


class _StepExtra(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OriginalStepExtra(_StepExtra):
    """On a step copied from trajectory.json: its step_id before renumbering."""

    kind: Literal["original"] = "original"
    original_step_id: int = Field(ge=1)


class CheckpointStepExtra(_StepExtra):
    """On an inserted checkpoint system step.

    `tool_call_id` links back to the agent step's tool call. It cannot be an observation
    `source_call_id`, which ATIF requires to name a tool call in the same step. The
    `CheckpointRecord` itself goes in `observation.results[0].extra`.
    """

    kind: Literal["checkpoint"] = "checkpoint"
    tool_call_id: str = Field(min_length=1)
    original_step_id: None = None


class CompactionStepExtra(_StepExtra):
    """On an inserted system step marking a compaction boundary recovered from native JSONL."""

    kind: Literal["compaction"] = "compaction"
    original_step_id: None = None


TrajlabStepExtra = Annotated[
    OriginalStepExtra | CheckpointStepExtra | CompactionStepExtra, Field(discriminator="kind")
]


class ContextManagementExtra(_StepExtra):
    """ATIF RFC 0001 section VII `context_management`. Harbor has no model for it."""

    type: Literal["compaction", "pruning", "injection"]
    boundary: Literal["replace", "append", "truncate"]
