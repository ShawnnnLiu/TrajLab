"""Ground truth from verifier replay: artifact states, timeline points, and replays.

Written by `trajlab gt` under each trial dir's `groundtruth/` (ADR-0013).
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Under the trial dir, next to Harbor's files (which are never modified).
GROUNDTRUTH_DIRNAME = "groundtruth"
STATES_DIRNAME = "states"  # one dir per artifact state, named by state_id
REPLAYS_DIRNAME = "replays"  # one Harbor regrade trial dir per replay, named by replay_id
POINTS_FILENAME = "points.jsonl"  # the trial's timeline, one TimelinePoint per line, in order
REPLAY_RECORDS_FILENAME = "replays.jsonl"  # append-only, one ReplayRecord per line
STATE_FILENAME = "state.json"  # an ArtifactState, inside its state dir
PATCHES_DIRNAME = "patches"  # every tried fix's diff, named <sha256>.diff
FIX_RECORDS_FILENAME = "fixes.jsonl"  # append-only, one FixRecord per tried fix

SHA256 = r"^[0-9a-f]{64}$"

# Harbor's ArtifactManifestEntry statuses; `failed` at a timeline point means the artifact was
# absent there, as Harbor records a collection that found nothing.
EntryStatus = Literal["ok", "failed", "empty", "skipped"]
PointKind = Literal["initial", "checkpoint", "final"]
# pytest: verifier/ctrf.json; trace: trace_results.json (vba-userform-port);
# cad: reward_details.json (freecad tasks, cad-model)
CheckKind = Literal["pytest", "trace", "cad"]
# final: the recorded final state (fidelity gate 2); repeat: a further sample of a state already
# replayed; timeline: a state first seen at an initial or checkpoint point; counterfactual: a
# state made by applying a fix to another state.
ReplayPurpose = Literal["final", "repeat", "timeline", "counterfactual"]
# verdict: the verifier reported its checks; no_verdict: the test script ran but reported no
# checks (it timed out or crashed), which is a fact about the state; infra: the environment
# or the network failed, which says nothing about the state and is retried.
ReplayOutcome = Literal["verdict", "no_verdict", "infra"]
# artifacts: the diff applies to the final state's artifacts; environment: to any file of a
# container started from the last checkpoint's image, after which a command may regenerate
# outputs and the artifacts are collected again.
FixMode = Literal["artifacts", "environment"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StateEntry(_Model):
    """One declared artifact at one point, as Harbor's `artifacts/manifest.json` records it."""

    source: str = Field(min_length=1, description="The artifact's path in the container.")
    type: Literal["file", "directory"]
    status: EntryStatus


class StateFile(_Model):
    """One path under a state's `artifacts/` dir."""

    path: str = Field(min_length=1, description="Posix path relative to the state's artifacts/.")
    kind: Literal["file", "symlink", "directory"]
    size: int | None = Field(default=None, ge=0, description="Bytes, for files.")
    sha256: str | None = Field(default=None, pattern=SHA256, description="For files.")
    executable: bool = Field(default=False, description="Any execute bit set, for files.")
    target: str | None = Field(default=None, description="Link target, for symlinks.")

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        if (self.kind == "file") != (self.sha256 is not None and self.size is not None):
            raise ValueError("files, and only files, carry sha256 and size")
        if (self.kind == "symlink") != (self.target is not None):
            raise ValueError("symlinks, and only symlinks, carry a target")
        return self


class ArtifactState(_Model):
    """What the verifier would receive at a timeline point: the declared artifacts' bytes."""

    state_id: str = Field(
        pattern=SHA256, description="sha256 of the canonical JSON of `entries` and `files`."
    )
    task_name: str = Field(min_length=1)
    entries: tuple[StateEntry, ...] = Field(min_length=1)
    files: tuple[StateFile, ...]


class TimelinePoint(_Model):
    """One point of a trial's timeline and the artifact state at it (ADR-0013, decision 2)."""

    trial_name: str = Field(min_length=1)
    index: int = Field(ge=0, description="Position in the timeline: initial is 0, final last.")
    kind: PointKind
    seq: int | None = Field(default=None, ge=1, description="The checkpoint's seq.")
    tool_call_id: str | None = Field(
        default=None, description="The checkpoint's tool_call_id (a stop id for a stop one)."
    )
    covered_tool_call_ids: tuple[str, ...] = Field(
        default=(), description="The checkpoint's covered calls: the calls this point blames."
    )
    trigger: Literal["tool_call", "stop"] | None = None
    image: str | None = Field(
        default=None, description="Image id the state was extracted from; null for final."
    )
    state_id: str = Field(pattern=SHA256)
    captured_at: AwareDatetime | None = Field(
        default=None, description="The checkpoint's captured_at."
    )

    @model_validator(mode="after")
    def _checkpoint_fields(self) -> Self:
        is_checkpoint = self.kind == "checkpoint"
        if is_checkpoint != (self.seq is not None and self.tool_call_id is not None):
            raise ValueError("checkpoint points, and only they, carry seq and tool_call_id")
        if is_checkpoint and not self.covered_tool_call_ids:
            raise ValueError("a checkpoint point names its covered calls")
        if (self.kind == "final") != (self.image is None):
            raise ValueError("every point but final names the image it was extracted from")
        if (self.kind == "initial") != (self.index == 0):
            raise ValueError("the initial point, and only it, has index 0")
        return self


