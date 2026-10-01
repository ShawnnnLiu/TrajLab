"""Did a tool call change the container's filesystem? (ADR-0010)

The backend lists the container's root filesystem with GNU `find`; this module parses listings
and compares them. It is pure: no Docker, no I/O.

Each path's entry is (type, mode, uid, gid, size, change time, link target). Change time, not
modification time: ordinary tools cannot set it, so `touch -d` or `cp -p` cannot hide a write.
Directories compare by type, mode, and owner only: overlayfs copy-up and changes to children
move a directory's change time, and the children carry the real change.
"""

import re
from collections.abc import Iterable

# The find invocation the backend runs; fields are NUL-separated so any file name is safe.
FIND_FIELDS = ("%y", "%m", "%U", "%G", "%s", "%C@", "%l", "%p")
FIND_PRINTF = "\\0".join(FIND_FIELDS) + "\\0"

Entry = tuple[str, str, str, str, str, str, str]
Listing = dict[str, Entry]

# Paths the harness writes, not the agent (ADR-0010, from the top layers of real checkpoints).
# They never make a call "changed"; checkpoints still capture them.
_HOME = r"/(?:root|home/[^/]+)"
EXCLUDED = re.compile(
    "|".join(
        rf"^{pattern}(?:/|$)"
        for pattern in (
            r"/logs",  # Harbor's bind mounts for agent, verifier, and artifact logs
            r"/tmp/claude-\d+",  # Claude Code's scratch and background-task output, per uid
            r"/tmp/claude-code-settings",  # Harbor's upload of the --settings file
            rf"{_HOME}/\.claude",  # Claude Code's config dir, when not redirected
            rf"{_HOME}/\.cache/claude",
            rf"{_HOME}/\.local/state/claude",  # Claude Code's version locks
        )
    )
    + rf"|^{_HOME}/\.claude\.json$"
)


class ListingError(ValueError):
    """The find output is not a whole number of entries."""


def parse_listing(raw: str) -> Listing:
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    width = len(FIND_FIELDS)
    if len(fields) % width:
        raise ListingError(f"{len(fields)} fields is not a multiple of {width}")
    listing: Listing = {}
    for i in range(0, len(fields), width):
        kind, mode, uid, gid, size, ctime, link, path = fields[i : i + width]
        listing[path] = (kind, mode, uid, gid, size, ctime, link)
    return listing


def _comparable(entry: Entry) -> Entry | tuple[str, str, str, str]:
    kind, mode, uid, gid = entry[:4]
    return (kind, mode, uid, gid) if kind == "d" else entry


def excluded(path: str) -> bool:
    return EXCLUDED.match(path) is not None


def changed_paths(before: Listing, after: Listing) -> list[str]:
    """`+added`, `-removed`, `~modified` paths, sorted, ignoring harness paths.

    A directory added or removed only to hold excluded paths (e.g. `/root/.cache` for
    `/root/.cache/claude`) is ignored too.
    """
    added = after.keys() - before.keys()
    removed = before.keys() - after.keys()
    modified = {
        path
        for path in after.keys() & before.keys()
        if _comparable(after[path]) != _comparable(before[path])
    }
    changes = {f"+{p}" for p in added} | {f"-{p}" for p in removed} | {f"~{p}" for p in modified}
    kept = {c for c in changes if not excluded(c[1:])}
    excluded_paths = [c[1:] for c in changes - kept]
    return sorted(c for c in kept if not _only_holds_excluded(c, kept, excluded_paths))


def _only_holds_excluded(change: str, kept: set[str], excluded_paths: Iterable[str]) -> bool:
    if change[0] == "~":
        return False
    path = change[1:]
    prefix = path.rstrip("/") + "/"
    if any(other[1:].startswith(prefix) for other in kept):
        return False
    return any(p.startswith(prefix) for p in excluded_paths)
