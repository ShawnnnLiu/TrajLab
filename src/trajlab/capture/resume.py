"""Start a trial from a checkpoint image instead of the task's derived image.

Resuming an agent mid-trial needs the environment state and the conversation at the same
point. Harbor's `load_trajectory` seeds the conversation (Claude Code runs with `--resume`);
this environment supplies the state, by starting the trial container from a `docker_commit`
checkpoint of an earlier trial. Use it as

    "environment": {"import_path": "trajlab.capture.resume:CheckpointResumeEnvironment",
                    "kwargs": {"checkpoint_image": "trajlab-checkpoint:<trial>.<seq>"}}

Pass the checkpoint by tag: Harbor's teardown (`compose down --rmi local`) removes images
without a custom tag that the project used.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, override

from harbor.environments.docker.docker import DockerEnvironment

from trajlab.capture.preinstall import PreinstalledDockerEnvironment, PreinstallError
from trajlab.contracts import (
    PREINSTALL_RECORD_FILENAME,
    RESUME_RECORD_FILENAME,
    ResumeRecord,
)

logger = logging.getLogger(__name__)


def descends_from(image_layers: list[str], base_layers: list[str]) -> bool:
    """True if an image's layers extend the base image's: a commit adds layers on top."""
    return len(image_layers) > len(base_layers) and image_layers[: len(base_layers)] == base_layers


class CheckpointResumeEnvironment(PreinstalledDockerEnvironment):
    """Harbor's Docker environment, started from a checkpoint of this task's derived image."""

    def __init__(self, *args: Any, checkpoint_image: str, **kwargs: Any) -> None:
        self.checkpoint_image = checkpoint_image
        super().__init__(*args, **kwargs)

    async def _layers(self, image: str) -> list[str]:
        raw = await self.docker("image", "inspect", "--format", "{{json .RootFS.Layers}}", image)
        return list(json.loads(raw))

    @override
    async def start(self, force_build: bool) -> None:
        if not self.is_agent_environment():
            await DockerEnvironment.start(self, force_build=force_build)
            return
        record = await self.prepare_image(force_build)
        checkpoint_id = await self.docker.image_id(self.checkpoint_image)
        if checkpoint_id is None:
            raise PreinstallError(f"checkpoint image {self.checkpoint_image} is not available")
        if not descends_from(
            await self._layers(self.checkpoint_image), await self._layers(record.image)
        ):
            raise PreinstallError(
                f"{self.checkpoint_image} is not a checkpoint of this task's image {record.image}"
            )
        self.task_env_config = self.task_env_config.model_copy(
            update={"docker_image": self.checkpoint_image}
        )
        self._env_vars.prebuilt_image_name = self.checkpoint_image
        trial_dir = self.trial_paths.trial_dir
        (trial_dir / PREINSTALL_RECORD_FILENAME).write_text(record.model_dump_json(indent=2) + "\n")
        resume = ResumeRecord(
            checkpoint_image=self.checkpoint_image,
            checkpoint_image_id=checkpoint_id,
            derived_image=record.image,
            recorded_at=datetime.now(UTC),
        )
        (trial_dir / RESUME_RECORD_FILENAME).write_text(resume.model_dump_json(indent=2) + "\n")
        logger.info("starting from checkpoint %s (%s)", self.checkpoint_image, checkpoint_id)
        await DockerEnvironment.start(self, force_build=False)
        await self.verify_trial_agent(record)


class SessionCutError(ValueError):
    """The native session has no result for the tool call to resume after."""


def _answers(event: dict[str, Any], tool_call_id: str) -> bool:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return (
        event.get("type") == "user"
        and isinstance(content, list)
        and any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") == tool_call_id
            for block in content
        )
    )


def cut_session(lines: list[str], tool_call_id: str) -> list[str]:
    """Claude Code's native session JSONL up to and including the result of `tool_call_id`.

    `claude --resume` on the cut file continues as if the agent had just received that tool
    result, which is the state the call's checkpoint holds. Lines are kept verbatim, so the
    model's thinking blocks and their signatures survive (an ATIF round trip would lose them).
    """
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        event = json.loads(line)
        if isinstance(event, dict) and _answers(event, tool_call_id):
            return lines[: index + 1]
    raise SessionCutError(f"no tool_result for {tool_call_id} in the session")
