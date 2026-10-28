"""Deltas from `docker commit` checkpoints (ADR-0014).

A `docker commit` image of a trial container is the trial's *start image* (the image its
container started from: the derived image, ADR-0008, or for a resumed trial the checkpoint it
resumed from) plus one layer on top: the container's whole writable layer at that moment
(Docker adds no layer when the container has no change; that top layer is empty). So the top
layer of checkpoint k holds every change since the container started, and every checkpoint of a
trial shares the start image's layers. The environment's state at checkpoint k is the *view*:
the start image's filesystem with top layer k applied. The delta of checkpoint k is the
difference between view k-1 and view k; view 0 is the start image itself (an empty top layer).
In the code, `base` is short for the start image's filesystem.

Layer format (OCI image spec, "Representing Changes"; as Docker 29 with the containerd
snapshotter writes it, checked 2026-10-18 on this repo's development Mac):

- Member names have no leading `/` (`etc/motd`); `./` prefixes and trailing `/` on directories
  may occur. The root entry (`.` or `./`) is ignored.
- `.wh.<name>`, an empty regular file, deletes `<name>` from the lower layers, and its whole
  subtree if it is a directory. containerd writes one `.wh.` per removed child even for a
  directory that was removed and recreated.
- `.wh..wh..opq` in a directory hides every lower-layer entry under that directory; entries of
  the same layer under it stay. containerd did not write one, but other writers do.
- A member that is not a directory replaces the whole lower subtree at its path: a directory
  replaced by a file or symlink appears as that one member, with no whiteouts for its old
  children (checked 2026-10-18).
- A hard link member (LNKTYPE) names an earlier member of the same layer; it is a file with that
  member's content.
- A file copied up by chmod, touch, or a rewrite with the same bytes appears in the layer, and
  parent directories appear with their children. Presence in a layer is therefore not a
  change; only an entry that differs from the earlier view is.

View(k)[p] is top layer k's entry for p if it has one; else nothing if p is hidden in layer k (p
or an ancestor whited out, or a strict ancestor opaque or not a directory, in that layer); else
the start image's entry for p, which may be nothing. The start image's filesystem is its own
layers applied bottom to top by the same rules.

Two entries are equal when their kind (file, dir, symlink, other), permission bits
(mode & 0o7777), uid, and gid are equal, and for a file the sha256 of its content, for a symlink
the sha256 of its target (UTF-8), are equal. Times are never compared.

The paths whose view can differ between k-1 and k are exactly the entries of layer k-1 or layer
k, and the paths under a root that layer k-1 or layer k hides (a whiteout, an opaque directory,
or a non-directory entry); every other path is the start image's in both views.

Rows: one `DeltaRow` per path whose entries differ, directories included. Excluded paths
(`trajlab.store.exclusions`) never give a row, and neither does an added or deleted directory
whose only changes underneath are excluded ones (e.g. `/root/.cache` made for
`/root/.cache/claude`), as in the change detector (ADR-0010).

Text rule (`TEXT_RULE`): a file version is text if it is at most `MAX_TEXT_BYTES`, contains no
NUL byte, and decodes as UTF-8; a symlink target is always text. A version without content (an
absent path, a directory, or another kind) counts as text, so a row is text when each of its
file or symlink versions is, and `is_text` is null when it has neither (a directory or another
kind on both sides). A text row whose content changed gets a unified diff: headers `--- a<path>`
and `+++ b<path>` (`/dev/null` for a side without content), 3 lines of context, lines split on
"\\n" only (a "\\r" stays in its line), and "\\ No newline at end of file" after a last line
without one. Lines are matched after trimming common leading and trailing lines: exactly
(difflib without autojunk) when that is cheap, else by a shortest edit script (Myers) within a
step budget, else by anchoring on lines unique to both sides (patience diff) with the gaps
matched exactly within a budget, so a hunk is made in a few seconds at most; it is small except
for a file of few distinct lines edited in many scattered places, which gets one hunk replacing
the whole file. A path with a newline, tab, double quote, or backslash is quoted in the headers
the way GNU diff and git quote it (`+++ "b/app/x\\n.txt"`), with a byte that is not UTF-8
as an octal escape.
A row whose content changed but which gets no hunk (a version that is not text) stores its
later version instead, if it has one: a file's bytes or a symlink's target go to
`<blobs_dir>/<sha256>`, named `blobs/<sha256>` in `blob_ref`. A mode- or owner-only change
stores nothing. Earlier versions are never stored; their sha256 and size are.

A path holding bytes that are not UTF-8 is stored with each such byte written as `\\xNN`
(`backslashreplace`), as are such bytes in a symlink target shown in a hunk; sha256 and size
are always of the raw bytes.

Rows are ordered by (step_idx, path).
"""

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
from array import array
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import BinaryIO, NamedTuple, Protocol
from uuid import UUID

