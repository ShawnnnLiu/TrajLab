"""What image a trial ran on when the agent was pre-installed (ADR-0008).

Written by `PreinstalledDockerEnvironment` to `<trial>/trajlab-preinstall.json`.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

PREINSTALL_RECORD_FILENAME = "trajlab-preinstall.json"


class PreinstallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_image: str = Field(min_length=1, description="The task image the derived one is built on.")
    task_image_id: str = Field(min_length=1)
    task_content_key: str = Field(
        min_length=1, description="Hash of the task image's layers and config; the cache key."
    )
    image: str = Field(min_length=1, description="The derived image the trial ran on.")
    image_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    agent_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="sha256 of the installed agent binary (ADR-0009)."
    )
    harbor_version: str = Field(min_length=1)
    recipe_version: int = Field(ge=1)
    cache_hit: bool = Field(description="True if the derived image already existed.")
    build_seconds: float | None = Field(default=None, ge=0, description="Null on a cache hit.")
    recorded_at: AwareDatetime
