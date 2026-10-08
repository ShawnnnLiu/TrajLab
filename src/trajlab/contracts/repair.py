"""What a repair job repairs: the failed trial, how it failed, and the arm's starting point.

Written by `trajlab repair` to `corpus/jobs/_repair-inputs/<repair job>/repair-source.json`
before the repair job starts (ADR-0012).
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

REPAIR_SOURCE_FILENAME = "repair-source.json"

# How a trial ended without passing, from its result.json alone (ADR-0012):
#   ended_turn    the agent ended its turn and the verifier scored it below 1
#   timeout       Harbor stopped the agent at its time cap (AgentTimeoutError)
#   agent_error   the agent itself failed: output or context limits, a refusal
#   infra_error   the harness failed (API, usage limit, environment, verifier); not an attempt
#   unclassified  an exception type in neither list; a person reads it before deciding
FailureKind = Literal["ended_turn", "timeout", "agent_error", "infra_error", "unclassified"]
REPAIRABLE_KINDS: frozenset[FailureKind] = frozenset({"ended_turn", "timeout", "agent_error"})

# Starting environment x loaded conversation, 2x2 (ADR-0012), plus `traj-text`: the task image,
# a new conversation, and the failed trial's history as a plain transcript in the prompt
# (ADR-0012, amendment of 2026-10-03). REPAIR_ARMS, the 2x2, is the default arm set.
RepairArm = Literal["fresh", "state", "state-traj", "traj", "traj-text"]
REPAIR_ARMS: tuple[RepairArm, ...] = ("fresh", "state", "state-traj", "traj")
CHECKPOINT_ARMS: frozenset[RepairArm] = frozenset({"state", "state-traj"})
SESSION_ARMS: frozenset[RepairArm] = frozenset({"state-traj", "traj"})
TRANSCRIPT_ARMS: frozenset[RepairArm] = frozenset({"traj-text"})
# Set for exactly the arms in TRANSCRIPT_ARMS.
TRANSCRIPT_FIELDS = (
    "transcript_file",
    "transcript_chars",
    "transcript_bytes",
    "transcript_outputs_total",
    "transcript_outputs_cut",
    "transcript_rule",
    "source_compacted",
)


class RepairSource(BaseModel):
    """One repair job: `attempts` repair trials of one failed trial under one arm."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_job: str = Field(min_length=1, description="The failed trial's job dir name.")
    source_trial: str = Field(min_length=1, description="The failed trial's dir name.")
    task_name: str = Field(min_length=1)
    failure_kind: FailureKind
    exception_type: str | None = None
    source_reward: float | None = None
    source_agent_s: float | None = Field(default=None, ge=0)
    arm: RepairArm
    attempts: int = Field(ge=1)
    checkpoint_seq: int | None = Field(default=None, ge=1)
    checkpoint_image: str | None = Field(
        default=None, description="Tag of the final checkpoint, for arms that start from it."
    )
    checkpoint_image_id: str | None = None
    session_file: str | None = Field(
        default=None, description="The copy of the native session loaded, for arms that do."
    )
    transcript_file: str | None = Field(
        default=None, description="The rendered transcript put in the prompt, for arms that do."
    )
    transcript_chars: int | None = Field(default=None, ge=0)
    transcript_bytes: int | None = Field(default=None, ge=0, description="Encoded as UTF-8.")
    transcript_outputs_total: int | None = Field(
        default=None, ge=0, description="Tool outputs in the transcript."
    )
    transcript_outputs_cut: int | None = Field(
        default=None, ge=0, description="Tool outputs shortened by `transcript_rule`."
    )
    transcript_rule: str | None = Field(
        default=None, description='e.g. "tool outputs > 4000 chars: first 2000 + last 2000".'
    )
    source_compacted: bool | None = Field(
        default=None,
        description="Whether the failed trial's native session records a compaction: a resumed "
        "conversation would hold the compacted context, the transcript holds every step.",
    )
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _arm_inputs(self) -> Self:
        if self.failure_kind not in REPAIRABLE_KINDS:
            raise ValueError(f"a {self.failure_kind} trial is not repaired")
        if (self.arm in CHECKPOINT_ARMS) != (self.checkpoint_image is not None):
            raise ValueError(f"arm {self.arm} and checkpoint_image disagree")
        if (self.arm in SESSION_ARMS) != (self.session_file is not None):
            raise ValueError(f"arm {self.arm} and session_file disagree")
        for name in TRANSCRIPT_FIELDS:
            if (self.arm in TRANSCRIPT_ARMS) != (getattr(self, name) is not None):
                raise ValueError(f"arm {self.arm} and {name} disagree")
        if (self.transcript_outputs_cut or 0) > (self.transcript_outputs_total or 0):
            raise ValueError("more tool outputs cut than the transcript has")
        return self
