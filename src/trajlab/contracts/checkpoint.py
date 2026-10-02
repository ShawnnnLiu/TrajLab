"""The record of one checkpoint, as the watcher writes it to `checkpoints.jsonl` and `.ack`,
and the policy a trial's checkpoints were taken under.

Spec: `docs/checkpoint-protocol.md`, "CheckpointRecord"; ADR-0007 for every-N; ADR-0010 for
change gating and `CallRecord`.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Backend = Literal["docker_commit"]
# Under the trial's agent/ dir; the hook, the watcher, and the manifest builder agree on these.
CHECKPOINTS_DIRNAME = "checkpoints"
POLICY_FILENAME = "policy.json"
CALLS_FILENAME = "calls.jsonl"
# How many of a call's own changed paths a CallRecord lists; the total is always recorded.
MAX_LISTED_PATHS = 100

Gate = Literal["none", "change", "audit"]


class CheckpointPolicy(BaseModel):
    """How a trial's checkpoints are taken; the watcher writes it to `policy.json`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Backend = "docker_commit"
    every: int = Field(ge=1, description="Checkpoint every Nth state-mutating call (ADR-0007).")
    gate: Gate = Field(
        default="none",
        description="none: every-N by count; change: only calls that changed the filesystem; "
        "audit: measure every call but checkpoint all of them (ADR-0010).",
    )

    @model_validator(mode="after")
    def _gate_needs_every_one(self) -> Self:
        if self.gate != "none" and self.every != 1:
            raise ValueError(f"gate {self.gate!r} requires every=1 (ADR-0010)")
        return self


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


class CallRecord(BaseModel):
    """What the watcher did for one hooked tool call (ADR-0010); in `calls.jsonl` and `.ack`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str = Field(min_length=1)
    trial_name: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    call_seq: int = Field(ge=1, description="Order among the trial's answered calls, from 1.")
    tool_failed: bool = Field(
        default=False,
        description="The call ended in error (PostToolUseFailure); it may still "
        "have changed files.",
    )
    outcome: Literal["checkpoint", "unchanged", "deferred"] = Field(
        description="checkpoint: this call triggered one; unchanged: the filesystem equals the "
        "last checkpoint's; deferred: below N under every-N, covered by a later checkpoint."
    )
    change: Literal["changed", "unchanged", "baseline", "unknown", "not_checked"] = Field(
        description="The detector's verdict against the last checkpoint. baseline: no reference "
        "yet; unknown: no detector in this image; not_checked: gate none."
    )
    changed_paths: tuple[str, ...] = Field(
        default=(),
        description="This call's own changes against the previous call, as +added, -removed, "
        f"~modified paths; at most {MAX_LISTED_PATHS}.",
    )
    changed_paths_total: int | None = Field(
        default=None, ge=0, description="How many paths this call changed; null if unmeasured."
    )
    detect_ms: int | None = Field(default=None, ge=0)
    checkpoint_seq: int | None = Field(
        default=None,
        ge=1,
        description="The checkpoint holding the state after this call; null for deferred calls "
        "(the covering checkpoint names them) and for unchanged calls before any checkpoint.",
    )
    requested_at: AwareDatetime
    answered_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome == "checkpoint" and self.checkpoint_seq is None:
            raise ValueError("a checkpoint outcome names its checkpoint_seq")
        if self.outcome == "unchanged" and self.change not in ("unchanged",):
            raise ValueError("an unchanged outcome needs the detector's unchanged verdict")
        if len(self.changed_paths) > MAX_LISTED_PATHS:
            raise ValueError(f"at most {MAX_LISTED_PATHS} changed paths are listed")
        return self