from trajlab.contracts import BLOBS_DIRNAME, CheckpointRecord, DeltaRow
from trajlab.store.exclusions import excluded as default_excluded
from trajlab.store.layers import (
    Entry,
    Layer,
    LayerError,
    ancestors,
    for_each_file,
    read_layer,
    scan_names,
    sha256_of,
    symlink_bytes,
)

logger = logging.getLogger(__name__)

MAX_TEXT_BYTES = 1 << 20
TEXT_RULE = (
    f"file at most {MAX_TEXT_BYTES} bytes, no NUL byte, valid UTF-8; symlink targets are text"
)
RECONCILE_METHOD = (
    "top-layer-view-v1: view k = start image + checkpoint k's top layer (OCI whiteouts, opaque "
    "directories, non-directory entries replacing subtrees); delta k = view k-1 vs view k by "
    "kind, mode, owner, content sha256; hunks for text: common ends trimmed, then exact difflib "
    "(line product <= 250000), Myers (<= 2000000 steps), or unique-line anchors; blobs: the "
    "later version of a changed row without a hunk; non-UTF-8 path bytes as \\xNN"
)


class LayerSource(Protocol):
    """One layer tar, gzip-compressed or not, readable from its start any number of times."""

    def open(self) -> BinaryIO:
        """A fresh binary stream positioned at the layer's first byte; the caller closes it."""
        ...


@dataclass(frozen=True)
class ImageChain:
    """The layers a trial's deltas are computed from.

    `start_layers` are the start image's layers, bottom to top. `tops` maps a checkpoint's
    `seq` to its image's top layer; a checkpoint whose image is gone has no entry.
    """

    start_layers: tuple[LayerSource, ...]
    tops: Mapping[int, LayerSource]


@dataclass(frozen=True)
class ReconcileResult:
    deltas: tuple[DeltaRow, ...]
    computed_seqs: tuple[int, ...]  # checkpoints whose delta was computed, ascending
    skipped_seqs: tuple[int, ...]  # checkpoints whose delta could not be: it or its predecessor
    # has no image


class ReconcileError(ValueError):
    """The layers or records cannot be reconciled: seqs with gaps or repeats, records of more
    than one trial, a checkpoint with no owning step or two sharing one, a top layer for a seq
    no record has, or a layer that cannot be read."""


def reconcile(
    chain: ImageChain,
    checkpoints: Sequence[CheckpointRecord],
    owner_step: Mapping[str, int],
    *,
    trial_id: UUID,
    blobs_dir: Path,
    work_dir: Path,
    excluded: Callable[[str], bool] = default_excluded,
) -> ReconcileResult:
    """The delta rows of every checkpoint of one trial.

    `checkpoints` are the trial's records in any order; their seqs must be 1..n without gaps.
    `owner_step` maps each record's `tool_call_id` (a stop record's request id included) to the
    `step_idx` of the step row that owns it; the checkpoint's rows carry that step_idx.
    `work_dir` is scratch space for text versions, which the caller removes afterwards.
    """
    step_of = _validate(chain, checkpoints, owner_step)
    if not step_of:
        return ReconcileResult(deltas=(), computed_seqs=(), skipped_seqs=())
    blobs_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    contents = _Contents(blobs_dir=blobs_dir, work_dir=work_dir, excluded=excluded)
    result = _Reconciler(chain, contents, excluded, trial_id).run(step_of)
    logger.info(
        "trial %s: %d delta rows from %d checkpoints; skipped %s",
        trial_id,
        len(result.deltas),
        len(step_of),
        list(result.skipped_seqs) or "none",
    )
    return result


# -- implementation ----------------------------------------------------------------------------
#
# Three passes over the layers, each layer read sequentially:
#
# 1. Names: every available top layer, for its entry and marker paths only. Their union is the
#    set of paths whose base entry can matter: an entry path of some top, or a path at or under
#    a root some top hides.
# 2. Base: the start image's layers bottom to top, keeping only those paths, applied by the
#    view rule (a layer is read again when a hard link it keeps names a member it did not).
# 3. Compare: each available top in seq order, hashing every file. Only layer k-1 and layer k
#    are held at a time; delta k compares their views over the docstring's candidate paths.
#
# Content: every text version of a non-excluded file is written to `work_dir/<sha256>` as it
# streams past (deduplicated by hash), so hunks can be made from any earlier version. A non-text
# later version of at most MAX_TEXT_BYTES is written to the blob dir as it streams past; anything
# a row needs that is still missing afterwards (a larger file, or a hard link to an unchanged
# member) is fetched by reading the layers again, matching by size and then hash.