class CheckResult(_Model):
    """One verifier check's outcome in one replay or recorded verifier run."""

    kind: CheckKind
    check: str = Field(min_length=1)
    status: str | None = Field(
        description="ctrf status (passed, failed, skipped, ...); null for a CAD metric row."
    )
    value: float | None = None
    message: str | None = Field(default=None, description="First characters of the failure.")


class ReplayRecord(_Model):
    """One run of a task's verifier on one artifact state (ADR-0013, decision 3)."""

    replay_id: str = Field(min_length=1, description="Also the regrade trial dir's name.")
    trial_name: str = Field(min_length=1)
    state_id: str = Field(pattern=SHA256)
    purpose: ReplayPurpose
    base_state_id: str | None = Field(
        default=None, pattern=SHA256, description="Counterfactual: the state the fix applies to."
    )
    patch_sha256: str | None = Field(
        default=None, pattern=SHA256, description="Counterfactual: sha256 of the applied diff."
    )
    reward: float | None = Field(description="Null when the verifier produced no reward.")
    checks: tuple[CheckResult, ...]
    exception_type: str | None = None
    exception_message: str | None = None
    harbor_version: str = Field(min_length=1)
    task_ref: str = Field(min_length=1, description="The task package digest the verifier ran.")
    started_at: AwareDatetime
    finished_at: AwareDatetime
    outcome: ReplayOutcome = "verdict"
    load_1m: float | None = Field(default=None, description="Host load average at the start.")
    beside: tuple[str, ...] = Field(
        default=(), description="Claims running on the host when this replay was admitted."
    )

    @model_validator(mode="after")
    def _counterfactual_fields(self) -> Self:
        is_counterfactual = self.purpose == "counterfactual"
        if is_counterfactual != (self.base_state_id is not None and self.patch_sha256 is not None):
            raise ValueError("counterfactual replays, and only they, name a base state and patch")
        return self


class FixFile(_Model):
    """What a fix does to one file, against the file's version in the fix's base."""

    path: str = Field(min_length=1, description="Container path, e.g. /app/src/db.cc.")
    existed: bool = Field(description="The file existed in the base.")
    removed: tuple[int, ...] = Field(
        default=(), description="Base lines (1-based) the fix removes or replaces."
    )
    inserted_before: tuple[int, ...] = Field(
        default=(), description="Base lines before which the fix only inserts."
    )
    added: int = Field(ge=0, description="Lines the fix adds.")


class FixRecord(_Model):
    """One counterfactual fix tried on a trial's final state (ADR-0013, decision 5)."""

    fix_id: str = Field(min_length=1)
    trial_name: str = Field(min_length=1)
    mode: FixMode
    base_state_id: str = Field(pattern=SHA256, description="The final state the fix starts from.")
    base_image: str | None = Field(
        default=None, description="Environment mode: the image the container started from."
    )
    patch_sha256: str = Field(pattern=SHA256, description="Its diff is patches/<sha256>.diff.")
    command: str | None = Field(default=None, description="Environment mode: run after patching.")
    command_user: str | None = None
    command_workdir: str | None = None
    command_timeout_s: float | None = None
    command_exit_code: int | None = None
    command_output: str | None = Field(default=None, description="The command's output, tail.")
    files: tuple[FixFile, ...] = ()
    state_id: str | None = Field(
        default=None, pattern=SHA256, description="The state the fix produced, if it applied."
    )
    replay_id: str | None = Field(default=None, description="The counterfactual replay.")
    error: str | None = Field(default=None, description="Why the fix produced no replay.")
    fixed: tuple[str, ...] = Field(default=(), description="Checks failing at base, now passing.")
    broken: tuple[str, ...] = Field(default=(), description="Checks passing at base, now not.")
    still_failing: tuple[str, ...] = ()
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _outcome(self) -> Self:
        if (self.replay_id is None) == (self.error is None):
            raise ValueError("a fix has either a replay or an error")
        if (self.mode == "environment") != (self.base_image is not None):
            raise ValueError("environment fixes, and only they, name a base image")
        return self


