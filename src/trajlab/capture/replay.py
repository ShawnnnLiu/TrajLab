"""What a file held after a given call, for the trace site's replay view (ADR-0015).

The site shows a changed file as it was after any call, from one of two sources:

1. The recorded diffs (`trajlab watch --extras`). A file the agent created starts from its
   first diff, which is against nothing, and each later diff is applied on top. A rebuilt
   version counts only if its sha256 matches the recorded one; the diffs do not record whether
   the file ends with a newline, so both endings are tried.
2. The checkpoint image that holds the state after the call, read with `docker run --rm
   --pull never --network none`. This covers files that existed before the trial and files
   whose diffs were skipped, but only where the images still exist: the machine that ran the
   trial, until they are pruned.
"""

import hashlib
import re
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

MAX_FILE_BYTES = 2 * 1024 * 1024
SNAPSHOT_TIMEOUT_S = 60
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def apply_diff(base: Sequence[str], diff: str) -> list[str] | None:
    """`base`, as lines without line ends, with the hunks of a difflib unified diff applied;
    None when a hunk does not fit `base`."""
    lines = diff.split("\n")
    out: list[str] = []
    position = 0
    i = 0
    while i < len(lines) and not lines[i].startswith("@@"):
        i += 1  # the ---/+++ header
    while i < len(lines):
        match = _HUNK.match(lines[i])
        if match is None:
            return None
        beginning, length = int(match.group(1)), int(match.group(2) or 1)
        # difflib numbers lines from 1, and starts an empty range at the line before it.
        start = beginning - 1 if length else beginning
        if start < position or start > len(base):
            return None
        out.extend(base[position:start])
        position = start
        i += 1
        while i < len(lines) and not lines[i].startswith("@@"):
            line = lines[i]
            i += 1
            tag, text = line[:1], line[1:]
            if tag in (" ", "-"):
                if position >= len(base) or base[position] != text:
                    return None
                if tag == " ":
                    out.append(text)
                position += 1
            elif tag == "+":
                out.append(text)
            elif tag != "\\" and line:  # "\ No newline at end of file"; a trailing empty line
                return None
    out.extend(base[position:])
    return out


def added_lines(diff: str) -> list[int]:
    """The line numbers, from 1, that a unified diff adds or changes in the new file."""
    numbers: list[int] = []
    current = 0
    for line in diff.split("\n"):
        match = _HUNK.match(line)
        if match is not None:
            beginning, length = int(match.group(3)), int(match.group(4) or 1)
            current = beginning if length else beginning + 1
            continue
        if not current or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            numbers.append(current)
            current += 1
        elif line.startswith(" "):
            current += 1
    return numbers


def rebuild(
    versions: Sequence[Mapping[str, Any]], diffs: Mapping[str, str]
) -> tuple[bool | None, str | None, bool]:
    """A path after the last of `versions` (its changes in order, as the trace lists them, with
    full diffs by call id): whether it exists (None: unknown), its text if the diffs determine
    it, and whether that text's sha256 matched the record."""
    exists: bool | None = None
    text: str | None = None
    verified = False
    for version in versions:
        if version["op"] == "removed":
            exists, text, verified = False, None, True
            continue
        exists = True
        diff = diffs.get(version["id"])
        base: list[str] | None = None
        if diff is not None and version.get("kind", "file") == "file":
            if diff.startswith("--- /dev/null"):
                base = []
            elif text is not None and verified:
                before = version.get("before_sha256")
                base = text.splitlines() if before in (None, _sha256(text)) else None
        if base is None:
            text, verified = None, False
            continue
        rebuilt = apply_diff(base, diff or "")
        if rebuilt is None:
            text, verified = None, False
            continue
        joined = "\n".join(rebuilt)
        match = next(
            (c for c in (joined + "\n", joined) if _sha256(c) == version.get("sha256")), None
        )
        text, verified = (match, True) if match is not None else (joined, False)
    return exists, text, verified


def file_after(
    versions: Sequence[Mapping[str, Any]], diffs: Mapping[str, str], event: int
) -> dict[str, Any]:
    """A changed path as it was after the event at index `event`: what the diffs say, and which
    lines its last change touched. `text` is None when the diffs cannot tell; the caller can then
    read the snapshot."""
    earlier = [v for v in versions if v["event"] <= event]
    later = [v for v in versions if v["event"] > event]
    if not earlier:
        return {
            "exists": None,
            "text": None,
            "verified": False,
            "last_change": None,
            "changed_lines": [],
            "next_change": later[0]["id"] if later else None,
            "note": "No call had changed this path yet.",
        }
    exists, text, verified = rebuild(earlier, diffs)
    last = earlier[-1]
    last_diff = diffs.get(last["id"])
    if exists is False:
        note = "Deleted by this point."
    elif text is not None and verified:
        note = "Rebuilt from the recorded diffs; the sha256 matches."
    elif text is not None:
        note = "Rebuilt from the recorded diffs, but the sha256 does not match."
    else:
        note = "The recorded diffs cannot rebuild this version."
    return {
        "exists": exists,
        "text": text if exists is not False else None,
        "verified": verified,
        "sha256": last.get("sha256"),
        "last_change": last["id"],
        "changed_lines": added_lines(last_diff) if last_diff else [],
        "next_change": later[0]["id"] if later else None,
        "note": note,
    }


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class SnapshotReader:
    """Reads one file from a checkpoint image, with a small cache (images never change) and
    at most two reads at once."""

    def __init__(self, runner: Runner = subprocess.run, cache_size: int = 128) -> None:
        self._run = runner
        self._cache: OrderedDict[tuple[str, str], tuple[bytes | None, str]] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(2)

    def read(self, image: str, path: str) -> tuple[bytes | None, str]:
        """The file's bytes (cut to MAX_FILE_BYTES), or None and why not."""
        key = (image, path)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        command = [
            "docker", "run", "--rm", "--pull", "never", "--network", "none",
            "-u", "0", "--entrypoint", "cat", image, "--", path,
        ]  # fmt: skip
        with self._slots:
            try:
                done = self._run(command, capture_output=True, timeout=SNAPSHOT_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired) as error:
                return None, f"could not run docker: {error}"
        if done.returncode != 0:
            message = done.stderr.decode(errors="replace").strip().splitlines()
            result: tuple[bytes | None, str] = (None, message[-1] if message else "docker failed")
        else:
            result = (done.stdout[:MAX_FILE_BYTES], "")
        with self._lock:
            self._cache[key] = result
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result
