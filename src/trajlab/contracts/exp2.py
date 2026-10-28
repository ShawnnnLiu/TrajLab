"""Exp 2's retry loop (ADR-0016, ADR-0022): the arms, one attempt's result, one ingest's result.

`trajlab exp2 run` (`trajlab.exp2`) keeps one directory per retry chain, a failure's attempts
under one arm, with two append-only logs: `attempts.jsonl`, one `AttemptResult` per attempt it
ran, and `ingests.jsonl`, one `IngestResult` per failed attempt it built into the chain's store.
The loop resumes from them. `QueryLogEntry` (ADR-0019) is defined with the store's rows in
`contracts.store` and named here too, since the technical spec lists it with these.

Models only: no I/O, no imports from other trajlab modules but `contracts`.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trajlab.contracts.repair import FailureKind
from trajlab.contracts.store import SHA256, QueryLogEntry

ATTEMPTS_FILENAME = "attempts.jsonl"
INGESTS_FILENAME = "ingests.jsonl"
# Where an attempt's agent finds the record of prior attempts in the container: the
# record-files bundle, mounted read-only (ADR-0022). The record-engine arm has its query
# command at /logs/agent/bin/query instead (ADR-0019) and nothing here.
RECORD_MOUNT = "/logs/agent/record"

# control       no record: an independent resample under the same prompt
# record-files  the record of prior attempts as files, the bundle at RECORD_MOUNT
# record-engine the same record through the query tool
Exp2Arm = Literal["control", "record-files", "record-engine"]
EXP2_ARMS: tuple[Exp2Arm, ...] = ("control", "record-files", "record-engine")
RECORD_ARMS: frozenset[Exp2Arm] = frozenset({"record-files", "record-engine"})

# passed  the verifier's reward is 1
# failed  the attempt ran and did not pass: reward below 1, or the agent's own failure
#         (`ended_turn`, `timeout`, `agent_error`, ADR-0012 item 4)
# infra   the harness failed (`infra_error`, `unclassified`): not an attempt; run again
# lost    no trial result: `trajlab run` refused, or Harbor died; not an attempt; run again
AttemptOutcome = Literal["passed", "failed", "infra", "lost"]
COUNTED_OUTCOMES: frozenset[AttemptOutcome] = frozenset({"passed", "failed"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArmConfig(_Model):
    """What one arm gives an attempt beyond the task. The arms differ only here: in the evidence
    sentence of the prompt, which this names, and in what the agent can reach."""

    arm: Exp2Arm
    ingests: bool = Field(
        description="Failed attempts are built into the chain's store, so the next attempt's "
        "record holds them; false for `control`, which has no store."
    )
    mounts_bundle: bool = Field(
        description="The chain's record-files bundle is mounted read-only at RECORD_MOUNT."
    )
    serves_queries: bool = Field(
        description="A query server (`trajlab.store.serve`) answers /logs/agent/bin/query from "
        "the chain's store for the attempt's duration."
    )
    query_cap: int | None = Field(default=None, ge=1, description="Rows an answer holds at most.")
    query_timeout_s: float | None = Field(default=None, gt=0)
    query_memory_limit: str | None = None
    query_threads: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.ingests != (self.arm in RECORD_ARMS):
            raise ValueError(f"arm {self.arm}: only the record arms ingest")
        if self.mounts_bundle != (self.arm == "record-files"):
            raise ValueError(f"arm {self.arm}: only record-files mounts the bundle")
        if self.serves_queries != (self.arm == "record-engine"):
            raise ValueError(f"arm {self.arm}: only record-engine serves queries")
        required = (self.query_cap, self.query_timeout_s, self.query_memory_limit)
        if self.serves_queries and any(value is None for value in required):
            raise ValueError(f"arm {self.arm}: query settings exactly when it serves queries")
        if not self.serves_queries and any(
            value is not None for value in (*required, self.query_threads)
        ):
            raise ValueError(f"arm {self.arm}: query settings exactly when it serves queries")
        return self


class AttemptResult(_Model):
    """One attempt of a retry chain, written once the verifier has scored it (or the harness
    has failed it). Attempt 0, the chain's root failure, is the corpus trial and has no line."""

    run: str = Field(min_length=1, description="The Exp 2 run the chain belongs to.")
    arm: Exp2Arm
    root_trial: str = Field(min_length=1, description="The root failure's trial dir name.")
    root_trial_id: UUID
    task: str = Field(min_length=1, description="Harbor's task name.")
    attempt_idx: int = Field(ge=1, description="Its position in the chain; retries are 1 to N.")
    parent_trial_id: UUID = Field(description="The attempt it followed (attempt idx - 1).")
    job_name: str = Field(min_length=1, description="Its Harbor job, also its corpus id.")
    outcome: AttemptOutcome
    trial_name: str | None = Field(default=None, description="Null when no trial dir was made.")
    trial_id: UUID | None = Field(default=None, description="Null without a result.json.")
    reward: float | None = None
    failure_kind: FailureKind | None = Field(
        default=None, description="How it failed, from result.json (ADR-0012 item 4)."
    )
    exception_type: str | None = None
    prompt_sha256: str = Field(pattern=SHA256, description="Of the rendered prompt, UTF-8.")
    prompt_bytes: int = Field(ge=1)
    prompt_delivered: bool | None = Field(
        default=None,
        description="The trajectory's first user step equals the rendered prompt; null without "
        "a trajectory.",
    )
    store_trials: tuple[UUID, ...] = Field(
        default=(),
        description="The trials in the chain's store when the attempt started, in attempt "
        "order: attempts 0 to attempt_idx - 1 in a record arm, none in control.",
    )
    bundle: str | None = Field(
        default=None, description="record-files: the host dir mounted at RECORD_MOUNT."
    )
    queries: int | None = Field(
        default=None, ge=0, description="record-engine: entries of the attempt's query log."
    )
    query_tool_ready: bool | None = Field(
        default=None,
        description="record-engine: bin/query was in the agent dir before the agent's first step.",
    )
    preregistration: str | None = Field(
        default=None,
        description="The commit of the preregistration the attempt ran under: required for "
        "record-engine, recorded for every arm when there is one.",
    )
    started_at: AwareDatetime
    finished_at: AwareDatetime
    note: str | None = Field(default=None, description="Why an attempt is infra or lost.")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome == "passed" and (self.reward is None or self.reward < 1):
            raise ValueError("a passed attempt has reward 1")
        if self.outcome != "passed" and self.reward is not None and self.reward >= 1:
            raise ValueError(f"an attempt with reward {self.reward} passed")
        if self.outcome in COUNTED_OUTCOMES and self.trial_id is None:
            raise ValueError("a counted attempt has a trial_id")
        if self.outcome == "lost" and self.trial_id is not None:
            raise ValueError("a lost attempt has no result")
        if (self.failure_kind is not None) != (self.outcome in ("failed", "infra")):
            raise ValueError("failure_kind is set exactly for failed and infra attempts")
        if self.arm == "control" and self.store_trials:
            raise ValueError("control has no store")
        if self.arm in RECORD_ARMS and len(self.store_trials) != self.attempt_idx:
            raise ValueError(
                f"attempt {self.attempt_idx} ran with {len(self.store_trials)} trials in its "
                "store; a chain's store holds exactly the attempts before it"
            )
        if (self.bundle is not None) != (self.arm == "record-files"):
            raise ValueError("bundle is set exactly for record-files")
        if self.arm != "record-engine" and (
            self.queries is not None or self.query_tool_ready is not None
        ):
            raise ValueError("queries are recorded for record-engine only")
        if self.arm == "record-engine" and self.preregistration is None:
            raise ValueError("a record-engine attempt runs under a committed preregistration")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self

    @property
    def counted(self) -> bool:
        """An attempt the budget counts: one the verifier scored, or the agent itself failed."""
        return self.outcome in COUNTED_OUTCOMES


