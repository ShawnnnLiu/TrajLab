"""Hand labels of the failure patterns, against which the detector is validated (ADR-0015 §8).

A labeler judges, for each trial, which patterns occurred; in the sampled trials they also judge
each step, meaning each hooked call and each stop, keyed by its `tool_call_id`, the key the
detector's flags carry. `trajlab trace` writes one file per labeler per trial:
`<labels dir>/<job name>/<labeler>/<trial name>.json`.
"""

from typing import Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

# ADR-0015 §1's seven patterns, plus `stall`, the loop subtype that §1 reports separately.
LabelPattern = Literal[
    "loop",
    "stall",
    "no_effect_write",
    "false_edit_claim",
    "false_test_claim",
    "unverified_completion",
    "server_state",
    "forbidden_action",
]
LABEL_PATTERNS: tuple[LabelPattern, ...] = get_args(LabelPattern)
LABELER_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
MAX_NOTE_CHARS = 20_000


class Label(BaseModel):
    """One labeler's judgment of one step, or of a whole trial.

    `patterns` lists the patterns judged present; every other pattern is judged absent. None
    means not judged yet, since a note alone is not a judgment.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    patterns: tuple[LabelPattern, ...] | None = None
    unsure: bool = False
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)
    updated_at: AwareDatetime

    @field_validator("patterns")
    @classmethod
    def _canonical_order(
        cls, value: tuple[LabelPattern, ...] | None
    ) -> tuple[LabelPattern, ...] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("a pattern is listed twice")
        return tuple(p for p in LABEL_PATTERNS if p in value)


class TrialLabels(BaseModel):
    """One labeler's labels for one trial."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_name: str = Field(min_length=1)
    trial_name: str = Field(min_length=1)
    labeler: str = Field(pattern=LABELER_NAME_PATTERN)
    trial: Label | None = Field(default=None, description="The trial-level judgment.")
    steps: dict[str, Label] = Field(
        default_factory=dict,
        description="By tool_call_id (a stop's request id for a stop); a step without an "
        "entry was not reviewed.",
    )
    done: bool = Field(default=False, description="The labeler finished this trial.")
    updated_at: AwareDatetime
