"""Identity of one trial, joining Harbor's ids with Claude Code's session id."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TrialRecord(BaseModel):
    """Where a trial lives in a corpus and which ids name it.

    Everything else about a trial (reward, tokens, timings, exceptions) stays in Harbor's
    `TrialResult`; this record does not copy it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: UUID = Field(description="Harbor's trial id, `id` in result.json.")
    trial_name: str = Field(min_length=1, description="The trial dir name, `<task>__<id>`.")
    task_name: str = Field(min_length=1, description="e.g. `hello-world/hello-world`.")
    job_id: UUID | None = Field(default=None, description="`job_id` in config.json.")
    job_dir: str = Field(
        min_length=1,
        description="Job dir name under corpus/jobs/; the trial dir is <job_dir>/<trial_name>.",
    )
    claude_session_id: str | None = Field(
        default=None,
        description="Claude Code session id: the ATIF root session_id and the native JSONL name. "
        "Not Harbor's environment session id.",
    )
