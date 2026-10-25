"""Checkpoint images as layer tars for `reconcile` (ADR-0014). The only store module that runs
`docker`.

`DockerImageProvider.chain` turns a trial's `CheckpointRecord`s into an `ImageChain`:

1. Each record's image is looked up by its `checkpoint_id` and referenced by its
   `trajlab-checkpoint:` tag from then on, since Harbor's teardown removes untagged images a
   trial used (trajlab.capture.resume). An image that is gone, or has no such tag, gives its
   seq no top layer; the build reports that seq as a missing image.
2. The `.RootFS.Layers` of every found image must be the start image's layers plus at most
   one. `docker commit` adds the container's whole writable layer as one layer, and adds no
   layer at all when the container has no changes (Docker 29.8, containerd store, checked
   2026-10-18). The start image is required: without it, a commit that added no layer would
   be indistinguishable from one that did, and the start image's own last layer would be read
   as the checkpoint's changes.
3. One `docker image save -o <file>` writes every found image, shared layers once. The save
   lands in a file because its tar puts `manifest.json` after the blobs (OCI layout), so a
   stream would have to be read to its end before any layer could be found.
4. `manifest.json` maps each tag to its layer members. The members every image starts with are
   the start image's layers; an image's member after them is its top layer. An image with no
   layer over the start image gets an empty top layer: its view is the start image.

A `SavedLayer` reads its member straight out of the save file, which must exist for as long as
the chain is used. Saves go to a directory of the provider's own, under `work_dir`, which
`close()` removes with anything in it (the docker CLI writes its own temporary file there, and
leaves it behind if killed). A save holds the start image plus every checkpoint's top layer,
and top layers are cumulative (each is the whole writable layer), so a build should use one
provider per trial and close it after the trial.
"""

import io
import json
import logging
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

from trajlab.contracts import CheckpointRecord
from trajlab.store.reconcile import ImageChain, LayerSource

logger = logging.getLogger(__name__)

# The docker_commit backend's REPOSITORY (trajlab.checkpoint.backends.docker_commit); the store
# does not import `checkpoint`, and a test keeps the two equal.
CHECKPOINT_REPOSITORY = "trajlab-checkpoint"
MANIFEST_MEMBER = "manifest.json"
INSPECT_TIMEOUT_S = 120
# A save writes the start image (often over 1 GB) and every top layer.
SAVE_TIMEOUT_S = 1800
_NO_SUCH_IMAGE = "no such image"
_READ_BUFFER = 1 << 20

Runner = Callable[..., subprocess.CompletedProcess[str]]


class DockerImagesError(ValueError):
    """A docker command failed, or an image is not a commit over the start image."""


def _empty_tar() -> bytes:
    buffer = io.BytesIO()
    tarfile.open(fileobj=buffer, mode="w").close()
    return buffer.getvalue()


_EMPTY_TAR = _empty_tar()


@dataclass(frozen=True)
class EmptyLayer:
    """The top layer of a commit that added none: a tar with no members."""

    def open(self) -> BinaryIO:
        return io.BytesIO(_EMPTY_TAR)


@dataclass(frozen=True)
class SavedLayer:
    """One layer blob of a `docker image save` file: `size` bytes at `offset` in `archive`.

    The bytes are the blob as saved (gzip for the containerd store); the reader decompresses.
    """

    archive: Path
    member: str
    offset: int
    size: int

    def open(self) -> BinaryIO:
        """A seekable stream of the blob; closing it closes the save file."""
        raw = _Slice(self.archive.open("rb", buffering=0), self.offset, self.size)
        return io.BufferedReader(raw, _READ_BUFFER)


class _Slice(io.RawIOBase):
    """Bytes [offset, offset + size) of a file, as a seekable raw stream that owns the file."""

    def __init__(self, file: io.FileIO, offset: int, size: int) -> None:
        self._file = file
        self._start = offset
        self._size = size
        self._position = 0
        super().__init__()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self._size - self._position
        if remaining <= 0:
            return 0
        view = memoryview(buffer).cast("B")[:remaining]
        self._file.seek(self._start + self._position)
        count = self._file.readinto(view) or 0
        self._position += count
        return count

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        if position < 0:
            raise ValueError(f"negative seek position {position}")
        self._position = position
        return position

    def tell(self) -> int:
        return self._position

    def close(self) -> None:
        if not self.closed:
            self._file.close()
        super().close()


@dataclass(frozen=True)
class _Found:
    """A checkpoint's image, by tag, and its `.RootFS.Layers` (diff ids, bottom to top)."""

    seq: int
    tag: str
    layers: tuple[str, ...]


