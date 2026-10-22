"""Harness paths that never give a delta row (ADR-0014, from ADR-0010's list).

These are the paths Harbor and Claude Code write in the trial container, found from the top
layers of real checkpoints for the change detector (ADR-0010, item 5). The store excludes the
same set, so a call the detector judged unchanged has no delta rows, and a delta row is always
the agent's (or its processes') work. `/tmp` as a whole is not excluded: agents keep their own
scripts and outputs there. A test keeps this list a superset of the detector's.

The list is recorded in each store's `store_meta`; changing it changes what a store holds and
needs an ADR.
"""

import re

_HOME = r"/(?:root|home/[^/]+)"

# Each pattern matches a path and everything under it.
EXCLUDED_PATTERNS: tuple[str, ...] = (
    r"/logs",  # Harbor's bind mounts for agent, verifier, and artifact logs
    r"/tmp/claude-\d+",  # Claude Code's scratch and background-task output, per uid
    r"/tmp/claude-code-settings",  # Harbor's upload of the --settings file
    rf"{_HOME}/\.claude",  # Claude Code's config dir, when not redirected
    rf"{_HOME}/\.cache/claude",
    rf"{_HOME}/\.local/state/claude",  # Claude Code's version locks
    rf"{_HOME}/\.claude\.json",  # Claude Code's state file, when not redirected
    r"/proc",  # kernel filesystems: never in an image, listed for completeness
    r"/sys",
)

EXCLUDED = re.compile("|".join(rf"^{pattern}(?:/|$)" for pattern in EXCLUDED_PATTERNS))


def excluded(path: str) -> bool:
    """True if `path` (absolute, no trailing slash) is a harness path or lies under one."""
    return EXCLUDED.match(path) is not None