class AttemptLaunch(_Model):
    """What the loop decided when it started an attempt, written beside its job config
    (`<jobs dir>/_exp2-inputs/<job>/launch.json`) before `trajlab run` starts, so a loop that
    restarts while the attempt runs can wait for it and record it."""

    run: str = Field(min_length=1)
    arm: Exp2Arm
    root_trial: str = Field(min_length=1)
    attempt_idx: int = Field(ge=1)
    rerun: int = Field(ge=0, description="Earlier tries of this attempt the harness failed.")
    job_name: str = Field(min_length=1)
    parent_trial_id: UUID
    prompt_sha256: str = Field(pattern=SHA256)
    prompt_bytes: int = Field(ge=1)
    store_trials: tuple[UUID, ...] = ()
    bundle: str | None = None
    preregistration: str | None = None
    started_at: AwareDatetime
    pid: int | None = Field(default=None, description="Of its `trajlab run` process.")


class IngestResult(_Model):
    """One failed attempt built into its chain's store and the bundle exported after it
    (technical spec §5.2): postprocess, reconcile, derive, export, verify."""

    attempt_idx: int = Field(ge=1)
    trial_id: UUID
    trial_name: str = Field(min_length=1)
    ok: bool = Field(description="The rows are in the store and the next bundle verified.")
    error: str | None = Field(default=None, description="Why not; the chain stops (§5.2).")
    steps: int | None = Field(default=None, ge=0)
    deltas: int | None = Field(default=None, ge=0)
    chain_complete: bool | None = Field(
        default=None, description="The attempt's checkpoint chain (ADR-0014 item 8)."
    )
    chain_reasons: tuple[str, ...] = ()
    bundle: str | None = Field(
        default=None,
        description="record-files: the bundle exported after it, for the next attempt.",
    )
    images_pruned: int = Field(
        default=0, ge=0, description="Its checkpoint images removed after ingest (opt-in)."
    )
    started_at: AwareDatetime
    seconds: float = Field(ge=0, description="Postprocess, build, and export, end to end.")

    @model_validator(mode="after")
    def _ok_or_error(self) -> Self:
        if self.ok == (self.error is not None):
            raise ValueError("error is set exactly when the ingest failed")
        if self.ok and (self.steps is None or self.deltas is None):
            raise ValueError("a finished ingest has its counts")
        if self.images_pruned and not self.ok:
            raise ValueError("images are pruned only after a finished ingest")
        return self


