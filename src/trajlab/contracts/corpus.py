"""The corpus manifest: what produced a corpus's trials and where they live.

Written by `trajlab manifest` and `trajlab run` to `corpus/manifests/<corpus_id>.json`.
Glossary: "corpus", "`corpus_id` / corpus manifest".
"""

from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from trajlab.contracts.trial import TrialRecord


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CorpusTask(_ManifestModel):
    """One task in the corpus, pinned by the digest Harbor resolved into lock.json."""

    name: str = Field(min_length=1, description="e.g. `terminal-bench/regex-chess`.")
    digest: str = Field(min_length=1, description="`task.digest` from the job's lock.json.")


class CorpusJob(_ManifestModel):
    """One Harbor job dir that contributes trials to the corpus."""

    job_name: str = Field(min_length=1, description="The job dir name under corpus/jobs/.")
    job_id: UUID = Field(description="`id` in the job's result.json.")
    job_config: dict[str, Any] = Field(
        description="The job's config.json verbatim: the exact Harbor input for this job."
    )
    trials: list[TrialRecord]


class CorpusManifest(_ManifestModel):
    """Everything needed to say how a corpus was produced and to find its trials.

    Fields that must agree across every job in the corpus (agent, model, attempts, timeouts,
    Harbor version) are stored once; a disagreement means the jobs are not one corpus.
    """

    corpus_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    created_at: AwareDatetime
    harbor_version: str = Field(min_length=1, description="From the jobs' lock.json.")
    repo_sha: str = Field(min_length=40, max_length=40, description="This repo's git HEAD.")
    repo_dirty: bool = Field(description="True if the working tree had uncommitted changes.")
    config_path: str | None = Field(
        default=None, description="Repo-relative job config under configs/harbor/, if one was used."
    )
    agent_name: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    agent_kwargs: dict[str, Any] = Field(
        description="`agents[0].kwargs`, e.g. the Claude Code version and reasoning effort."
    )
    environment_type: str = Field(min_length=1, description="e.g. `docker`.")
    n_attempts: int = Field(ge=1)
    timeout_multiplier: float = Field(gt=0)
    agent_timeout_multiplier: float | None = Field(default=None, gt=0)
    checkpoint_every: int | None = Field(
        ge=1,
        description="N from the trials' policy.json (ADR-0007); null without checkpoints.",
    )
    tasks: list[CorpusTask] = Field(min_length=1)
    jobs: list[CorpusJob] = Field(min_length=1)
    storage: str | None = Field(
        default=None, description="Where the job dirs live outside git (corpus/README.md)."
    )
