"""Read one OCI image layer tar: its entries, whiteouts, and opaque directories (ADR-0014).

Pure: functions over binary streams, with no Docker and no subprocess. The layer format and the
rules for applying layers are in `trajlab.store.reconcile`'s module docstring; this module reads
one layer, and `Layer.hidden` is the per-layer half of the view rule there.

Layers are read sequentially (`tarfile` stream mode, gzip or plain), because `docker image save`
writes them as members of one archive and a layer can be larger than memory. A file's content is
handed to a `FileHook` while the layer streams past; the hook returns the content's sha256 and
can keep what it needs, so nothing holds a whole layer.
"""

import hashlib
import logging
import tarfile
import zlib
from bisect import bisect_left
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol

from trajlab.contracts import EntryKind

logger = logging.getLogger(__name__)

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"
_CHUNK = 1 << 20


class LayerError(ValueError):
    """A layer cannot be read: not a tar, truncated, or with a member it cannot place."""


def normalize(name: str) -> str | None:
    """A tar member name as an absolute posix path; None for the root entry (`.`, `./`).

    Empty and `.` components are dropped, so leading `./` or `/` and a trailing `/` go too.
    (`name.lstrip("./")` would be wrong: it turns `./.bashrc` into `bashrc`.)
    """
    parts = [part for part in name.split("/") if part and part != "."]
    if ".." in parts:
        raise LayerError(f"member {name!r} leaves the layer's root")
    return "/" + "/".join(parts) if parts else None


def ancestors(path: str) -> Iterator[str]:
    """The strict ancestors of an absolute path, nearest first, ending with `/`."""
    while path != "/":
        path = path.rpartition("/")[0] or "/"
        yield path


@dataclass(frozen=True, slots=True)
class Entry:
    """One path's state in a layer. Times are not kept: they are never compared."""

    kind: EntryKind
    mode: int  # permission bits: tar mode & 0o7777
    uid: int
    gid: int
    size: int | None = None  # file: content bytes; symlink: UTF-8 bytes of its target
    sha256: str | None = None  # file: of its content; symlink: of its target (UTF-8)
    link_target: str | None = None  # symlink only

    @property
    def state(self) -> tuple[str, int, int, int, str | None]:
        """What two entries are compared by: kind, permission bits, owner, content."""
        return (self.kind, self.mode, self.uid, self.gid, self.sha256)


def file_entry(info: tarfile.TarInfo, sha256: str) -> Entry:
    """The entry of a regular file member whose content has this sha256."""
    return Entry("file", info.mode & 0o7777, info.uid, info.gid, info.size, sha256)


def symlink_bytes(target: str) -> bytes:
    """A symlink target as the bytes the kernel stores; `tarfile` decodes names with
    surrogateescape, so undecodable bytes round-trip."""
    return target.encode("utf-8", "surrogateescape")


@dataclass(frozen=True)
class Layer:
    """What one layer holds: entries by path and the paths its markers name.

    `whiteouts` are the paths `.wh.<name>` members delete; `opaque` are the directories with a
    `.wh..wh..opq` member. `nondirs` are the paths of every member that is not a directory or a
    marker, kept or not: such a member replaces whatever lower subtree its path had (a directory
    replaced by a file or symlink appears as that one member, with no whiteouts for its old
    children). `unresolved_links` are hard link targets that `read_layer` did not keep, so the
    links naming them have no entry; read again keeping them.
    """

    entries: Mapping[str, Entry] = field(default_factory=dict)
    whiteouts: frozenset[str] = frozenset()
    opaque: frozenset[str] = frozenset()
    nondirs: frozenset[str] = frozenset()
    unresolved_links: frozenset[str] = frozenset()

    def hidden(self, path: str) -> bool:
        """True if this layer hides the lower layers' entry for `path`.

        An entry present in this layer is never hidden. Otherwise `path` is hidden if it or an
        ancestor is whited out, or a strict ancestor is opaque or not a directory, in this
        layer; descendants of a whited-out path are hidden even if that path itself has an
        entry here.
        """
        if path in self.entries or not (self.whiteouts or self.opaque or self.nondirs):
            return False
        if path in self.whiteouts:
            return True
        return any(
            a in self.whiteouts or a in self.opaque or a in self.nondirs for a in ancestors(path)
        )

    def hidden_in(self, paths: Sequence[str]) -> set[str]:
        """The members of `paths`, which must be sorted, that `hidden` is true for."""
        found: set[str] = set()
        for root in self.whiteouts:
            found.update(_under(paths, root, inclusive=True))
        for root in self.opaque | self.nondirs:
            found.update(_under(paths, root, inclusive=False))
        return {path for path in found if path not in self.entries}


def _under(paths: Sequence[str], root: str, *, inclusive: bool) -> Iterator[str]:
    """The members of sorted `paths` under `root`, and `root` itself if `inclusive`."""
    if inclusive:
        i = bisect_left(paths, root)
        if i < len(paths) and paths[i] == root:
            yield root
    prefix = "/" if root == "/" else root + "/"
    i = bisect_left(paths, prefix)
    while i < len(paths) and paths[i].startswith(prefix):
        yield paths[i]
        i += 1


@dataclass(frozen=True)
class LayerNames:
    """A layer's entry paths and marker paths, without content; `nondirs` as in `Layer`."""

    paths: frozenset[str]
    whiteouts: frozenset[str]
    opaque: frozenset[str]
    nondirs: frozenset[str]


