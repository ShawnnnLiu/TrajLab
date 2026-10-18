"""Rows of the Exp 2 store (ADR-0014): one model per table, one instance per row.

The store has two DuckDB files (`trajlab.store.schema` holds their DDL):

- `record.duckdb`, the record an agent may read: `trials`, `steps`, `deltas`, and `store_meta`.
- `heldout.duckdb`, never opened by an agent connection or exported to an agent: `checks`,
  `faults`, and `blame` (ground truth, for scoring), and `chains` (capture completeness).

Field order here is column order there; a test keeps the two in step. Models only: no I/O, no
imports from other trajlab modules but `contracts`.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Bump with an ADR whenever any table below or in trajlab.store.schema changes shape.
SCHEMA_VERSION = 1
RECORD_DB_FILENAME = "record.duckdb"
HELDOUT_DB_FILENAME = "heldout.duckdb"
# Content-addressed blobs of binary file versions: <store>/blobs/<sha256>.
BLOBS_DIRNAME = "blobs"
SHA256 = r"^[0-9a-f]{64}$"

StepSource = Literal["user", "agent", "system"]
EntryKind = Literal["file", "dir", "symlink", "other"]
DeltaOp = Literal["add", "delete", "modify"]


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrialRow(_Row):
    """One trial: an attempt at a task, in a retry chain of a failure (attempt 0 is the corpus
    first attempt). Values come from Harbor's `result.json` and the trajectory's first step."""

    trial_id: UUID = Field(description="Harbor's trial id, `id` in result.json.")
    trial_name: str = Field(min_length=1, description="The trial dir name, `<task>__<id>`.")
    task: str = Field(min_length=1, description="Harbor's task name, e.g. `terminal-bench/x`.")
    attempt_idx: int = Field(ge=0, description="Position in its retry chain; 0 = first attempt.")
    parent_trial_id: UUID | None = Field(
        default=None, description="The attempt this one followed; null for attempt 0."
    )
    prompt: str = Field(description="The instruction the agent received: the first user step.")
    model: str = Field(min_length=1, description="e.g. `anthropic/claude-sonnet-5-5`.")
    harness: str = Field(min_length=1, description="The agent, e.g. `claude-code`.")
    harness_version: str | None = Field(default=None, description="e.g. `2.1.278`.")
    reward: float | None = Field(default=None, description="The verifier's reward; null if none.")
    tokens_in: int | None = Field(default=None, ge=0, description="Input tokens, cache included.")
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    wall_ms: int | None = Field(default=None, ge=0, description="Agent execution wall time.")

    @model_validator(mode="after")
    def _parent_iff_retry(self) -> Self:
        if (self.attempt_idx == 0) != (self.parent_trial_id is None):
            raise ValueError("attempt 0 has no parent_trial_id and every retry has one")
        return self


class StepRow(_Row):
    """One tool call, or one ATIF step without tool calls, or one end-of-turn (stop) record.

    An ATIF step with k tool calls gives k rows, in call order; its message and reasoning sit on
    the first of them only. The first user step is not a row: it is `trials.prompt`. Checkpoint
    system steps are not rows either: their state is `checkpoint_seq` and their changes are the
    `deltas` of the step that owns them.
    """

    trial_id: UUID
    step_idx: int = Field(ge=1, description="Order within the trial, from 1.")
    atif_step_id: int | None = Field(
        default=None, ge=1, description="step_id in Harbor's trajectory.json; null for a stop row."
    )
    source: StepSource
    tool_call_id: str | None = Field(
        default=None,
        description="Claude Code's tool_use_id; the stop request id for a stop row (ADR-0011).",
    )
    tool: str | None = Field(
        default=None, description="Tool name; `Stop`/`StopFailure` for a stop."
    )
    args_json: str | None = Field(
        default=None, description="The call's arguments, JSON with sorted keys."
    )
    output: str | None = Field(default=None, description="The tool result as the agent saw it.")
    is_error: bool | None = Field(default=None, description="Claude Code flagged the result.")
    message: str | None = Field(default=None, description="The step's text, on its first row.")
    reasoning: str | None = Field(default=None, description="ATIF reasoning_content, first row.")
    t_start: AwareDatetime | None = Field(default=None, description="The ATIF step's timestamp.")
    t_end: AwareDatetime | None = Field(
        default=None, description="When the hook saw the call finish; null for unhooked calls."
    )
    checkpoint_seq: int | None = Field(
        default=None,
        ge=1,
        description="The checkpoint holding the environment state right after this row; null "
        "before the first checkpoint (the task image's state) or when unknown (a timed-out call, "
        "or a call deferred under every-N whose changes are in a later checkpoint).",
    )
    checkpoint_id: str | None = Field(default=None, description="That checkpoint's image id.")
    # Derived columns (ADR-0014; trajlab.store.derive, build-order step 3). Null until derived.
    is_loop: bool | None = None
    is_revert: bool | None = None
    files_touched: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.checkpoint_seq is None) != (self.checkpoint_id is None):
            raise ValueError("checkpoint_seq and checkpoint_id are set together")
        if self.tool is None and (self.args_json is not None or self.output is not None):
            raise ValueError("a row without a tool has no args_json or output")
        return self


class DeltaRow(_Row):
    """One path whose state differs between a checkpoint and the one before it (ADR-0014).

    Keyed to the step that owns the later checkpoint. A path changes if it appears, disappears,
    or its kind, mode, owner, or content changes; times are never compared. Directories have no
    content. Content is a file's bytes or a symlink's target.
    """

    trial_id: UUID
    step_idx: int = Field(ge=1)
    path: str = Field(pattern=r"^/", description="Absolute path in the container.")
    op: DeltaOp
    kind_before: EntryKind | None = None
    kind_after: EntryKind | None = None
    is_text: bool | None = Field(
        default=None,
        description="Both versions are text under the store's text rule; null for directories "
        "and other kinds.",
    )
    hunk: str | None = Field(
        default=None,
        description="Unified diff from the earlier to the later version, for text whose content "
        "changed; may be empty when neither version has a line.",
    )
    blob_ref: str | None = Field(
        default=None,
        description="`blobs/<sha256>` of the later version, when it is a non-text file whose "
        "content changed.",
    )
    sha256_before: str | None = Field(default=None, pattern=SHA256)
    sha256_after: str | None = Field(default=None, pattern=SHA256)
    bytes_before: int | None = Field(default=None, ge=0)
    bytes_after: int | None = Field(default=None, ge=0)
    mode_before: str | None = Field(default=None, pattern=r"^[0-7]{4}$", description="e.g. 0644.")
    mode_after: str | None = Field(default=None, pattern=r"^[0-7]{4}$")
    owner_before: str | None = Field(default=None, pattern=r"^\d+:\d+$", description="uid:gid.")
    owner_after: str | None = Field(default=None, pattern=r"^\d+:\d+$")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        before, after = self.kind_before is not None, self.kind_after is not None
        expected = {"add": (False, True), "delete": (True, False), "modify": (True, True)}
        if (before, after) != expected[self.op]:
            raise ValueError(
                f"op {self.op} with kind_before={self.kind_before!r}, "
                f"kind_after={self.kind_after!r}"
            )
        if self.hunk is not None and self.is_text is not True:
            raise ValueError("only text has a hunk")
        if self.blob_ref is not None and (self.is_text is not False or self.kind_after is None):
            raise ValueError("a blob_ref is the later version of a non-text file")
        return self


class StoreMeta(_Row):
    """What a store was built under; one `store_meta` row per field, value as JSON."""

    schema_version: int = Field(ge=1)
    exclusions: tuple[str, ...] = Field(
        description="Regexes of harness paths that never give a delta row "
        "(trajlab.store.exclusions)."
    )
    text_rule: str = Field(min_length=1, description="When a file version counts as text.")
    reconcile_method: str = Field(min_length=1, description="How deltas were computed.")


# -- held out: heldout.duckdb ------------------------------------------------------------------

# How a call relates to a fault in its ground truth (ADR-0013 §5).
BlameRole = Literal["blamed", "related", "alternative"]


class CheckRow(_Row):
    """One verifier check of a trial and its outcome. Held out from every agent (ADR-0014)."""

    trial_id: UUID
    check_id: str = Field(min_length=1, description="`<kind>:<check>`, as ADR-0013 keys checks.")
    passed: bool | None = Field(description="Null for a check with a value and no pass/fail.")
    fault_id: str | None = Field(
        default=None,
        description="The fault this failing check belongs to, if ground truth has one.",
    )


class FaultRow(_Row):
    """One fault: a set of failing checks with a confirmed cause (an ADR-0013 item). Held out."""

    trial_id: UUID
    fault_id: str = Field(min_length=1, description="The ground-truth item id.")
    kind: str = Field(min_length=1, description="ADR-0013 item kind, e.g. wrong_edit, omission.")
    method: str = Field(min_length=1, description="ADR-0013 item method, e.g. counterfactual.")
    flags: tuple[str, ...] = ()


class BlameRow(_Row):
    """One call a fault's ground truth names. Held out.

    `blamed` calls are the primary fix's; `alternative` calls are another confirmed fix's
    (`fix_id` groups them); `related` calls are neutral to cite (ADR-0013 §5, §7).
    """

    trial_id: UUID
    fault_id: str = Field(min_length=1)
    role: BlameRole
    fix_id: str | None = None
    tool_call_id: str = Field(min_length=1)
    step_idx: int | None = Field(default=None, ge=1, description="The call's row in `steps`.")


class ChainReport(_Row):
    """Whether a trial's checkpoint chain is complete: every hooked call answered and every
    checkpoint image readable (ADR-0014). Held out; `exp2 run` refuses incomplete chains."""

    trial_id: UUID
    complete: bool
    hooked_calls: int = Field(ge=0, description="Tool calls of hooked tools in the trajectory.")
    answered_calls: int = Field(ge=0, description="Of those, calls with a CallRecord.")
    timed_out_calls: tuple[str, ...] = Field(default=(), description="Calls with only a .timeout.")
    unanswered_calls: tuple[str, ...] = Field(
        default=(), description="Hooked calls with neither a record nor a .timeout."
    )
    checkpoints: int = Field(ge=0)
    images_checked: bool = Field(description="False when deltas were not computed at all.")
    missing_images: tuple[int, ...] = Field(default=(), description="Seqs whose image is gone.")
    final_state_recorded: bool = Field(description="A stop record names the final state.")
    reasons: tuple[str, ...] = Field(default=(), description="Why the chain is incomplete.")

    @model_validator(mode="after")
    def _complete_iff_no_reason(self) -> Self:
        if self.complete == bool(self.reasons):
            raise ValueError("a chain is complete exactly when it has no reasons")
        return self


class DerivedSpec(_Row):
    """A derived column: its interface is the column, its implementation a registered,
    versioned function (ADR-0014; trajlab.store.derive). A second implementation of the same
    name, e.g. an LLM one, changes `implementation`, never the schema."""

    name: str = Field(pattern=r"^[a-z_]+$")
    table: Literal["steps"]
    sql_type: Literal["BOOLEAN", "INTEGER"]
    implementation: str = Field(min_length=1, description="e.g. `deterministic-v1`.")
    description: str = Field(min_length=1)
