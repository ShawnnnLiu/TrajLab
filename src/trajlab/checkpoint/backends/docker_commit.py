"""The `docker_commit` backend: one `docker commit` image per checkpoint (ADR-0004, ADR-0006).

`docker commit` pauses the container while it runs, so the hook waiting inside it cannot give
up mid-snapshot. Bind mounts, including /logs/agent, are not part of the image.

`docker commit` copies the container's labels onto the image, including Compose's project
label, and Harbor tears a trial down with `docker compose down --rmi local`, which deletes every
image carrying that label. Each checkpoint therefore blanks the container's
`com.docker.compose.*` labels, or Harbor deletes it when the trial ends (ADR-0006).
"""

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from trajlab.checkpoint.backends.base import BackendError, Snapshot, SnapshotBackend
from trajlab.checkpoint.changes import FIND_PRINTF

logger = logging.getLogger(__name__)

REPOSITORY = "trajlab-checkpoint"
COMPOSE_LABEL_PREFIX = "com.docker.compose."
# Below the hook's default 240 s ack wait: a slower commit would be discarded anyway.
COMMAND_TIMEOUT_S = 230
_TAG_INVALID = re.compile(r"[^A-Za-z0-9_.-]")
_TAG_MAX = 128

Runner = Callable[..., subprocess.CompletedProcess[str]]


def image_tag(trial_name: str, seq: int) -> str:
    """`<trial_name>.<seq:04d>`, made a valid Docker tag. The exact name is in the labels."""
    suffix = f".{seq:04d}"
    stem = _TAG_INVALID.sub("_", trial_name)[: _TAG_MAX - len(suffix)]
    if not stem or not (stem[0].isalnum() or stem[0] == "_"):
        stem = f"_{stem}"[: _TAG_MAX - len(suffix)]
    return stem + suffix


class DockerCommitBackend(SnapshotBackend):
    name = "docker_commit"

    def __init__(self, docker: str = "docker", runner: Runner = subprocess.run) -> None:
        self._docker_bin = docker
        self._run = runner
        self._compose_labels: dict[str, list[str]] = {}
        self._has_gnu_find: dict[str, bool] = {}

    def _docker(self, *args: str) -> str:
        command = [self._docker_bin, *args]
        try:
            completed: Any = self._run(
                command, check=True, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S
            )
        except subprocess.CalledProcessError as error:
            raise BackendError(f"{' '.join(command)}: {error.stderr.strip()}") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackendError(f"{' '.join(command)}: {error}") from error
        return completed.stdout.strip()

    def container_for(self, compose_project: str) -> str:
        ids = self._docker(
            "ps",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
            "--filter",
            "label=com.docker.compose.service=main",
            "--format",
            "{{.ID}}",
        ).split()
        if len(ids) != 1:
            raise BackendError(
                f"expected one running main container for compose project {compose_project!r}, "
                f"found {len(ids)}"
            )
        return ids[0]

    def compose_labels(self, container: str) -> list[str]:
        """The container's Compose label keys, which its images must not inherit. Cached."""
        if container not in self._compose_labels:
            raw = self._docker(
                "container", "inspect", "--format", "{{json .Config.Labels}}", container
            )
            keys = json.loads(raw) or {}
            self._compose_labels[container] = sorted(
                key for key in keys if key.startswith(COMPOSE_LABEL_PREFIX)
            )
        return self._compose_labels[container]

    def snapshot(self, container: str, *, tag: str, labels: Mapping[str, str]) -> Snapshot:
        scrubbed = dict.fromkeys(self.compose_labels(container), "")
        changes = [
            f"--change=LABEL {key}={json.dumps(value)}"
            for key, value in (scrubbed | dict(labels)).items()
        ]
        start = time.monotonic()
        image_id = self._docker("commit", *changes, container, f"{REPOSITORY}:{tag}")
        capture_ms = round((time.monotonic() - start) * 1000)
        captured_at = datetime.now(UTC)
        if not image_id.startswith("sha256:"):
            raise BackendError(f"docker commit printed {image_id!r}, not an image id")
        return Snapshot(
            checkpoint_id=image_id,
            capture_ms=capture_ms,
            captured_at=captured_at,
            bytes=self._writable_layer_bytes(container),
        )

    def _writable_layer_bytes(self, container: str) -> int | None:
        # `image inspect .Size` double-counts under the containerd snapshotter (ADR-0006).
        try:
            size = self._docker(
                "container", "inspect", "--size", "--format", "{{.SizeRw}}", container
            )
            return int(size)
        except (BackendError, ValueError):
            logger.warning("could not read the writable-layer size of %s", container)
            return None

    def listing(self, container: str) -> str | None:
        if container not in self._has_gnu_find:
            try:
                version = self._docker("exec", "-u", "0", container, "find", "--version")
            except BackendError:
                version = ""
            self._has_gnu_find[container] = "GNU findutils" in version
            if not self._has_gnu_find[container]:
                logger.warning("%s has no GNU find; every call will be checkpointed", container)
        if not self._has_gnu_find[container]:
            return None
        command = [
            self._docker_bin, "exec", "-u", "0", container,
            "find", "/", "-xdev", "-printf", FIND_PRINTF,
        ]  # fmt: skip
        try:
            completed: Any = self._run(
                command,
                check=False,
                capture_output=True,
                text=True,
                errors="surrogateescape",
                timeout=COMMAND_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackendError(f"find in {container}: {error}") from error
        # find exits 1 when a file vanishes mid-walk; the rest of the listing is still valid.
        if completed.returncode not in (0, 1) or not completed.stdout:
            raise BackendError(
                f"find in {container} exited {completed.returncode}: {completed.stderr}"
            )
        if completed.returncode == 1:
            logger.info("find in %s: %s", container, completed.stderr.strip()[:200])
        return completed.stdout

    def discard(self, snapshot: Snapshot) -> None:
        self._docker("image", "rm", "--force", snapshot.checkpoint_id)