class FileHook(Protocol):
    def __call__(self, path: str, info: tarfile.TarInfo, data: BinaryIO) -> str:
        """Consume a regular file member's content and return its sha256 (hex)."""
        ...


def sha256_of(data: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := data.read(_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def hash_file(path: str, info: tarfile.TarInfo, data: BinaryIO) -> str:
    """The default `FileHook`: hash the content and keep nothing."""
    return sha256_of(data)


def read_layer(
    stream: BinaryIO,
    *,
    keep: Callable[[str], bool] | None = None,
    on_file: FileHook = hash_file,
) -> Layer:
    """The entries and markers of one layer tar, read once from `stream`.

    Only paths `keep` accepts become entries (all, without `keep`); markers are always read.
    `on_file` gets the content of every kept regular file. A hard link member names an earlier
    member and gets that member's entry; when that member was not kept, the link has no entry
    and its target is in `unresolved_links`. A path with two members takes the last one.
    """
    entries: dict[str, Entry] = {}
    skipped: set[str] = set()
    whiteouts: set[str] = set()
    opaque: set[str] = set()
    nondirs: set[str] = set()
    unresolved: set[str] = set()
    with _tar(stream) as tar:
        for info in tar:
            path = normalize(info.name)
            if path is None:
                continue
            marker = _marker(path)
            if marker is not None:
                kind, target = marker
                if kind == "whiteout":
                    whiteouts.add(target)
                elif kind == "opaque":
                    opaque.add(target)
                continue
            if info.isdir():
                nondirs.discard(path)
            else:
                nondirs.add(path)
            if keep is not None and not keep(path):
                skipped.add(path)
                continue
            if not info.islnk():
                entries[path] = _entry(path, info, tar, on_file)
                continue
            target = _hard_link_target(path, info)
            if target in entries:
                entries[path] = entries[target]
            elif target in skipped:
                entries.pop(path, None)
                unresolved.add(target)
            else:
                raise LayerError(
                    f"hard link {path} names {info.linkname!r}, which is no earlier member"
                )
    return Layer(
        entries, frozenset(whiteouts), frozenset(opaque), frozenset(nondirs), frozenset(unresolved)
    )


def scan_names(stream: BinaryIO) -> LayerNames:
    """The entry and marker paths of one layer tar, read once without hashing content."""
    paths: set[str] = set()
    whiteouts: set[str] = set()
    opaque: set[str] = set()
    nondirs: set[str] = set()
    with _tar(stream) as tar:
        for info in tar:
            path = normalize(info.name)
            if path is None:
                continue
            marker = _marker(path)
            if marker is None:
                paths.add(path)
                if info.isdir():
                    nondirs.discard(path)
                else:
                    nondirs.add(path)
            elif marker[0] == "whiteout":
                whiteouts.add(marker[1])
            elif marker[0] == "opaque":
                opaque.add(marker[1])
    return LayerNames(frozenset(paths), frozenset(whiteouts), frozenset(opaque), frozenset(nondirs))


def for_each_file(
    stream: BinaryIO, on_file: Callable[[str, tarfile.TarInfo, BinaryIO], None]
) -> None:
    """Call `on_file` with every regular file member of a layer tar that is not a marker."""
    with _tar(stream) as tar:
        for info in tar:
            path = normalize(info.name)
            if path is None or not info.isreg() or _marker(path) is not None:
                continue
            on_file(path, info, _content(tar, info))


@contextmanager
def _tar(stream: BinaryIO) -> Iterator[tarfile.TarFile]:
    """The layer as a stream-mode tar; read errors, including those raised while a hook reads
    a member's content, become `LayerError`."""
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as tar:
            yield tar
    except (tarfile.TarError, EOFError, zlib.error) as error:
        raise LayerError(f"cannot read layer: {error}") from error


def _marker(path: str) -> tuple[str, str] | None:
    """`("whiteout", deleted path)` or `("opaque", directory)` for a marker member, `("ignored",
    path)` for another AUFS marker; None for an ordinary entry."""
    parent, _, name = path.rpartition("/")
    if not name.startswith(WHITEOUT_PREFIX):
        return None
    if name == OPAQUE_MARKER:
        return ("opaque", parent or "/")
    deleted = name[len(WHITEOUT_PREFIX) :]
    if deleted.startswith(WHITEOUT_PREFIX) or deleted in ("", ".", ".."):
        logger.warning("ignoring layer marker %s, which is not a whiteout or opaque marker", path)
        return ("ignored", path)
    return ("whiteout", f"{parent}/{deleted}")


def _hard_link_target(path: str, info: tarfile.TarInfo) -> str:
    target = normalize(info.linkname)
    if target is None:
        raise LayerError(f"hard link {path} names the root")
    return target


def _content(tar: tarfile.TarFile, info: tarfile.TarInfo) -> BinaryIO:
    data = tar.extractfile(info)
    if data is None:
        raise LayerError(f"regular member {info.name!r} has no content")
    return data


def _entry(path: str, info: tarfile.TarInfo, tar: tarfile.TarFile, on_file: FileHook) -> Entry:
    mode = info.mode & 0o7777
    if info.isreg():
        return file_entry(info, on_file(path, info, _content(tar, info)))
    if info.isdir():
        return Entry("dir", mode, info.uid, info.gid)
    if info.issym():
        target = symlink_bytes(info.linkname)
        return Entry(
            "symlink",
            mode,
            info.uid,
            info.gid,
            size=len(target),
            sha256=hashlib.sha256(target).hexdigest(),
            link_target=info.linkname,
        )
    return Entry("other", mode, info.uid, info.gid)
