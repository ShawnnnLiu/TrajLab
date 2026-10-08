"""What a trial run on Waypoint records about its Waypoint session (ADR-0013).

Written by `trajlab.capture.waypoint` to `<trial dir>/trajlab-waypoint.json`. A first attempt
records the session it ran in and its save (`final`): the files and running programs at the end
of the agent's turn, before the tests. A repair that opened a copy of a save records which save.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

WAYPOINT_RECORD_FILENAME = "trajlab-waypoint.json"
# The one save a first attempt makes (ADR-0013 decision 2).
FINAL_CHECKPOINT = "final"

# source: a trial that starts from the task image (first attempts; `fresh` and `traj` repairs).
# fork:   a repair that starts from a copy of a first attempt's save.
WaypointRole = Literal["source", "fork"]


class RunningProcess(BaseModel):
    """One program running in the environment, as seen from inside it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(ge=1, description="Inside the environment's own process list.")
    command: str = Field(description="Its command line, cut to 200 characters.")


class WaypointSave(BaseModel):
    """The save a first attempt made when its agent stopped."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str = FINAL_CHECKPOINT
    path: str = Field(min_length=1, description="The save's directory on the host.")
    agent_stopped: Literal["ended", "timeout_or_error"] = Field(
        description="ended: Claude Code had exited; timeout_or_error: it was still running and "
        "was killed before the save."
    )
    killed_agent_pids: tuple[int, ...] = Field(
        default=(), description="Claude Code processes killed just before the save."
    )
    processes: tuple[RunningProcess, ...] = Field(
        description="What was running at the save, besides Waypoint's own shell."
    )
    capture_ms: int = Field(ge=0, description="Wall time of `waypoint snapshot`.")
    files_bytes: int | None = Field(default=None, ge=0, description="Size of the save's upper/.")
    memory_bytes: int | None = Field(default=None, ge=0, description="Size of the save's criu/.")
    secret_found_in: tuple[str, ...] = Field(
        default=(), description="Files of the save that contain an API key or token."
    )
    saved_at: AwareDatetime


class WaypointRecord(BaseModel):
    """Where a trial ran: its Waypoint session, its copy, and for a first attempt, its save."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: WaypointRole
    waypoint_version: str = Field(min_length=1)
    sessions_dir: str = Field(min_length=1)
    session: str = Field(min_length=1, description="The Waypoint session id.")
    fork: str = Field(min_length=1, description='"main" for a source trial.')
    opened_from: str | None = Field(
        default=None, description='For a fork: the save it opened, "<session>/<checkpoint>".'
    )
    stopped_processes: tuple[RunningProcess, ...] | None = Field(
        default=None,
        description="For a fork that keeps only the files (`state-files`): the programs that "
        "were running in the save and were stopped before the agent started.",
    )
    image: str = Field(min_length=1, description="The pre-installed image the session came from.")
    network: str | None = Field(
        default=None, description="The trial's own network namespace, if it had one."
    )
    save: WaypointSave | None = None
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _role_fields(self) -> Self:
        if (self.role == "fork") != (self.opened_from is not None):
            raise ValueError("opened_from is set exactly for a fork")
        if self.role == "fork" and self.save is not None:
            raise ValueError("a fork makes no save")
        if self.role == "source" and self.fork != "main":
            raise ValueError('a source trial runs in the "main" fork')
        if self.stopped_processes is not None and self.role != "fork":
            raise ValueError("only a fork stops the programs it opened with")
        return self