ITEMS_FILENAME = "items.jsonl"  # the trial's ground-truth items, rewritten by `trajlab gt items`
LABELS_FILENAME = "labels.json"  # the labeler's claims, input to `trajlab gt items`
MINIMIZED_FILENAME = "minimized.jsonl"  # one MinimizationRecord per minimized fix
REVERTS_FILENAME = "reverts.jsonl"  # one RevertRecord per tested regression

# What one hunk of a confirmed fix says about the agent (ADR-0013, decision 5):
# wrong_edit: it replaces lines a checkpoint wrote
# incomplete_edit: it only inserts, next to lines a checkpoint wrote
# missed_fix: it replaces lines present since the initial state
# omission: it only inserts, next to lines present since the initial state, or creates a file
#   the verifier does not grade
# missing_artifact: it creates a graded artifact absent at the end
HunkKind = Literal["wrong_edit", "incomplete_edit", "missed_fix", "omission", "missing_artifact"]
# An item's kind: its strongest hunk kind, `regression` for a timeline item, or `unconfirmed`
ItemKind = Literal[
    "regression",
    "wrong_edit",
    "incomplete_edit",
    "missed_fix",
    "omission",
    "missing_artifact",
    "unconfirmed",
]
# timeline: decided by replays alone; counterfactual: a labeler's fix the verifier confirmed;
# labeler: the labeler's claim, unconfirmed
ItemMethod = Literal["timeline", "counterfactual", "labeler"]
ReviewState = Literal["unreviewed", "accepted", "rejected"]
View = Literal["lines", "json"]


class BlamedHunk(_Model):
    """One hunk of a confirmed fix, blamed against the file's history."""

    path: str = Field(min_length=1, description="Container path.")
    view: View = Field(
        default="lines", description="json: line numbers count in the pretty-printed view."
    )
    kind: HunkKind
    raw_hunk: int = Field(ge=0, description="Index of the hunk in the fix's raw diff.")
    removed: tuple[int, ...] = Field(
        default=(), description="Final-version lines (1-based) the fix removes or replaces."
    )
    origins: tuple[int, ...] = Field(
        default=(), description="Per removed line, the point that last wrote it."
    )
    earliest: tuple[int, ...] = Field(
        default=(), description="Per removed line, the earliest point the file held its text."
    )
    inserted_before: int | None = Field(
        default=None, description="A pure insertion: the final-version line it goes before."
    )
    anchors: tuple[int, ...] = Field(default=(), description="Insertion: the lines beside it.")
    anchor_origins: tuple[int, ...] = ()
    added: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _shapes(self) -> Self:
        if not len(self.removed) == len(self.origins) == len(self.earliest):
            raise ValueError("one origin and one earliest point per removed line")
        if len(self.anchors) != len(self.anchor_origins):
            raise ValueError("one origin per anchor")
        return self


class AlternativeFix(_Model):
    """Another verifier-confirmed fix for the same checks, with its own blame."""

    fix_id: str = Field(min_length=1)
    kind: ItemKind
    points: tuple[int, ...] = ()
    tool_call_ids: tuple[str, ...] = ()
    hunks: tuple[BlamedHunk, ...] = ()
    fix_size: int = Field(ge=0)


class GroundTruthItem(_Model):
    """Where and when a failed trial broke the checks it names (ADR-0013)."""

    item_id: str = Field(min_length=1)
    trial_name: str = Field(min_length=1)
    task_name: str = Field(min_length=1)
    artifact_class: str = Field(description="source, output, or generated (traits).")
    checks: tuple[str, ...] = Field(min_length=1, description="`<kind>:<check>` keys.")
    kind: ItemKind
    method: ItemMethod
    points: tuple[int, ...] = Field(
        default=(), description="Blamed timeline points (checkpoints); empty if none is blamed."
    )
    tool_call_ids: tuple[str, ...] = Field(
        default=(), description="The blamed points' covered calls."
    )
    step_ids: tuple[int, ...] = Field(
        default=(), description="ATIF step ids in agent/trajectory.json of those calls."
    )
    earliest_points: tuple[int, ...] = Field(
        default=(), description="Earliest points that introduced the blamed text (lineage)."
    )
    related_tool_call_ids: tuple[str, ...] = Field(
        default=(),
        description="Calls that edited ungraded files near the fixed ones; citing them is not "
        "a false accusation.",
    )
    hunks: tuple[BlamedHunk, ...] = ()
    fix_id: str | None = None
    fix_size: int | None = Field(default=None, ge=0, description="Lines removed plus added.")
    alternatives: tuple[AlternativeFix, ...] = Field(
        default=(), description="Other confirmed fixes for these checks: acceptable answers too."
    )
    revert_fix_id: str | None = Field(
        default=None, description="Regression: the try that undid the point's change."
    )
    replays: tuple[str, ...] = Field(default=(), description="Replay ids the item rests on.")
    flags: tuple[str, ...] = ()
    explanation: str | None = None
    category: str | None = None
    review: ReviewState = "unreviewed"

    @model_validator(mode="after")
    def _method_fits_kind(self) -> Self:
        if (self.kind == "regression") != (self.method == "timeline"):
            raise ValueError("regressions, and only they, come from the timeline")
        if (self.kind == "unconfirmed") != (self.method == "labeler"):
            raise ValueError("unconfirmed items, and only they, are the labeler's alone")
        if self.method == "counterfactual" and self.fix_id is None:
            raise ValueError("a counterfactual item names its fix")
        if self.kind in ("wrong_edit", "incomplete_edit") and not self.points:
            raise ValueError("a wrong or incomplete edit blames the points that wrote the lines")
        return self


