"""What a retry prompt must not hold: a check name or the verifier's text (technical spec §5.1,
§9; ADR-0016 item 1). The failure signal is binary, the word FAILED.

The needles are read from the task's own tests (Harbor's cached copy of the task, named by the
root failure's `lock.json`) and from the root failure's verifier output:

- check names: every `def test...` and `class Test...` in the task's test files, and every test
  name in the verifier's `ctrf.json` (the part after `::`, without pytest's `[params]`);
- verifier text: every line of `test-stdout.txt` and `test-stderr.txt`, and every string value
  of a JSON file the verifier wrote (`ctrf.json`'s messages and traces, a scored task's
  `reward_details.json` reasons), that is at least `MIN_VERIFIER_LINE` characters once stripped.

A task scored by a grader rather than tests (the freecad tasks) has no check names; its needles
are its verifier text. Only a task whose tests cannot be read at all (not in Harbor's cache, and
no `ctrf.json`) is refused, so a missing cache is never read as a clean prompt.

A needle the task instruction itself holds is not a leak: the instruction is the task's, the
same in every arm and in the root failure. Every other hit is.
"""

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from harbor.models.task.id import PackageTaskId
from harbor.models.trial.paths import TrialPaths

# Shorter lines are pytest's furniture (`PASSED`, `====`, `collected 2 items`), which say
# nothing about a check; a line this long that the prompt repeats is the verifier's text.
MIN_VERIFIER_LINE = 24
VERIFIER_TEXT_FILES = ("test-stdout.txt", "test-stderr.txt")
CTRF_FILENAME = "ctrf.json"
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)", re.MULTILINE)
_TEST_CLASS = re.compile(r"^\s*class\s+(Test\w*)", re.MULTILINE)


class LeakCheckError(ValueError):
    """The needles cannot be read: no task tests and no verifier output to check against."""


@dataclass(frozen=True)
class Needles:
    check_names: frozenset[str]
    verifier_lines: frozenset[str]

    def __bool__(self) -> bool:
        return bool(self.check_names or self.verifier_lines)


def task_tests_dir(trial_dir: Path) -> Path | None:
    """The task's `tests/` in Harbor's package cache, from the trial's `lock.json`; None when the
    task is not a cached package."""
    lock_path = trial_dir / "lock.json"
    if not lock_path.is_file():
        return None
    task = json.loads(lock_path.read_text()).get("task") or {}
    name, digest = task.get("name"), task.get("digest")
    if task.get("type") != "package" or not name or "/" not in name or not digest:
        return None
    org, short = name.split("/", 1)
    tests = PackageTaskId(org=org, name=short, ref=digest).get_local_path() / "tests"
    return tests if tests.is_dir() else None


def _ctrf_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    tests = (json.loads(path.read_text()).get("results") or {}).get("tests") or []
    names = set()
    for test in tests:
        name = str(test.get("name") or "").rsplit("::", 1)[-1].split("[", 1)[0].strip()
        if name:
            names.add(name)
    return names


def _source_names(tests_dir: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted(tests_dir.rglob("*.py")):
        text = path.read_text(errors="replace")
        names.update(_TEST_DEF.findall(text))
        names.update(_TEST_CLASS.findall(text))
    return names


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield from value.splitlines()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _verifier_lines(verifier_dir: Path) -> set[str]:
    texts: list[str] = []
    for name in VERIFIER_TEXT_FILES:
        path = verifier_dir / name
        if path.is_file():
            texts.extend(path.read_text(errors="replace").splitlines())
    for path in sorted(verifier_dir.glob("*.json")):
        try:
            texts.extend(_strings(json.loads(path.read_text(errors="replace"))))
        except json.JSONDecodeError:
            continue
    return {line.strip() for line in texts if len(line.strip()) >= MIN_VERIFIER_LINE}


def needles(trial_dir: Path, tests_dir: Path | None = None) -> Needles:
    """What the prompts of a chain rooted at `trial_dir` must not hold. `tests_dir` defaults to
    the task's cached `tests/`. Raises `LeakCheckError` when the task's tests cannot be read
    (no tests dir and no `ctrf.json`)."""
    tests_dir = tests_dir if tests_dir is not None else task_tests_dir(trial_dir)
    verifier_dir = TrialPaths(trial_dir).verifier_dir
    ctrf = verifier_dir / CTRF_FILENAME
    if tests_dir is None and not ctrf.is_file():
        raise LeakCheckError(
            f"{trial_dir}: the task's tests cannot be read (not in Harbor's cache, no "
            f"{CTRF_FILENAME})"
        )
    names = _ctrf_names(ctrf)
    if tests_dir is not None:
        names |= _source_names(tests_dir)
    return Needles(frozenset(names), frozenset(_verifier_lines(verifier_dir)))


def _named(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def hits(text: str, found: Needles) -> list[str]:
    """Every needle in `text`: check names as whole identifiers, verifier lines as substrings."""
    named = [f"check name {name}" for name in sorted(found.check_names) if _named(text, name)]
    quoted = [f"verifier text {line!r}" for line in sorted(found.verifier_lines) if line in text]
    return named + quoted


def leaks(prompt: str, instruction: str, found: Needles) -> list[str]:
    """The needles in `prompt` that the task instruction does not hold itself."""
    own = set(hits(instruction, found))
    return [hit for hit in hits(prompt, found) if hit not in own]


def all_leaks(prompts: Iterable[tuple[str, str]], found: Needles) -> list[str]:
    """`leaks` over (prompt, instruction) pairs, each hit once."""
    seen: dict[str, None] = {}
    for prompt, instruction in prompts:
        seen.update(dict.fromkeys(leaks(prompt, instruction, found)))
    return list(seen)