class RootFailure(_Model):
    """A failed first attempt that roots one chain per arm (design spec §5)."""

    trial_name: str = Field(min_length=1)
    trial_id: UUID
    task: str = Field(min_length=1)
    instruction_sha256: str = Field(
        pattern=SHA256, description="Of its `trials.prompt`: the instruction every retry gets."
    )


class Exclusion(_Model):
    trial_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Exp2Settings(_Model):
    """What one Exp 2 run is, as its preregistration fixes it (technical spec §5.3).
    `trajlab exp2 prereg` writes it as the file's machine-read block; `trajlab exp2 run` runs
    exactly this and refuses when the code would render other prompts or views."""

    run: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="Prefix of its job names.")
    kind: Literal["full", "dry-run"]
    corpus_job: str = Field(min_length=1, description="The job dir of the root failures.")
    corpus_store: str = Field(min_length=1, description="Its store; attempt 0's rows.")
    corpus_config: str = Field(
        min_length=1, description="The job config every attempt copies (model, agent, timeouts)."
    )
    sample: tuple[RootFailure, ...] = Field(min_length=1)
    exclusions: tuple[Exclusion, ...] = ()
    budget: int = Field(ge=1, description="N: retries per chain at most.")
    report_at: tuple[int, ...]
    arms: tuple[Exp2Arm, ...] = Field(min_length=1)
    primary: str = Field(min_length=1)
    test: str = Field(min_length=1)
    schema_version: int
    derived: str = Field(min_length=1, description="The store's derived id (ADR-0018 item 4).")
    derived_views: tuple[str, ...]
    protocol_sha256: str = Field(pattern=SHA256, description="Of the four protocol steps.")
    prefix_sha256: dict[str, str] = Field(
        description="`<arm>/<k>` to the sha256 of the prompt before the instruction, k prior "
        "attempts; the whole prompt is that and the root's instruction."
    )
    hooks_sha256: str = Field(pattern=SHA256, description="Of the Claude Code settings file.")
    stop_hook: str | None = Field(description="None: no stop hook (ADR-0022).")
    query: ArmConfig | None = Field(description="record-engine's settings, caps included.")
    model: str = Field(min_length=1)
    agent_kwargs: dict[str, str] = Field(description="Effort and Claude Code version, as run.")
    agent_timeout_multiplier: float | None = None
    repo_sha: str = Field(pattern=r"^[0-9a-f]{40}$", description="HEAD when it was written.")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("an arm is listed twice")
        if any(n < 1 or n > self.budget for n in self.report_at):
            raise ValueError("report_at lies outside 1 to the budget")
        expected = {f"{arm}/{k}" for arm in self.arms for k in range(1, self.budget + 1)}
        if set(self.prefix_sha256) != expected:
            raise ValueError("prefix_sha256 has one hash per arm and k from 1 to the budget")
        if ("record-engine" in self.arms) != (self.query is not None):
            raise ValueError("query settings exactly when record-engine runs")
        if len({root.trial_name for root in self.sample}) != len(self.sample):
            raise ValueError("a root failure is listed twice")
        return self


__all__ = [
    "ATTEMPTS_FILENAME",
    "Exclusion",
    "Exp2Settings",
    "RootFailure",
    "COUNTED_OUTCOMES",
    "EXP2_ARMS",
    "INGESTS_FILENAME",
    "RECORD_ARMS",
    "RECORD_MOUNT",
    "ArmConfig",
    "AttemptLaunch",
    "AttemptOutcome",
    "AttemptResult",
    "Exp2Arm",
    "IngestResult",
    "QueryLogEntry",
]