_CONTENT_KINDS = frozenset({"file", "symlink"})
_CONTEXT_LINES = 3
# Hunks should show what changed and no more, in bounded time. difflib without autojunk is close
# to cubic when lines repeat (0.8 s for 1,000 lines of pretty-printed JSON, 21 s for 3,000), and
# with autojunk a one-line edit to such a file rewrites every line. So common leading and trailing
# lines are trimmed, and the rest is matched:
# 1. exactly, by difflib without autojunk, when the two sides' line counts multiply to at most
#    _EXACT_LINE_PRODUCT (its cost grows about as that product to the 1.5: 0.09 s at the cap on
#    the worst shape measured, blank lines against blank lines interleaved with distinct ones);
# 2. else by a shortest edit script (Myers) if one takes at most _EDIT_SCRIPT_BUDGET steps
#    (about 0.5 s; 500 scattered edits in 30,000 lines take 0.13 s);
# 3. else, for a wholesale rewrite, by anchoring on lines that occur exactly once on each side
#    (patience diff) and matching each gap between anchors exactly while the gaps' line products
#    add up to at most _GAP_BUDGET (about 2 s at worst), and as replacements after that.
# So a hunk takes under about 3 s and is always a correct diff. It is not always small: a file
# of few distinct lines (say, of 0s and 1s) edited in many scattered places has no unique lines
# to anchor on, and gets one hunk replacing the whole file.
_EXACT_LINE_PRODUCT = 60_000
_EDIT_SCRIPT_BUDGET = 2_000_000
_GAP_BUDGET = 1_200_000
_CHUNK = 1 << 20
_EMPTY_TOP = Layer()  # view 0 is the start image with an empty top layer


class _Change(NamedTuple):
    path: str
    before: Entry | None
    after: Entry | None


def _same(a: Entry | None, b: Entry | None) -> bool:
    if a is None or b is None:
        return a is b
    return a.state == b.state


def _file_content_changed(before: Entry | None, after_file_sha256: str) -> bool:
    """True unless `before` is a file with exactly this content."""
    return before is None or before.kind != "file" or before.sha256 != after_file_sha256


def _content_changed(before: Entry | None, after: Entry | None) -> bool:
    """True if the two versions' content differs: the sha256 of a file's bytes or a symlink's
    target, none for an absent path, a directory, or another kind. A file replaced by a symlink
    whose target is that file's bytes has the same content: its kinds tell the change."""
    return (before.sha256 if before else None) != (after.sha256 if after else None)


class _View:
    """View k: top layer k's entries over the base, by the docstring's rule."""

    __slots__ = ("base", "top")

    def __init__(self, top: Layer, base: Mapping[str, Entry]) -> None:
        self.top = top
        self.base = base

    def get(self, path: str) -> Entry | None:
        entry = self.top.entries.get(path)
        if entry is not None:
            return entry
        if self.top.hidden(path):
            return None
        return self.base.get(path)


def _validate(
    chain: ImageChain, checkpoints: Sequence[CheckpointRecord], owner_step: Mapping[str, int]
) -> dict[int, int]:
    """The owning step_idx of each checkpoint, by seq; raises `ReconcileError` on bad input."""
    seqs = sorted(record.seq for record in checkpoints)
    if seqs != list(range(1, len(seqs) + 1)):
        missing = sorted(set(range(1, (seqs[-1] if seqs else 0) + 1)) - set(seqs))
        repeated = sorted({seq for seq in seqs if seqs.count(seq) > 1})
        raise ReconcileError(
            f"checkpoint seqs must be 1..{len(seqs)} without gaps or repeats; "
            f"missing {missing[:10]}, repeated {repeated[:10]}"
        )
    trial_names = sorted({record.trial_name for record in checkpoints})
    if len(trial_names) > 1:
        raise ReconcileError(f"checkpoints of more than one trial: {trial_names}")
    step_of: dict[int, int] = {}
    owner_of: dict[int, int] = {}
    for record in sorted(checkpoints, key=lambda record: record.seq):
        step_idx = owner_step.get(record.tool_call_id)
        if step_idx is None:
            raise ReconcileError(
                f"checkpoint {record.seq} ({record.tool_call_id}) has no owning step"
            )
        if step_idx in owner_of:
            # Their rows would share (trial_id, step_idx, path), the deltas table's key.
            raise ReconcileError(
                f"checkpoints {owner_of[step_idx]} and {record.seq} are both owned by "
                f"step {step_idx}"
            )
        owner_of[step_idx] = record.seq
        step_of[record.seq] = step_idx
    stray = sorted(set(chain.tops) - step_of.keys())
    if stray:
        raise ReconcileError(f"top layers given for seqs {stray}, which no checkpoint has")
    return step_of


