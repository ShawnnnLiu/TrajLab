"""The snapshot backend interface (docs/checkpoint-protocol.md, "Participants")."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime


class BackendError(RuntimeError):
    """The backend could not find a container or take, or discard, a snapshot."""


@dataclass(frozen=True)
class Snapshot:
    """What a backend reports about one snapshot; the watcher turns it into a CheckpointRecord."""

    checkpoint_id: str
    capture_ms: int
    captured_at: datetime
    bytes: int | None = None
    path: str | None = None


class SnapshotBackend(ABC):
    """Takes checkpoints of a trial's running environment."""

    name: str

    @abstractmethod
    def container_for(self, compose_project: str) -> str:
        """The id of the main container of a Harbor compose project. Raises BackendError."""

    @abstractmethod
    def snapshot(self, container: str, *, tag: str, labels: Mapping[str, str]) -> Snapshot:
        """Capture the container's state now. Raises BackendError."""

    @abstractmethod
    def listing(self, container: str) -> str | None:
        """The container's root filesystem as `trajlab.checkpoint.changes` find output.

        None if the container has no GNU find; the watcher then checkpoints every call.
        Raises BackendError if the listing fails.
        """

    @abstractmethod
    def discard(self, snapshot: Snapshot) -> None:
        """Delete a snapshot that will not be recorded. Raises BackendError."""

    # Detector capture (ADR-0015 §2). Optional: None means the backend cannot, and the watcher
    # then records nothing of that kind. Each gives up after `timeout` seconds (BackendError),
    # since all of them run while the agent waits for its ack.

    def processes(self, container: str, *, timeout: float) -> str | None:
        """`trajlab.checkpoint.processes.PROBE_SCRIPT` output from the container."""
        return None

    def read_files(
        self, container: str, paths: Sequence[str], *, timeout: float
    ) -> dict[str, bytes] | None:
        """The current content of those regular files that exist, by absolute path."""
        return None

    def image_of(self, container: str, *, timeout: float) -> str | None:
        """The id of the image the container was started from."""
        return None

    def read_image_files(
        self, image: str, paths: Sequence[str], *, timeout: float
    ) -> dict[str, bytes] | None:
        """The content of those regular files that exist in an image, by absolute path."""
        return None

    def restore(self, snapshot: Snapshot) -> str:
        raise NotImplementedError("restore is not part of the capture phase")

    def fork(self, snapshot: Snapshot) -> str:
        raise NotImplementedError("fork is not part of the capture phase")