class DockerImageProvider:
    """The `ImageChain` of a trial's checkpoints, from the local Docker daemon.

    `work_dir` holds the provider's save directory until it is closed; it needs room for the
    start image plus every checkpoint's top layer, compressed. `start_image` is the image the
    trial's container started from (`trajlab.store.build.start_image` reads it from the trial
    dir): the derived image named in `trajlab-preinstall.json`, or for a trial resumed from a
    checkpoint, that checkpoint image. `runner` stands in for `subprocess.run` in tests.
    """

    def __init__(
        self,
        work_dir: Path,
        *,
        start_image: str,
        docker: str = "docker",
        runner: Runner = subprocess.run,
    ) -> None:
        self.work_dir = work_dir
        self.start_image = start_image
        self._docker_bin = docker
        self._run = runner
        self._archives: list[Path] = []
        self._save_dir: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Delete this provider's save directory and everything in it; chains it returned can
        no longer open."""
        for archive in self._archives:
            archive.unlink(missing_ok=True)
        self._archives.clear()
        if self._save_dir is not None:
            shutil.rmtree(self._save_dir, ignore_errors=True)
            self._save_dir = None

    def chain(self, checkpoints: Sequence[CheckpointRecord]) -> ImageChain:
        """The start image's layers and each found checkpoint's top layer, keyed by seq.

        `checkpoints` are one trial's records, in any order. A record whose image is gone or
        untagged has no entry in `tops`. Raises `DockerImagesError` if a docker command fails
        or an image is not a commit over the start image.
        """
        seqs = [record.seq for record in checkpoints]
        if len(set(seqs)) != len(seqs):
            raise DockerImagesError(f"checkpoints repeat a seq: {sorted(seqs)}")
        found: list[_Found] = []
        for record in sorted(checkpoints, key=lambda r: r.seq):
            image = self._find(record)
            if image is not None:
                found.append(image)
        if not found:
            return ImageChain(start_layers=(), tops={})
        start = self._start_layers(found)
        tags = list(dict.fromkeys(image.tag for image in found))
        archive = self._save(tags)
        try:
            return self._assemble(archive, found, start, self._read_manifest(archive, tags))
        except BaseException:
            self._discard(archive)
            raise

    # -- docker ----------------------------------------------------------------------------

    def _docker(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        command = [self._docker_bin, *args]
        logger.debug("running %s", shlex.join(command))
        try:
            return self._run(command, check=False, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DockerImagesError(f"{shlex.join(command)}: {error}") from error

    def _inspect(self, ref: str, template: str) -> Any | None:
        """`docker image inspect --format <template> <ref>` as JSON, or None if no such image."""
        completed = self._docker(
            ["image", "inspect", "--format", template, ref], timeout=INSPECT_TIMEOUT_S
        )
        if completed.returncode != 0:
            if _NO_SUCH_IMAGE in completed.stderr.lower():
                return None
            raise DockerImagesError(_failure(completed))
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise DockerImagesError(
                f"{shlex.join(completed.args)} printed {completed.stdout!r}: {error}"
            ) from error

    def _find(self, record: CheckpointRecord) -> _Found | None:
        where = f"checkpoint {record.seq} of {record.trial_name} ({record.checkpoint_id})"
        tags = self._inspect(record.checkpoint_id, "{{json .RepoTags}}")
        if tags is None:
            logger.warning("%s: image not found", where)
            return None
        ours = sorted(tag for tag in tags or [] if tag.startswith(f"{CHECKPOINT_REPOSITORY}:"))
        if not ours:
            logger.warning("%s: image has no %s: tag", where, CHECKPOINT_REPOSITORY)
            return None
        if len(ours) > 1:
            logger.info("%s: image has tags %s; using %s", where, ours, ours[0])
        layers = self._layers(ours[0])
        if layers is None:
            logger.warning("%s: image %s went away", where, ours[0])
            return None
        return _Found(seq=record.seq, tag=ours[0], layers=layers)

    def _layers(self, ref: str) -> tuple[str, ...] | None:
        layers = self._inspect(ref, "{{json .RootFS.Layers}}")
        if layers is None:
            return None
        if not isinstance(layers, list) or not all(isinstance(layer, str) for layer in layers):
            raise DockerImagesError(f"{ref}: .RootFS.Layers is {layers!r}, not a list of ids")
        return tuple(layers)

    def _start_layers(self, found: Sequence[_Found]) -> tuple[str, ...]:
        """The start image's diff ids; raises unless every image is a commit over it."""
        start = self._layers(self.start_image)
        if start is None:
            raise DockerImagesError(f"start image {self.start_image} not found")
        for image in found:
            if image.layers == start:
                logger.info(
                    "checkpoint %d (%s) has no layer over the start image: its container had "
                    "no changes",
                    image.seq,
                    image.tag,
                )
            elif image.layers[:-1] != start:
                raise DockerImagesError(
                    f"{image.tag} is not a commit over the start image {self.start_image}: "
                    f"{len(image.layers)} layers, {_overlap(image.layers, start)} of them the "
                    f"start image's {len(start)}; a commit adds at most one layer"
                )
        return start

    def _save(self, tags: Sequence[str]) -> Path:
        if self._save_dir is None:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            self._save_dir = Path(tempfile.mkdtemp(prefix="trajlab-images-", dir=self.work_dir))
        descriptor, name = tempfile.mkstemp(prefix="images-", suffix=".tar", dir=self._save_dir)
        os.close(descriptor)
        archive = Path(name).absolute()  # SavedLayer opens it later, maybe from another cwd
        self._archives.append(archive)
        start = time.monotonic()
        try:
            completed = self._docker(
                ["image", "save", "-o", str(archive), *tags], timeout=SAVE_TIMEOUT_S
            )
            if completed.returncode != 0:
                raise DockerImagesError(_failure(completed))
        except BaseException:
            self._discard(archive)
            raise
        seconds = time.monotonic() - start
        logger.info(
            "saved %d checkpoint images in %.1f s: %.1f MB in %s",
            len(tags),
            seconds,
            archive.stat().st_size / 1e6,
            archive,
        )
        return archive

    def _discard(self, archive: Path) -> None:
        archive.unlink(missing_ok=True)
        if archive in self._archives:
            self._archives.remove(archive)

    # -- the save file ---------------------------------------------------------------------

    def _read_manifest(self, archive: Path, tags: Sequence[str]) -> dict[str, list[SavedLayer]]:
        """Each tag's layer members, bottom to top, located in the save file."""
        # A save cut short (a full disk) is a tar that ends early: `tarfile` raises its own
        # errors for that, which are neither ValueError nor OSError.
        try:
            with tarfile.open(archive, mode="r:") as outer:
                entries = {member.name: member for member in outer.getmembers()}
                manifest_info = entries.get(MANIFEST_MEMBER)
                stream = outer.extractfile(manifest_info) if manifest_info is not None else None
                if stream is None:
                    raise DockerImagesError(f"{archive} has no {MANIFEST_MEMBER}")
                try:
                    manifest = json.load(stream)
                except ValueError as error:  # bad JSON, or bytes that are not text
                    raise DockerImagesError(f"{archive}: bad {MANIFEST_MEMBER}: {error}") from error
        except (tarfile.TarError, EOFError) as error:
            raise DockerImagesError(f"{archive}: cannot read the save: {error}") from error
        if not isinstance(manifest, list) or not all(isinstance(image, dict) for image in manifest):
            raise DockerImagesError(f"{archive}: {MANIFEST_MEMBER} is not a list of images")
        layers_by_tag: dict[str, list[SavedLayer]] = {}
        for image in manifest:
            layers: list[SavedLayer] = []
            for name in image.get("Layers") or []:
                info = entries.get(name)
                if info is None or not info.isfile():
                    raise DockerImagesError(f"{archive}: layer {name} is not a file in the save")
                layers.append(SavedLayer(archive, name, info.offset_data, info.size))
            for tag in image.get("RepoTags") or []:
                layers_by_tag[tag] = layers
        missing = [tag for tag in tags if tag not in layers_by_tag]
        if missing:
            raise DockerImagesError(f"{archive}: {MANIFEST_MEMBER} does not list {missing}")
        return layers_by_tag

    def _assemble(
        self,
        archive: Path,
        found: Sequence[_Found],
        start: tuple[str, ...],
        members: Mapping[str, list[SavedLayer]],
    ) -> ImageChain:
        start_members = members[found[0].tag][: len(start)]
        tops: dict[int, LayerSource] = {}
        for image in found:
            layers = members[image.tag]
            if len(layers) != len(image.layers):
                raise DockerImagesError(
                    f"{archive}: {image.tag} has {len(layers)} layers in {MANIFEST_MEMBER} but "
                    f"{len(image.layers)} in docker image inspect"
                )
            if [layer.member for layer in layers[: len(start)]] != [
                layer.member for layer in start_members
            ]:
                raise DockerImagesError(
                    f"{archive}: {image.tag} and {found[0].tag} store the start image's layers "
                    "as different blobs"
                )
            tops[image.seq] = layers[-1] if len(layers) > len(start) else EmptyLayer()
        return ImageChain(start_layers=tuple(start_members), tops=tops)


def _failure(completed: subprocess.CompletedProcess[str]) -> str:
    command = shlex.join(completed.args)
    return f"{command} exited {completed.returncode}: {completed.stderr.strip()}"


def _overlap(layers: Sequence[str], start: Sequence[str]) -> int:
    count = 0
    for mine, theirs in zip(layers, start, strict=False):
        if mine != theirs:
            break
        count += 1
    return count