@contextmanager
def _reading(source: LayerSource, what: str) -> Iterator[BinaryIO]:
    try:
        with source.open() as stream:
            yield stream
    except LayerError as error:
        raise ReconcileError(f"{what}: {error}") from error


def _wanted(exact: frozenset[str], roots: frozenset[str], path: str) -> bool:
    if path in exact or path in roots:
        return True
    return bool(roots) and any(a in roots for a in ancestors(path))


def _wanted_or_extra(keep: Callable[[str], bool], extra: set[str], path: str) -> bool:
    return keep(path) or path in extra


def _has_prefix(sorted_paths: Sequence[str], prefix: str) -> bool:
    i = bisect_left(sorted_paths, prefix)
    return i < len(sorted_paths) and sorted_paths[i].startswith(prefix)


def _drop_excluded(changes: list[_Change], excluded: Callable[[str], bool]) -> list[_Change]:
    """Changes without excluded paths, and without an added or deleted directory whose only
    changes underneath are excluded ones (as `trajlab.checkpoint.changes` decides)."""
    kept = [change for change in changes if not excluded(change.path)]
    dropped = sorted(change.path for change in changes if excluded(change.path))
    if not dropped:
        return kept
    kept_paths = sorted(change.path for change in kept)

    def only_holds_excluded(change: _Change) -> bool:
        if change.before is not None and change.after is not None:
            return False
        prefix = change.path + "/"
        return not _has_prefix(kept_paths, prefix) and _has_prefix(dropped, prefix)

    return [change for change in kept if not only_holds_excluded(change)]


def _is_text(content: bytes) -> bool:
    if b"\0" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _put(directory: Path, sha256: str, content: bytes) -> None:
    """Write `directory/<sha256>` once, atomically."""
    target = directory / sha256
    if target.exists():
        return
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _spool(directory: Path, data: BinaryIO) -> tuple[str, Path]:
    """Copy a stream to a temporary file in `directory`; its sha256 and the file."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := data.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return digest.hexdigest(), Path(tmp)


class _Contents:
    """File contents by sha256: text versions in the work dir, non-text ones in the blob dir."""

    def __init__(self, *, blobs_dir: Path, work_dir: Path, excluded: Callable[[str], bool]):
        self.blobs_dir = blobs_dir
        self.work_dir = work_dir
        self.excluded = excluded
        self.is_text: dict[str, bool] = {}  # every file version hashed so far

    def take(
        self, path: str, info: tarfile.TarInfo, data: BinaryIO, *, previous: _View | None
    ) -> str:
        """The `FileHook`: hash a file version, note whether it is text, and keep its content
        if a row may need it. `previous` is the view this layer's delta compares against."""
        if info.size > MAX_TEXT_BYTES:
            sha256 = sha256_of(data)
            self.is_text[sha256] = False
            return sha256
        content = data.read()
        sha256 = hashlib.sha256(content).hexdigest()
        text = _is_text(content)
        self.is_text[sha256] = text
        if self.excluded(path):
            return sha256
        if text:
            _put(self.work_dir, sha256, content)
        elif previous is not None and _file_content_changed(previous.get(path), sha256):
            _put(self.blobs_dir, sha256, content)
        return sha256

    def entry_is_text(self, entry: Entry) -> bool:
        return entry.kind == "symlink" or self.is_text[entry.sha256 or ""]

    def store_blob(self, entry: Entry) -> None:
        """Put a later version in the blob dir: a file's bytes (which `ensure` made present) or
        a symlink's target."""
        sha256 = entry.sha256 or ""
        if entry.kind == "symlink":
            _put(self.blobs_dir, sha256, symlink_bytes(entry.link_target or ""))
        elif self.is_text[sha256]:
            _put(self.blobs_dir, sha256, (self.work_dir / sha256).read_bytes())

    def location(self, sha256: str) -> Path:
        return (self.work_dir if self.is_text[sha256] else self.blobs_dir) / sha256

    def text(self, entry: Entry | None) -> str | None:
        """The text of a version for a hunk; None for a version without content."""
        if entry is None or entry.kind not in _CONTENT_KINDS:
            return None
        if entry.kind == "symlink":
            return symlink_bytes(entry.link_target or "").decode("utf-8", "backslashreplace")
        return (self.work_dir / (entry.sha256 or "")).read_bytes().decode("utf-8")

    def ensure(self, needed: Mapping[str, int], sources: Sequence[tuple[LayerSource, str]]) -> None:
        """Make every content in `needed` (sha256 -> size) present, reading `sources` in order
        until each is found; raises `ReconcileError` if one is in none of them."""
        missing = {sha: size for sha, size in needed.items() if not self.location(sha).exists()}
        for source, what in sources:
            if not missing:
                break
            logger.debug("fetching %d contents from %s", len(missing), what)
            with _reading(source, what) as stream:
                for_each_file(stream, partial(self._fetch, missing))
        if missing:
            raise ReconcileError(f"contents {sorted(missing)} are in no layer")

    def _fetch(
        self, missing: dict[str, int], path: str, info: tarfile.TarInfo, data: BinaryIO
    ) -> None:
        candidates = [sha for sha, size in missing.items() if size == info.size]
        if not candidates:
            return
        # Blobs are spooled in their own directory so the final rename is atomic.
        blob = any(not self.is_text[sha] for sha in candidates)
        sha256, tmp = _spool(self.blobs_dir if blob else self.work_dir, data)
        if sha256 not in missing or self.location(sha256).exists():
            tmp.unlink()
        else:
            shutil.move(tmp, self.location(sha256))
        missing.pop(sha256, None)


