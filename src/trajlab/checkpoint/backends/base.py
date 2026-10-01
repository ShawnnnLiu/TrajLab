"""The snapshot backend interface (docs/checkpoint-protocol.md, "Participants")."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
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
    def discard(self, snapshot: Snapshot) -> None:
        """Delete a snapshot that will not be recorded. Raises BackendError."""

    def restore(self, snapshot: Snapshot) -> str:
        raise NotImplementedError("restore is not part of the capture phase")

    def fork(self, snapshot: Snapshot) -> str:
        raise NotImplementedError("fork is not part of the capture phase")