class LabelCause(_Model):
    """One cause the labeler claims for some of a trial's failing checks."""

    checks: tuple[str, ...] = Field(min_length=1, description="`<kind>:<check>` keys.")
    fix_id: str | None = Field(
        default=None, description="The fix that removes this cause; null if none was found."
    )
    explanation: str = Field(min_length=1)
    category: str | None = Field(default=None, description="A codebook category, if any.")
    suspected_tool_call_ids: tuple[str, ...] = Field(
        default=(), description="For a cause without a fix: the calls the labeler suspects."
    )


class TrialLabels(_Model):
    """The labeler's output for one trial, `groundtruth/labels.json`."""

    trial_name: str = Field(min_length=1)
    labeler: str = Field(min_length=1, description="Who proposed the causes, e.g. a model id.")
    causes: tuple[LabelCause, ...]
    notes: str | None = None


class LeaveOut(_Model):
    """A confirmed fix replayed without one of its raw hunks."""

    raw_hunk: int = Field(ge=0)
    fix_id: str | None = Field(default=None, description="The try without this hunk.")
    needed_for: tuple[str, ...] = Field(
        default=(), description="Checks the fix turned passing that fail without this hunk."
    )
    broken: tuple[str, ...] = Field(default=(), description="Checks the reduced fix breaks.")
    error: str | None = None


class MinimizationRecord(_Model):
    """Which hunks of a confirmed fix each of its checks needs (ADR-0013, decision 5)."""

    trial_name: str = Field(min_length=1)
    fix_id: str = Field(min_length=1)
    raw_hunks: int = Field(ge=1)
    leave_outs: tuple[LeaveOut, ...]
    created_at: AwareDatetime


class RevertRecord(_Model):
    """A regression tested by undoing its point's change on the final state."""

    trial_name: str = Field(min_length=1)
    point: int = Field(ge=1, description="The regression's first failing point.")
    checks: tuple[str, ...] = Field(min_length=1)
    fix_id: str | None = Field(default=None, description="The try that undid the change.")
    verdict: Literal["confirmed", "cause_moved", "cannot_revert"]
    restored: tuple[str, ...] = Field(default=(), description="Checks passing again.")
    broken: tuple[str, ...] = ()
    reason: str | None = None
    created_at: AwareDatetime


class GroundTruthManifest(_Model):
    """How a ground-truth dataset was produced and what it holds (ADR-0013).

    Written by `trajlab gt manifest` to `corpus/manifests/<dataset_id>.json`.
    """

    dataset_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    created_at: AwareDatetime
    source_corpus_id: str = Field(min_length=1)
    source_job: str = Field(min_length=1, description="The job dir name under corpus/jobs/.")
    storage: str | None = None
    harbor_version: str = Field(min_length=1)
    repo_sha: str = Field(min_length=40, max_length=40)
    repo_dirty: bool
    labelers: tuple[str, ...] = Field(description="Who proposed causes, e.g. model and run id.")
    verifier_images: dict[str, str] = Field(description="Task name -> verifier image@digest.")
    admission: dict[str, float] = Field(description="CPU and memory caps replays ran under.")
    trials: int = Field(ge=0)
    failed_trials: int = Field(ge=0)
    points: int = Field(ge=0)
    states: int = Field(ge=0)
    replays_by_outcome: dict[str, int]
    replays_by_purpose: dict[str, int]
    gate1: dict[str, int]
    gate2: dict[str, int]
    excluded_checks: int = Field(ge=0)
    fixes: int = Field(ge=0)
    items_by_kind: dict[str, int]
    items_by_method: dict[str, int]


REFUTATIONS_FILENAME = "refutations.jsonl"  # one RefutationRecord per refutation attempt


class RefutationRecord(_Model):
    """An adversarial reviewer's verdict on one item (ADR-0013, decision 5)."""

    trial_name: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    verdict: Literal["upheld", "refuted", "uncertain"]
    reasons: str = Field(min_length=1)
    alternative_fix_ids: tuple[str, ...] = Field(
        default=(), description="Fixes the reviewer tried at another location for these checks."
    )
    reviewer: str = Field(min_length=1, description="e.g. a model id.")
    created_at: AwareDatetime