class _Reconciler:
    def __init__(
        self,
        chain: ImageChain,
        contents: _Contents,
        excluded: Callable[[str], bool],
        trial_id: UUID,
    ) -> None:
        self.chain = chain
        self.contents = contents
        self.excluded = excluded
        self.trial_id = trial_id
        self.base: dict[str, Entry] = {}
        self.base_paths: list[str] = []

    def run(self, step_of: Mapping[int, int]) -> ReconcileResult:
        tops = self.chain.tops
        if tops:
            self.base = self._read_base(self._interest())
            self.base_paths = sorted(self.base)
        rows: list[DeltaRow] = []
        computed: list[int] = []
        skipped: list[int] = []
        prev: Layer | None = _EMPTY_TOP
        for seq in range(1, len(step_of) + 1):
            if seq not in tops:
                skipped.append(seq)
                prev = None
                continue
            if prev is None:
                # No view k-1 to compare with; read layer k only as the next one's predecessor.
                skipped.append(seq)
                prev = self._read_top(seq, previous=None) if seq + 1 in tops else None
                continue
            layer = self._read_top(seq, previous=_View(prev, self.base))
            rows.extend(self._delta(seq, step_of[seq], prev, layer))
            computed.append(seq)
            prev = layer
        rows.sort(key=lambda row: (row.step_idx, row.path))
        return ReconcileResult(
            deltas=tuple(rows), computed_seqs=tuple(computed), skipped_seqs=tuple(skipped)
        )

    def _interest(self) -> Callable[[str], bool]:
        """Which base paths can matter: names pass over every available top."""
        exact: set[str] = set()
        roots: set[str] = set()
        for seq in sorted(self.chain.tops):
            with _reading(self.chain.tops[seq], f"top layer of checkpoint {seq}") as stream:
                names = scan_names(stream)
            exact |= names.paths
            # A non-directory entry replaces the base subtree at its path, so that subtree's
            # paths can be deleted rows.
            roots |= names.whiteouts | names.opaque | names.nondirs
        return partial(_wanted, frozenset(exact), frozenset(roots))

    def _read_base(self, keep: Callable[[str], bool]) -> dict[str, Entry]:
        view: dict[str, Entry] = {}
        for index, source in enumerate(self.chain.start_layers):
            layer = self._read_base_layer(index, source, keep)
            if layer.whiteouts or layer.opaque or layer.nondirs:
                view = {path: entry for path, entry in view.items() if not layer.hidden(path)}
            view.update(layer.entries)
        return view

    def _read_base_layer(
        self, index: int, source: LayerSource, keep: Callable[[str], bool]
    ) -> Layer:
        hook = partial(self.contents.take, previous=None)
        extra: set[str] = set()  # hard link targets outside `keep`, kept on a second read
        keep_more = partial(_wanted_or_extra, keep, extra)
        while True:
            with _reading(source, f"base layer {index}") as stream:
                layer = read_layer(stream, keep=keep_more, on_file=hook)
            if layer.unresolved_links <= extra:
                return layer
            extra |= layer.unresolved_links  # in place: keep_more sees it

    def _read_top(self, seq: int, *, previous: _View | None) -> Layer:
        hook = partial(self.contents.take, previous=previous)
        with _reading(self.chain.tops[seq], f"top layer of checkpoint {seq}") as stream:
            return read_layer(stream, on_file=hook)

    def _sources(self, seq: int) -> list[tuple[LayerSource, str]]:
        """Where delta `seq`'s contents can be: its top, the one before, then the base."""
        tops = self.chain.tops
        sources = [(tops[seq], f"top layer of checkpoint {seq}")]
        if seq - 1 in tops:
            sources.append((tops[seq - 1], f"top layer of checkpoint {seq - 1}"))
        base = self.chain.start_layers
        sources.extend((base[i], f"base layer {i}") for i in reversed(range(len(base))))
        return sources

    def _delta(self, seq: int, step_idx: int, prev: Layer, layer: Layer) -> list[DeltaRow]:
        before, after = _View(prev, self.base), _View(layer, self.base)
        candidates = (
            prev.entries.keys()
            | layer.entries.keys()
            | prev.hidden_in(self.base_paths)
            | layer.hidden_in(self.base_paths)
        )
        changes = []
        for path in sorted(candidates):
            change = _Change(path, before.get(path), after.get(path))
            if not _same(change.before, change.after):
                changes.append(change)
        shaped = [
            (change, self._shape(change)) for change in _drop_excluded(changes, self.excluded)
        ]
        needed: dict[str, int] = {}
        for change, (_, hunk, blob) in shaped:
            versions = [change.before, change.after] if hunk else []
            versions += [change.after] if blob else []
            for entry in versions:
                if entry is not None and entry.kind == "file":
                    needed[entry.sha256 or ""] = entry.size or 0
        if needed:
            self.contents.ensure(needed, self._sources(seq))
        for change, (_, _, blob) in shaped:
            if blob and change.after is not None:
                self.contents.store_blob(change.after)
        return [self._row(step_idx, change, shape) for change, shape in shaped]

    def _shape(self, change: _Change) -> tuple[bool | None, bool, bool]:
        """(is_text, has a hunk, has a blob) of a change's row.

        A version without content (absent, a directory, or another kind) counts as text: a row
        is text when every file or symlink version in it is, and null when it has none.
        """
        versions = [
            entry
            for entry in (change.before, change.after)
            if entry is not None and entry.kind in _CONTENT_KINDS
        ]
        if not versions:
            return None, False, False
        is_text = all(self.contents.entry_is_text(entry) for entry in versions)
        changed = _content_changed(change.before, change.after)
        hunk = is_text and changed
        # Without a hunk, the later version is the only record of what the step wrote.
        after = change.after
        blob = not is_text and changed and after is not None and after.kind in _CONTENT_KINDS
        return is_text, hunk, blob

    def _row(
        self, step_idx: int, change: _Change, shape: tuple[bool | None, bool, bool]
    ) -> DeltaRow:
        is_text, hunk, blob = shape
        b, a = change.before, change.after
        return DeltaRow(
            trial_id=self.trial_id,
            step_idx=step_idx,
            path=_printable(change.path),
            op="add" if b is None else "delete" if a is None else "modify",
            kind_before=b.kind if b else None,
            kind_after=a.kind if a else None,
            is_text=is_text,
            hunk=(
                _unified_diff(change.path, self.contents.text(b), self.contents.text(a))
                if hunk
                else None
            ),
            blob_ref=f"{BLOBS_DIRNAME}/{a.sha256}" if blob and a else None,
            sha256_before=b.sha256 if b else None,
            sha256_after=a.sha256 if a else None,
            bytes_before=b.size if b else None,
            bytes_after=a.size if a else None,
            mode_before=f"{b.mode:04o}" if b else None,
            mode_after=f"{a.mode:04o}" if a else None,
            owner_before=f"{b.uid}:{b.gid}" if b else None,
            owner_after=f"{a.uid}:{a.gid}" if a else None,
            mtime_before=_instant(b.mtime) if b else None,
            mtime_after=_instant(a.mtime) if a else None,
        )


