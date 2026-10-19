"""What each hooked call changed, as content: diffs and hashes (ADR-0015 §2).

The change gate (`changes.py`) says which paths a call changed. For regular files among them,
the watcher reads the new versions from the running container and diffs them against the
versions it holds from earlier calls; a version it does not hold comes from the last
checkpoint's image, or, before any checkpoint, the trial's start image. Directories are not
recorded; removed paths and symlinks are recorded without content.

Everything here but `read_tar` is pure. Limits keep it inside the hook's wait: at most
`MAX_FILES_PER_CALL` files read per call, preferring files outside package and cache dirs; no
diff when the two sides' line counts multiply past `MAX_LINE_PRODUCT` (difflib's cost grows
with that product; ADR-0014 measured minutes on large repetitive files); and the watcher stops
reading before-versions once `BUDGET_S` has passed. Beyond a limit a file keeps its path, size,
and whatever hash is known. This is our own diff, not ADR-0014's; it may be replaced by that
one once it is shared outside `store/`.
"""

import difflib
import hashlib
import io
import re
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from trajlab.checkpoint.changes import Listing
from trajlab.contracts import MAX_TEXT_BYTES, FileChange, SkipReason

MAX_FILES_PER_CALL = 20
MAX_LINE_PRODUCT = 1_000_000
BUDGET_S = 2.0
CONTEXT_LINES = 3

# Read last: what package managers and builds write, rarely what the agent means to change.
NOISY = re.compile(
    r"/(?:site-packages|dist-packages|node_modules|__pycache__|\.cache|\.git|\.npm|\.cargo"
    r"|target|build|dist|\.venv|venv)/"
    r"|^/(?:usr|var|opt|etc|lib|lib64|bin|sbin)/"
)

_OPS = {"+": "added", "~": "modified", "-": "removed"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_text(data: bytes | None) -> bool:
    """ADR-0014's text rule: at most 1 MiB, no NUL byte, valid UTF-8. No content is text."""
    if data is None:
        return True
    if len(data) > MAX_TEXT_BYTES or b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def unified_diff(path: str, before: bytes | None, after: bytes | None) -> str | None:
    """A unified diff of two text versions, or None past the line-product limit."""
    old = before.decode("utf-8").splitlines() if before is not None else []
    new = after.decode("utf-8").splitlines() if after is not None else []
    if len(old) * len(new) > MAX_LINE_PRODUCT:
        return None
    lines = difflib.unified_diff(
        old,
        new,
        fromfile=f"a{path}" if before is not None else "/dev/null",
        tofile=f"b{path}" if after is not None else "/dev/null",
        n=CONTEXT_LINES,
        lineterm="",
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class Candidate:
    """A changed file or symlink the call touched, as the listing describes it."""

    path: str
    op: str  # added | modified | removed
    kind: str  # file | symlink | other
    size: int | None


def candidates(own: Sequence[str], after: Listing) -> list[Candidate]:
    """The non-directory paths among a call's `+`/`~`/`-` changes, in listing order."""
    found = []
    for change in own:
        sign, path = change[0], change[1:]
        op = _OPS.get(sign)
        if op is None:
            continue
        if op == "removed":
            found.append(Candidate(path, op, "other", None))
            continue
        entry = after.get(path)
        if entry is None or entry[0] == "d":
            continue
        kind = {"f": "file", "l": "symlink"}.get(entry[0], "other")
        size = int(entry[4]) if entry[4].isdigit() else None
        found.append(Candidate(path, op, kind, size))
    return found


def to_read(found: Sequence[Candidate]) -> list[str]:
    """Regular files small enough to read, quiet paths first, at most MAX_FILES_PER_CALL."""
    readable = [
        c
        for c in found
        if c.kind == "file" and c.op != "removed" and (c.size or 0) <= MAX_TEXT_BYTES
    ]
    readable.sort(key=lambda c: (NOISY.search(c.path) is not None, c.path))
    return [c.path for c in readable[:MAX_FILES_PER_CALL]]


def file_change(
    candidate: Candidate,
    *,
    after: bytes | None,
    before: bytes | None,
    before_known: bool,
    skipped: SkipReason | None = None,
) -> FileChange:
    """The record of one changed path from what was read; `before_known` says whether `before`
    is the real prior version (None then means the path did not exist) or simply unknown."""
    common = {"path": candidate.path, "op": candidate.op, "kind": candidate.kind}
    if candidate.op == "removed" or candidate.kind != "file":
        return FileChange(
            **common,  # type: ignore[arg-type]
            size=candidate.size,
            before_sha256=sha256(before) if before is not None else None,
        )
    if after is None:
        reason = skipped or ("too_large" if (candidate.size or 0) > MAX_TEXT_BYTES else None)
        return FileChange(**common, size=candidate.size, skipped=reason or "unreadable")  # type: ignore[arg-type]
    before_hash = sha256(before) if before is not None else None
    text = is_text(after) and is_text(before)
    diff = None
    reason: SkipReason | None = skipped
    if not text:
        reason = "not_text"
    elif not before_known:
        reason = reason or "over_budget"
    else:
        diff = unified_diff(candidate.path, before, after)
        if diff is None:
            reason = "too_many_lines"
    return FileChange(
        **common,  # type: ignore[arg-type]
        size=len(after),
        sha256=sha256(after),
        before_sha256=before_hash,
        is_text=text,
        diff=diff,
        skipped=reason,
    )


def read_tar(data: bytes) -> dict[str, bytes]:
    """`/path` -> content for every regular file in a tar stream; partial streams are fine.

    `tar -cf - -C / app/x` writes `app/x`; a member named `./app/x` is normalised too.
    """
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                if not member.isreg():
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                name = "/" + member.name.removeprefix("./").lstrip("/")
                files[name] = stream.read()
    except (tarfile.TarError, EOFError):
        pass  # a truncated stream still yields the members read so far
    return files


def tar_members(paths: Sequence[str]) -> list[str]:
    """Absolute paths as tar arguments relative to `/`, for `tar -cf - -C / <members>`."""
    return [path.lstrip("/") for path in paths]


def prior_versions(
    paths: Sequence[str], held: Mapping[str, bytes | None]
) -> tuple[dict[str, bytes | None], list[str]]:
    """The before-versions held for `paths`, and the paths whose before-version is unknown."""
    known = {path: held[path] for path in paths if path in held}
    return known, [path for path in paths if path not in held]
