"""What a resumed trial started from: a checkpoint of an earlier trial.

Written by `CheckpointResumeEnvironment` to `<trial>/trajlab-resume.json`.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

RESUME_RECORD_FILENAME = "trajlab-resume.json"


class ResumeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_image: str = Field(min_length=1, description="The checkpoint image as given.")
    checkpoint_image_id: str = Field(min_length=1)
    derived_image: str = Field(
        min_length=1, description="The task's derived image the checkpoint was taken on."
    )
    recorded_at: AwareDatetime