def _instant(mtime: float | None) -> datetime | None:
    """A tar entry's mtime as an aware UTC instant; None when the entry had none."""
    return None if mtime is None else datetime.fromtimestamp(mtime, tz=UTC)


def _printable(text: str) -> str:
    """`text` with the bytes `tarfile` could not decode (surrogate escapes) as `\\xNN`."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _lines(text: str | None) -> list[str]:
    """Lines split on "\\n" only, each keeping its "\\n"; a last line may lack one."""
    if not text:
        return []
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


_Opcode = tuple[str, int, int, int, int]


def _opcodes(old: list[str], new: list[str]) -> list[_Opcode]:
    """difflib opcodes turning `old` into `new`, matched after trimming common ends."""
    shortest = min(len(old), len(new))
    head = 0
    while head < shortest and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < shortest - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    middle_old, middle_new = old[head : len(old) - tail], new[head : len(new) - tail]
    codes: list[_Opcode] = [("equal", 0, head, 0, head)] if head else []
    for tag, i1, i2, j1, j2 in _match(middle_old, middle_new):
        codes.append((tag, i1 + head, i2 + head, j1 + head, j2 + head))
    if tail:
        codes.append(("equal", len(old) - tail, len(old), len(new) - tail, len(new)))
    return codes


def _match(old: list[str], new: list[str]) -> list[_Opcode]:
    if len(old) * len(new) <= _EXACT_LINE_PRODUCT:
        return SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    script = _edit_script(old, new, _EDIT_SCRIPT_BUDGET)
    if script is not None:
        return script
    logger.debug("%d and %d lines differ widely; matching on unique lines", len(old), len(new))
    return _anchored(old, new)


def _anchored(old: list[str], new: list[str]) -> list[_Opcode]:
    """Opcodes anchored on lines that occur exactly once in `old` and once in `new`, kept in
    their longest common order (patience diff); the gaps between anchors are matched exactly
    while `_GAP_BUDGET` lasts and are replacements after that."""
    codes: list[_Opcode] = []
    budget = _GAP_BUDGET
    i = j = 0
    for ai, aj in [*_unique_anchors(old, new), (len(old), len(new))]:
        a, b = old[i:ai], new[j:aj]
        if a and b and len(a) * len(b) <= min(budget, _EXACT_LINE_PRODUCT):
            budget -= len(a) * len(b)
            gap = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            codes.extend((t, x1 + i, x2 + i, y1 + j, y2 + j) for t, x1, x2, y1, y2 in gap)
        elif a or b:
            tag = "replace" if a and b else "delete" if a else "insert"
            codes.append((tag, i, ai, j, aj))
        if ai < len(old):
            codes.append(("equal", ai, ai + 1, aj, aj + 1))
        i, j = ai + 1, aj + 1
    return _merged(codes)


def _unique_anchors(old: list[str], new: list[str]) -> list[tuple[int, int]]:
    """(i, j) pairs of lines unique on both sides, a longest run increasing in both."""
    in_old, in_new = Counter(old), Counter(new)
    where_new = {line: j for j, line in enumerate(new) if in_new[line] == 1}
    pairs = [
        (i, where_new[line])
        for i, line in enumerate(old)
        if in_old[line] == 1 and line in where_new
    ]
    # Longest increasing subsequence of j over pairs (already in i order), by patience sorting.
    tails: list[int] = []  # tails[k]: index into pairs of the smallest tail of a run of k + 1
    tail_js: list[int] = []  # the j of each of those tails, increasing
    previous: list[int] = [-1] * len(pairs)
    for index, (_, j) in enumerate(pairs):
        k = bisect_left(tail_js, j)
        if k:
            previous[index] = tails[k - 1]
        if k == len(tails):
            tails.append(index)
            tail_js.append(j)
        else:
            tails[k] = index
            tail_js[k] = j
    run: list[tuple[int, int]] = []
    index = tails[-1] if tails else -1
    while index >= 0:
        run.append(pairs[index])
        index = previous[index]
    return run[::-1]


def _merged(codes: list[_Opcode]) -> list[_Opcode]:
    """Adjacent opcodes coalesced: equal runs into one, edit runs into one replace (or a
    delete or insert), as difflib's opcodes alternate."""
    out: list[_Opcode] = []
    for tag, i1, i2, j1, j2 in codes:
        if i1 == i2 and j1 == j2:
            continue
        if out and (out[-1][0] == "equal") == (tag == "equal"):
            _, pi1, _, pj1, _ = out[-1]
            if tag != "equal":
                tag = "replace" if i2 > pi1 and j2 > pj1 else "delete" if i2 > pi1 else "insert"
            out[-1] = (tag, pi1, i2, pj1, j2)
        else:
            out.append((tag, i1, i2, j1, j2))
    return out


