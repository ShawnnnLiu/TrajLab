"""Our entries in ATIF `extra` dicts, written by postprocess into the enriched trajectory.

Shape: ADR-0003. Every step of the enriched trajectory carries `extra["trajlab"]`; a compaction
step also carries `extra["context_management"]` (ATIF RFC 0001, section VII); the trajectory root
carries `extra["trajlab"]` as `EnrichedTrajectoryExtra`. Harbor's models forbid unknown keys, so
everything we add lives in these dicts.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from trajlab.contracts.checkpoint import CallRecord, CheckpointPolicy

TRAJLAB_EXTRA_KEY = "trajlab"
CONTEXT_MANAGEMENT_EXTRA_KEY = "context_management"
CHECKPOINT_STEP_MESSAGE = "checkpoint"
COMPACTION_STEP_MESSAGE = "context compaction"
ENRICHED_TRAJECTORY_FILENAME = "trajectory.enriched.json"


class _StepExtra(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OriginalStepExtra(_StepExtra):
    """On a step copied from trajectory.json: its step_id before renumbering, and what capture
    did for each of its tool calls that the hook saw."""

    kind: Literal["original"] = "original"
    original_step_id: int = Field(ge=1)
    calls: tuple[CallRecord, ...] = Field(
        default=(),
        description="The watcher's CallRecord for each of this step's answered hooked calls, in "
        "tool_calls order; it names the checkpoint holding the state after the call.",
    )
    timed_out_tool_call_ids: tuple[str, ...] = Field(
        default=(),
        description="This step's hooked calls the hook gave up on (`.timeout`): no record, "
        "no checkpoint.",
    )


class CheckpointStepExtra(_StepExtra):
    """On an inserted checkpoint system step.

    `tool_call_id` links back to the agent step's tool call, or is the stop request id for a
    stop checkpoint (ADR-0011). It cannot be an observation `source_call_id`, which ATIF
    requires to name a tool call in the same step. The `CheckpointRecord` itself goes in
    `observation.results[0].extra`.
    """

    kind: Literal["checkpoint"] = "checkpoint"
    tool_call_id: str = Field(min_length=1)
    original_step_id: None = None


class CompactionStepExtra(_StepExtra):
    """On an inserted system step marking a compaction boundary recovered from native JSONL.

    The `CompactionRecord` goes in `observation.results[0].extra`.
    """

    kind: Literal["compaction"] = "compaction"
    original_step_id: None = None


TrajlabStepExtra = Annotated[
    OriginalStepExtra | CheckpointStepExtra | CompactionStepExtra, Field(discriminator="kind")
]


class ContextManagementExtra(_StepExtra):
    """ATIF RFC 0001 section VII `context_management`. Harbor has no model for it."""

    type: Literal["compaction", "pruning", "injection"]
    boundary: Literal["replace", "append", "truncate"]


class CompactionRecord(_StepExtra):
    """One `compact_boundary` event of Claude Code's native session JSONL.

    Claude Code writes the boundary, then a user message flagged `isCompactSummary` whose text
    is the summary the model continues from; Harbor's converter keeps that message as a user
    step and drops the boundary.
    """

    uuid: str = Field(min_length=1, description="The boundary event's native uuid.")
    timestamp: AwareDatetime
    native_file: str = Field(
        min_length=1, description="The session JSONL it came from, relative to the agent/ dir."
    )
    trigger: str | None = Field(description="Claude Code's compactMetadata.trigger: auto, manual.")
    pre_tokens: int | None = Field(ge=0, description="Context tokens before compaction.")
    post_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    logical_parent_uuid: str | None = Field(
        default=None, description="The last native event before the boundary."
    )
    summary_uuid: str | None = Field(
        default=None, description="The `isCompactSummary` user event that follows, if logged."
    )
    is_sidechain: bool = Field(default=False, description="Compaction inside a subagent.")
    agent_id: str | None = None


class EnrichedTrajectoryExtra(BaseModel):
    """`extra["trajlab"]` on the enriched trajectory's root: the trial-level capture record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="sha256 of the trajectory.json it was built from."
    )
    policy: CheckpointPolicy | None = Field(
        description="The trial's policy.json; null for a trial captured without checkpoints."
    )
    stop_calls: tuple[CallRecord, ...] = Field(
        default=(),
        description="CallRecords of the stop requests (ADR-0011): each names the checkpoint "
        "holding the state the agent left behind.",
    )
    timed_out_stop_ids: tuple[str, ...] = Field(
        default=(), description="Stop requests the hook gave up on."
    )
