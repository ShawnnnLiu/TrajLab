"""Line blame across a trial's timeline, and what a fix changes (ADR-0013, decision 5).

`blame` follows one file through its versions at successive timeline points with line diffs
(difflib, no junk heuristic): a line kept from the previous version keeps its origin, and any
other line originates at the point where it first appears in its current form. A version that
is absent (the artifact did not exist there) resets every origin. Lines are compared after
`normalize`: trailing whitespace is ignored and numbers are compared by value, so rewriting `8`
as `8.00` does not move the blame. A JSON file is compared in a pretty-printed `view` with sorted
keys, so a one-line JSON output is blamed value by value.

`fix_hunks` lists what a fix does to a file's final version, comparing lines exactly: lines it
removes or replaces, and points where it only inserts. `apply_hunks` rebuilds a version with
only some of them, and `unified_diff` writes a diff `git apply -p1` accepts.
"""

import difflib
import json
import re
from dataclasses import dataclass
from typing import Literal

View = Literal["lines", "json"]
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])")


def _canonical_number(found: re.Match[str]) -> str:
    try:
        return repr(float(found.group(0)))  # the exact value: 8, 8.0 and 8.00 are equal
    except ValueError:
        return found.group(0)


def split_lines(text: str) -> list[str]:
    """Lines as git and diff see them: split on newlines only, no line endings kept."""
    lines = text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def split_keep(text: str) -> list[str]:
    """Lines split on newlines only, each keeping its newline (the last may lack one)."""
    return [line for line in re.split(r"(?<=\n)", text) if line]


def normalize(line: str) -> str:
    """A line as blame compares it: numbers by value, trailing whitespace dropped."""
    return _NUMBER.sub(_canonical_number, line.rstrip())


def view_of(path: str, lines: list[str] | None) -> tuple[View, list[str] | None]:
    """The lines blame works on: a JSON file pretty-printed with sorted keys, else as is."""
    if lines is None or not path.endswith(".json"):
        return "lines", lines
    try:
        parsed = json.loads("\n".join(lines))
    except ValueError:
        return "lines", lines
    # Without trailing commas, so adding a key after a value does not change the value's line.
    pretty = json.dumps(parsed, indent=1, sort_keys=True).split("\n")
    return "json", [line.removesuffix(",") for line in pretty]


def _matcher(a: list[str], b: list[str], *, exact: bool = False) -> difflib.SequenceMatcher[str]:
    if exact:
        return difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return difflib.SequenceMatcher(
        a=[normalize(x) for x in a], b=[normalize(x) for x in b], autojunk=False
    )


def blame(versions: list[tuple[int, list[str] | None]]) -> list[int]:
    """For each line of the last version, the point index that introduced it.

    `versions` is `(point index, lines or None if the file is absent)` in timeline order; the
    last version must be present.
    """
    origin: list[int] = []
    previous: list[str] | None = None
    for index, lines in versions:
        if lines is None:
            origin, previous = [], None
            continue
        current = [index] * len(lines)
        if previous is not None:
            for tag, i1, i2, j1, j2 in _matcher(previous, lines).get_opcodes():
                if tag == "equal":
                    current[j1:j2] = origin[i1:i2]
        origin, previous = current, lines
    if previous is None:
        raise ValueError("the last version is absent; nothing to blame")
    return origin


def first_seen(versions: list[tuple[int, list[str] | None]]) -> dict[str, int]:
    """For each normalized line, the earliest point at which some version of the file holds it.

    Last-writer blame credits a line that was written, reverted, and written again (or restored
    from a copy) to the last writer; this gives the earliest introduction of the same text.
    """
    seen: dict[str, int] = {}
    for index, lines in versions:
        for line in lines or []:
            seen.setdefault(normalize(line), index)
    return seen


@dataclass(frozen=True)
class Hunk:
    """One change a fix makes to a file's final version; line numbers are 1-based."""

    removed: tuple[int, ...]  # final-version lines the fix removes or replaces
    inserted_before: int | None  # for a pure insertion: the final line it goes before
    added: int  # lines the fix adds


def fix_hunks(final: list[str], fixed: list[str]) -> list[Hunk]:
    """The changes from `final` to `fixed`, compared exactly (a fix's every byte counts)."""
    hunks = []
    for tag, i1, i2, j1, j2 in _matcher(final, fixed, exact=True).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            hunks.append(Hunk(removed=(), inserted_before=i1 + 1, added=j2 - j1))
        else:
            hunks.append(
                Hunk(removed=tuple(range(i1 + 1, i2 + 1)), inserted_before=None, added=j2 - j1)
            )
    return hunks


def changed_lines(hunks: list[Hunk]) -> int:
    """The size of a fix: lines removed or replaced plus lines added."""
    return sum(len(h.removed) + h.added for h in hunks)


def anchors(hunk: Hunk, length: int) -> tuple[int, ...]:
    """For a pure insertion, the final-version lines on either side of it."""
    if hunk.inserted_before is None:
        return ()
    return tuple(
        line for line in (hunk.inserted_before - 1, hunk.inserted_before) if 1 <= line <= length
    )


def raw_hunks(final: list[str], fixed: list[str]) -> list[tuple[int, int, int, int]]:
    """The non-equal opcodes `(i1, i2, j1, j2)` from `final` to `fixed`, compared exactly."""
    return [
        (i1, i2, j1, j2)
        for tag, i1, i2, j1, j2 in _matcher(final, fixed, exact=True).get_opcodes()
        if tag != "equal"
    ]


def apply_hunks(final: list[str], fixed: list[str], keep: set[int]) -> list[str]:
    """`final` with only the raw hunks whose indexes are in `keep` applied."""
    out: list[str] = []
    position = 0
    for index, (i1, i2, j1, j2) in enumerate(raw_hunks(final, fixed)):
        out += final[position:i1]
        out += fixed[j1:j2] if index in keep else final[i1:i2]
        position = i2
    return out + final[position:]


def unified_diff(path: str, before: str | None, after: str | None) -> str:
    """A git-style diff of one file (`path` relative to the container root)."""
    old = split_keep(before or "")
    new = split_keep(after or "")
    out = []
    for line in difflib.unified_diff(
        old,
        new,
        fromfile=f"a/{path}" if before is not None else "/dev/null",
        tofile=f"b/{path}" if after is not None else "/dev/null",
    ):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    header = f"diff --git a/{path} b/{path}\n"
    if not out:
        if before is None and after == "":
            return header + "new file mode 100644\n"  # git applies a header-only diff
        if after is None and before == "":
            return header + "deleted file mode 100644\n"
        return ""
    if before is None:
        header += "new file mode 100644\n"
    elif after is None:
        header += "deleted file mode 100644\n"
    return header + "".join(out)