def _edit_script(old: list[str], new: list[str], budget: int) -> list[_Opcode] | None:
    """Opcodes of a shortest edit script from `old` to `new` (Myers, "An O(ND) Difference
    Algorithm and Its Variations", 1986), or None if finding it takes more than `budget` steps.

    `furthest[k]` is the furthest x reached on diagonal k = x - y; `trace[d]` keeps diagonals
    -d-1..d+1 of it as they were before round d, for walking the script back.
    """
    n, m = len(old), len(new)
    offset = n + m + 1
    furthest = array("q", bytes(8 * (2 * offset + 1)))
    trace: list[array[int]] = []
    steps = 0
    for d in range(n + m + 1):
        trace.append(furthest[offset - d - 1 : offset + d + 2])
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and furthest[offset + k - 1] < furthest[offset + k + 1]):
                x = furthest[offset + k + 1]  # down: insert new[y - 1]
            else:
                x = furthest[offset + k - 1] + 1  # right: delete old[x - 1]
            y = x - k
            start = x
            while x < n and y < m and old[x] == new[y]:
                x += 1
                y += 1
            steps += x - start + 1
            furthest[offset + k] = x
            if x >= n and y >= m:
                return _script_opcodes(trace, n, m)
        if steps > budget:
            return None
    raise AssertionError("an edit script of at most n + m edits always exists")


def _script_opcodes(trace: list[array[int]], n: int, m: int) -> list[_Opcode]:
    """Walk an edit script back from (n, m) and merge its moves into difflib opcodes."""
    moves: list[str] = []  # "=", "-" (delete), "+" (insert), last first
    x, y = n, m
    for d in range(len(trace) - 1, -1, -1):
        window = trace[d]  # window[k + d + 1] is diagonal k
        k = x - y
        if k == -d or (k != d and window[k + d] < window[k + d + 2]):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = window[prev_k + d + 1]
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            moves.append("=")
            x, y = x - 1, y - 1
        if d > 0:
            moves.append("+" if x == prev_x else "-")
        x, y = prev_x, prev_y
    codes: list[_Opcode] = []
    i = j = 0
    change: tuple[int, int] | None = None  # where the current run of edits started
    for move in [*reversed(moves), "end"]:
        if move in ("=", "end") and change is not None:
            ci, cj = change
            tag = "replace" if i > ci and j > cj else "delete" if i > ci else "insert"
            codes.append((tag, ci, i, cj, j))
            change = None
        if move == "=":
            if codes and codes[-1][0] == "equal":
                codes[-1] = ("equal", codes[-1][1], i + 1, codes[-1][3], j + 1)
            else:
                codes.append(("equal", i, i + 1, j, j + 1))
            i, j = i + 1, j + 1
        elif move in ("-", "+"):
            change = change or (i, j)
            i, j = (i + 1, j) if move == "-" else (i, j + 1)
    return codes


def _groups(codes: list[_Opcode], context: int) -> Iterator[list[_Opcode]]:
    """Opcodes grouped into hunks with `context` lines around each change, as
    `difflib.SequenceMatcher.get_grouped_opcodes` groups them."""
    if not codes:
        return
    codes = list(codes)
    tag, i1, i2, j1, j2 = codes[0]
    if tag == "equal":
        codes[0] = (tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2)
    tag, i1, i2, j1, j2 = codes[-1]
    if tag == "equal":
        codes[-1] = (tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context))
    group: list[_Opcode] = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > 2 * context:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            yield group
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        yield group


def _range(start: int, stop: int) -> str:
    """A hunk header range, as GNU diff and difflib write it."""
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f"{start if length == 0 else start + 1},{length}"


_HEADER_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t"}


def _header_name(name: str) -> str:
    """A diff header's file name, C-quoted as git and GNU diff quote it when it holds a
    newline, tab, double quote, backslash, or a byte that is not UTF-8 (a surrogate escape from
    `tarfile`, written as an octal escape), so the header stays one line and names the file's
    real bytes."""
    quoted = False
    out: list[str] = []
    for char in name:
        if char in _HEADER_ESCAPES:
            out.append(_HEADER_ESCAPES[char])
            quoted = True
        elif "\udc80" <= char <= "\udcff":
            out.append(f"\\{ord(char) - 0xDC00:03o}")
            quoted = True
        else:
            out.append(char)
    return f'"{"".join(out)}"' if quoted else name


def _unified_diff(path: str, before: str | None, after: str | None) -> str:
    """The unified diff from `before` to `after` (None: the side does not exist); "" when no
    line differs."""
    old, new = _lines(before), _lines(after)
    out: list[str] = []

    def emit(prefix: str, lines: list[str]) -> None:
        for line in lines:
            out.append(prefix + line)
            if not line.endswith("\n"):
                out.append("\n\\ No newline at end of file\n")

    for group in _groups(_opcodes(old, new), _CONTEXT_LINES):
        if not out:
            out.append(f"--- {'/dev/null' if before is None else _header_name('a' + path)}\n")
            out.append(f"+++ {'/dev/null' if after is None else _header_name('b' + path)}\n")
        first, last = group[0], group[-1]
        out.append(f"@@ -{_range(first[1], last[2])} +{_range(first[3], last[4])} @@\n")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                emit(" ", old[i1:i2])
            else:
                emit("-", old[i1:i2])
                emit("+", new[j1:j2])
    return "".join(out)
