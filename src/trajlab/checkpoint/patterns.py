"""The checker: rule-based failure-pattern flags for Claude Code runs (ADR-0015 §3, v0 ADR-0023).

`Checker` is fed one hooked call at a time, with only what the watcher holds when that call's
hook fires (ADR-0015 §3): the call's hook input, `CallRecord`, `CallChanges` and
`ProcessSnapshot`, and its own memory of the calls before. It never sees a later call, so the
flags for the first N calls are the same whether a replay stops at N or runs the whole trial,
and the same inputs always give the same flags. No model calls.

v0 checks, each tied to the call that triggered it:

- `self_kill`: `pkill -f`, `pgrep -f`, or `kill $(pgrep -f ...)` whose pattern matches the
  call's own command text, on a call that exited 143 or 144.
- `silent_no_write`: a call meant to write a path (Write/Edit/NotebookEdit, or a shell
  redirect, `tee`, `sed -i`, `cp`, `mv`) exits 0 and leaves the path unchanged, with a complete
  record. Identical rewrites, held background jobs, visible failures, and anything the records
  cannot decide are not flagged; the last two kinds are recorded as notes and unknowns.
  Subtype `promised`: a write promised inside a program, read only when its path is literal
  (`-o <path>`, or `open(<path>, 'w')` and a few other forms in Python code the call runs), on a
  call with no crash in its output; a promised file that changed is `empty` or `broken` JSON.
- `masked_error`: the call exits 0 but its output shows a crash. The answer to an import
  probe (`python -c "import X; print(X.__version__)"` failing with ImportError) is not one.
- at the stop, `unverified_completion` (no check after the last change, or the last check
  failed) and `missing_output` (a path the instruction asks for is absent). A check is the
  shared test-command list, a command the instruction shows, a script it names that exists at
  the start, or (task-agnostic, a later call) running, importing or comparing a file the last
  change touched. With none of these defined, the stop is noted `no check defined`.

Left out of v0, with ADR-0023's reasons: `loop` and `stall` (the off corpus's six loops were
deliberate undos); `false_edit_claim` (2 of 104 missing writes were claims in the agent's own
words); `false_test_claim` (covered for now by `unverified_completion`); `server_state` (agents
kill and restart their own servers on purpose, so it needs labels first); `forbidden_action`
(test edits are sometimes asked for, which needs task-specific rules). ADR-0020's attribution
of late writes uses later calls, so the checker cannot use it.

`checkpoint/` imports other trajlab packages only through `contracts/` (CLAUDE.md
constraint 3).
"""

import ast
import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from harbor.models.task.task import Task
from harbor.models.trajectories import Trajectory
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import TrialPaths
from pydantic import BaseModel, ValidationError

from trajlab.checkpoint.changes import excluded
from trajlab.checkpoint.shell import (
    Script,
    ShellParseError,
    SimpleCommand,
    Word,
    basename,
    resolve,
)
from trajlab.checkpoint.shell import parse as parse_shell
from trajlab.contracts import (
    CALL_INPUT_SUFFIX,
    CALLS_FILENAME,
    CHANGES_FILENAME,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    HARNESS_PROCESS_PATTERN,
    PROCESSES_FILENAME,
    TEST_COMMAND_PATTERN,
    TIMEOUT_SUFFIX,
    TRAJLAB_DIRNAME,
    CallChanges,
    CallRecord,
    CheckpointRecord,
    CheckRecord,
    FileChange,
    FlagRecord,
    HeldWrite,
    ProcessSnapshot,
    UnknownItem,
)

logger = logging.getLogger(__name__)

DETECTOR_VERSION = "checker-v0"
HARNESS_PROCESS = re.compile(HARNESS_PROCESS_PATTERN)
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path"}
NOTEBOOK_TOOLS = {"NotebookEdit": "notebook_path"}
SELF_KILL_EXITS = (143, 144)  # 128 + SIGTERM, and Claude Code's code for a killed shell

# Output that shows a crash, chosen from the exit-0 Bash outputs of the 40 off runs
# (ADR-0023). "command not found" and "No such file or directory" are left out: in those runs
# they are mostly the agent probing for a tool or a path.
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")
_EXCEPTION_LINE = re.compile(r"(?m)^(?:[\w.]+\.)?(\w*(?:Error|Exception|Exit)): ")
_CRASH_LINES = re.compile(
    r"(?m)^\s*(?:SyntaxError|IndentationError|TabError): .*$"  # compile errors: no traceback
    r"|^.*can't open file .*\[Errno 2\].*$"  # python3 <missing script>
    r"|^.*(?:Segmentation fault|core dumped).*$"
    r"|^npm ERR! .*$"
    r"|^\S+:\d+:\d+: (?:fatal )?error: .*$"  # gcc and clang
    r"|^make(?:\[\d+\])?: \*\*\* .*$"
)
# The shared test-command list. Task-specific checks come only from the instruction (the commands
# it shows and the scripts it names), never from what agents ran (ADR-0023).
TEST_COMMAND = re.compile(TEST_COMMAND_PATTERN)
_TEST_FAILED = re.compile(
    r"\b[1-9]\d* (?:failed|failures?|errors?)\b|\bFAILED\b|(?m:^FAIL\b)|\bAssertionError\b"
    r"|(?m:^make(?:\[\d+\])?: \*\*\* )"
)
_EXIT_CODE = re.compile(r"Exit code (-?\d+)")
# Bytecode and test caches that running code writes; a call whose changes are only these did not
# change the agent's work, so `unverified_completion` does not count it as the last change.
_CACHE_PATH = re.compile(r"(?:^|/)(?:__pycache__|\.pytest_cache)(?:/|$)|\.pyc$")
_INSTRUCTION_PATH = re.compile(r"(?<![\w.:/~-])/(?:[\w.+@-]+/)*[\w.+@-]*/?")
_NOT_FILES = ("/dev/", "/proc/", "/sys/")
_TEXT_KEPT = (".py", ".json")


def crash_lines(output: str) -> list[str]:
    """Every line that shows a crash, in output order. A traceback whose only exception is a
    `BrokenPipeError` (output piped into `head`) is not a crash."""
    found: list[tuple[int, str]] = []
    if _TRACEBACK.search(output):
        lines = list(_EXCEPTION_LINE.finditer(output))
        real = [m for m in lines if m.group(1) != "BrokenPipeError"]
        found.extend((m.start(), output[m.start() :].split("\n", 1)[0].strip()) for m in real)
        if not lines:
            found.append((_TRACEBACK.search(output).start(), "Traceback (most recent call last)"))  # type: ignore[union-attr]
    found.extend((m.start(), m.group(0).strip()) for m in _CRASH_LINES.finditer(output))
    return [line for _, line in sorted(found)]


def crash_line(output: str) -> str | None:
    """The first line that shows a crash, or None."""
    lines = crash_lines(output)
    return lines[0] if lines else None


# An import probe: `python -c` code that only imports modules and prints, run to see whether
# they are installed. Its ImportError is the answer the agent asked for, so nothing is masked.
_PRINTED = r"(?:[\w.]+|'[^']*'|\"[^\"]*\")"  # print only names, attributes and literals
_PROBE_STATEMENT = re.compile(
    r"\s*(?:import\s+[\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*"
    r"|from\s+[\w.]+\s+import\s+[\w\s,*()]+"
    rf"|print\(\s*(?:{_PRINTED}(?:\s*,\s*{_PRINTED})*)?\s*\)|)\s*"
)
_IMPORT_ERROR = re.compile(r"^(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError): ")
_PYTHON = re.compile(r"python(?:\d(?:\.\d+)?)?$|pypy3?$")


def imported_modules(code: str) -> list[str]:
    """Top-level-first dotted names of the modules Python `code` imports (`import a.b, c as d`,
    `from a.b import x`)."""
    modules: list[str] = []
    for line in re.split(r"[\n;]", code):
        line = line.strip()
        match = re.match(r"from\s+([\w.]+)\s+import\b", line)
        if match:
            modules.append(match.group(1))
            continue
        match = re.match(r"import\s+(.+)$", line)
        if match:
            for part in match.group(1).split(","):
                name = part.split(" as ")[0].strip()
                if re.fullmatch(r"[\w.]+", name):
                    modules.append(name)
    return modules


def probe_modules(script: Script) -> set[str]:
    """The top-level modules a command's import probes ask for."""
    modules: set[str] = set()
    for simple in script.commands:
        args = simple.args_from()
        if not args or not _PYTHON.match(basename(args[0])) or len(args) < 3 or args[1] != "-c":
            continue
        code = args[2]
        if all(_PROBE_STATEMENT.fullmatch(s) for s in re.split(r"[\n;]", code)):
            modules.update(m.split(".")[0] for m in imported_modules(code))
    return modules


def _probe_answer(line: str, probes: set[str]) -> bool:
    if not _IMPORT_ERROR.match(line):
        return False
    return any(re.search(rf"['\"]{re.escape(m)}(?:[.'\"])", line) for m in probes)


# -- what a call meant to write ---------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A path a call meant to write. `any_of`: the write happened if any of these changed (a
    `cp` or `mv` whose destination may be a directory); `tree`: changes below a path count."""

    path: str
    how: str
    any_of: tuple[str, ...]
    tree: bool = False
    removed_later: bool = False  # a later `rm` or `mv` in the same command takes it away
    # A write promised inside a program (`-o <path>`, or Python code the call runs): where the
    # promise is, its code line, and the blocks around it. None for the shell's own writes.
    promise: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Invocation:
    """One simple command, as the stop's check rules read it."""

    name: str  # the program's basename, after wrappers such as `timeout` and `env`
    operands: tuple[str, ...]  # its arguments as written, options left out
    paths: tuple[str, ...]  # what it runs, or passes to an interpreter or a comparer, resolved
    code: tuple[str, ...]  # code given to an interpreter: `-c`/`-e`, a heredoc, `-m module`
    cwd: str | None  # the shell's directory when it runs
    workdir: str | None  # the program's directory: `-C <dir>` if given, else `cwd`
    index: int = 0  # the simple command's place in the call's command


@dataclass
class ShellReading:
    targets: list[Target] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)  # (written text, reason)
    background: bool = False  # `&` or `nohup` at some command
    cwd_known: bool = True
    invocations: list[Invocation] = field(default_factory=list)
    removals: list[tuple[int, str]] = field(default_factory=list)  # (command index, path)


# Programs that run a file given to them, or compare files. A path passed to one of these is
# used, not just looked at; `cat`, `ls`, `grep` and editors are not in the list.
_INTERPRETER = re.compile(
    r"(?:python(?:\d(?:\.\d+)?)?|pypy3?|node|deno|bun|ts-node|tsx|ruby|perl|php|Rscript|lua"
    r"|java)$"
)
_SHELLS = {"bash", "sh", "zsh", "dash", "source", "."}
_COMPARERS = {"diff", "cmp", "comm", "sdiff", "colordiff", "compare"}
_RUNNERS_OF_OTHERS = {"npx"}
# Wrappers that run the command after them, with their options that take a value.
_WRAPPERS = {
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "env": {"-u", "--unset", "-C", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "stdbuf": {"-i", "-o", "-e"},
    "xvfb-run": {"-n", "-s", "-f", "-e", "-p"},
    "unbuffer": set(),
    "nohup": set(),
}


def _unwrap(words: list[Word]) -> list[Word]:
    """The words from the real program on: `timeout 60 python x` is `python x`, and
    `uv run python x` is `python x`."""
    while words:
        name = basename(words[0].text)
        if name == "uv" and len(words) > 1 and words[1].text == "run":
            words = words[2:]
            while words and words[0].text.startswith("-"):
                words = words[1:]
            continue
        if name not in _WRAPPERS:
            return words
        takes_value = _WRAPPERS[name]
        i = 1
        while i < len(words):
            text = words[i].text
            if text in takes_value:
                i += 2
            elif text.startswith("-") or _ASSIGNMENT.match(text):
                i += 1
            else:
                break
        if name == "timeout" and i < len(words):
            i += 1  # the duration
        words = words[i:]
    return words


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _invocations(
    words: list[Word], redirects: list[Any], bodies: list[str], here: str | None
) -> list[Invocation]:
    """How one simple command uses files: what it runs, and the code it gives an interpreter.
    `bodies` are its heredoc bodies, in order."""
    words = _unwrap(words)
    if not words:
        return []
    name = basename(words[0].text)
    paths: list[str] = []
    code: list[str] = []
    nested: list[Invocation] = []
    operands: list[str] = []
    workdir = here

    def add_path(word: Word) -> None:
        if word.text == "-":
            return  # stdin
        path, _ = _path_of(word, here)
        if path is not None:
            paths.append(path)

    if "/" in words[0].text:
        add_path(words[0])  # ./run.sh, /app/build/test_x
    interpreter = _INTERPRETER.match(name) is not None
    shell = name in _SHELLS
    i = 1
    while i < len(words):
        word = words[i]
        text = word.text
        if text in ("-c", "-e", "--eval") and (interpreter or shell) and i + 1 < len(words):
            body = words[i + 1].text
            if shell:
                nested.extend(read_command(body, here)[1].invocations)
            else:
                code.append(body)
            i += 2
            break  # what follows is the code's own argv
        if text == "-m" and interpreter and i + 1 < len(words):
            code.append(f"import {words[i + 1].text}")
            operands.extend(w.text for w in words[i + 1 :])
            for later in words[i + 2 :]:
                if not later.text.startswith("-"):
                    add_path(later)
            break
        if text == "-C" and i + 1 < len(words):
            workdir = resolve(words[i + 1].text, here) if not words[i + 1].expands else None
            i += 2
            continue
        if text.startswith("-") and text != "-":
            i += 1
            continue
        operands.append(text)
        if interpreter or shell or name in _COMPARERS or name in _RUNNERS_OF_OTHERS:
            add_path(word)
        i += 1
    body_index = 0
    for redirect in redirects:
        if redirect.op in ("<<", "<<-"):
            body = bodies[body_index] if body_index < len(bodies) else ""
            body_index += 1
            if interpreter:
                code.append(body)
            elif shell:
                nested.extend(read_command(body, here)[1].invocations)
        elif redirect.op == "<<<" and interpreter:
            code.append(redirect.target.text)
        elif redirect.op == "<" and (interpreter or shell):
            add_path(redirect.target)
    own = Invocation(
        name=name,
        operands=tuple(operands),
        paths=tuple(paths),
        code=tuple(code),
        cwd=here,
        workdir=workdir,
    )
    return [own, *nested]


def _path_of(word: Word, cwd: str | None) -> tuple[str | None, str | None]:
    """(absolute path, None) or (None, why it cannot be known)."""
    if word.expands:
        return None, "expansion"
    if word.globs:
        return None, "glob"
    if word.tilde:
        return None, "home"
    if not word.text:
        return None, "empty"
    path = resolve(word.text, cwd)
    return (path, None) if path is not None else (None, "relative_unknown_cwd")


def _options_split(args: list[str], takes_value: set[str]) -> tuple[list[str], list[str]]:
    """(options, operands) of a command's arguments, with `--` ending the options."""
    options, operands = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            operands.extend(args[i + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            options.append(arg)
            if arg in takes_value and i + 1 < len(args):
                options.append(args[i + 1])
                i += 1
        else:
            operands.append(arg)
        i += 1
    return options, operands


def _sed_files(args: list[str]) -> list[str] | None:
    """The files `sed -i` edits, or None if it does not edit in place."""
    in_place = False
    script_given = False
    operands: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            operands.extend(args[i + 1 :])
            break
        if arg.startswith("--"):
            if arg.startswith("--in-place"):
                in_place = True
            elif arg in ("--expression", "--file"):
                script_given = True
                i += 1
            elif arg.startswith(("--expression=", "--file=")):
                script_given = True
        elif arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            for j, letter in enumerate(letters):
                if letter == "i":
                    in_place = True
                    break  # the rest is the backup suffix
                if letter in "ef":
                    script_given = True
                    if j == len(letters) - 1:
                        i += 1  # the script or script file is the next argument
                    break
                if letter == "l":
                    if j == len(letters) - 1:
                        i += 1
                    break
        else:
            operands.append(arg)
        i += 1
    if not in_place:
        return None
    return operands if script_given else operands[1:]


_CP_VALUE_OPTIONS = {"-t", "--target-directory", "-S", "--suffix"}


def read_command(command: str, cwd: str | None) -> tuple[Script | None, ShellReading]:
    """What a Bash command meant to write, resolving relative paths against `cwd` and any `cd`
    before them in the same command. Text in heredoc bodies, `python -c` strings, and command
    substitutions is never read as a path."""
    reading = ShellReading()
    try:
        script = parse_shell(command)
    except ShellParseError as error:
        reading.unknown.append((command[:120], f"unparsable: {error}"))
        return None, reading
    cwds: dict[int, str | None] = {0: cwd}  # by subshell depth

    def current(depth: int) -> str | None:
        while depth > 0 and depth not in cwds:
            depth -= 1
        return cwds.get(depth)

    previous_depth = 0
    written: list[tuple[int, int]] = []  # (command index, target index)
    removals: list[tuple[int, str]] = []
    bodies = iter(script.heredocs)
    for index, simple in enumerate(script.commands):
        first_target = len(reading.targets)
        own_bodies = [next(bodies, "") for r in simple.redirects if r.op in ("<<", "<<-")]
        if simple.depth < previous_depth:
            for depth in [d for d in cwds if d > simple.depth]:
                del cwds[depth]
        previous_depth = simple.depth
        here = current(simple.depth)
        if simple.ended_by == "&" or (simple.words and simple.words[0].text in ("nohup", "setsid")):
            reading.background = True
        for redirect in simple.redirects:
            if redirect.op in (">", ">>", ">|", "&>", "&>>"):
                _add(reading, redirect.target, here, redirect.op)
        args = simple.args_from()
        offset = len(simple.words) - len(args)
        words = simple.words[offset:]
        name = args[0] if args else ""
        if name != "cd":
            reading.invocations.extend(
                replace(i, index=index)
                for i in _invocations(words, simple.redirects, own_bodies, here)
            )
            _output_flags(
                reading, basename(name), words, here, script.text[simple.start : simple.end]
            )
        if name == "cd":
            operands = [w for w in words[1:] if not w.text.startswith("-") or w.text == "-"]
            if not operands or operands[0].text == "-" or operands[0].expands:
                cwds[simple.depth] = None
            else:
                path, _ = _path_of(operands[0], here)
                cwds[simple.depth] = path
            continue
        if name == "tee":
            for word in words[1:]:
                if not word.text.startswith("-"):
                    _add(reading, word, here, "tee")
        elif name == "sed":
            files = _sed_files(args[1:])
            if files is not None:
                by_text = {w.text: w for w in words[1:]}
                for text in files:
                    _add(reading, by_text.get(text, Word(text, text)), here, "sed -i")
        elif name in ("cp", "mv"):
            _copy_targets(reading, name, words, here)
        if name in ("rm", "mv"):
            operands = [w for w in words[1:] if not w.text.startswith("-")]
            for word in operands[:-1] if name == "mv" else operands:
                path, _ = _path_of(word, here)
                if path is not None:
                    removals.append((index, path))
        written.extend((index, t) for t in range(first_target, len(reading.targets)))
    for index, t in written:
        target = reading.targets[t]
        if any(
            later > index and (target.path == gone or target.path.startswith(gone + "/"))
            for later, gone in removals
        ):
            reading.targets[t] = replace(target, removed_later=True)
    reading.cwd_known = all(value is not None for value in cwds.values())
    reading.removals = removals
    return script, reading


# `-o` is not an output path for these: a flag with no value, or an option list (`ssh -o`,
# `mount -o`), or `find`'s "or".
_O_NOT_OUTPUT = {
    "grep", "egrep", "fgrep", "zgrep", "rg", "ls", "ps", "mount", "ssh", "scp", "sftp", "find",
    "set", "bash", "sh", "zsh", "dash", "pgrep", "pkill", "kill", "top", "df", "du", "tar", "zip",
    "unzip", "xargs", "od", "ip", "nm", "lsof", "netstat", "free", "cut", "uniq", "sed", "awk",
    "docker",
}  # fmt: skip
_OUTPUT_OPTIONS = ("-o", "--output", "--out")


def _output_flags(
    reading: ShellReading, name: str, words: list[Word], cwd: str | None, text: str
) -> None:
    """Writes a command promises with an output flag: `-o <path>`, `--output <path>`,
    `--out <path>` and their `=` forms. A path with an expansion is unknown, never guessed."""
    if name in _O_NOT_OUTPUT:
        return
    for i, word in enumerate(words[1:], start=1):
        flag, _, value = word.text.partition("=")
        if word.text in _OUTPUT_OPTIONS and i + 1 < len(words):
            target, how = words[i + 1], f"promised {name} {word.text}"
        elif value and flag in ("--output", "--out"):
            target, how = (
                Word(value, word.raw, word.expands, word.globs),
                f"promised {name} {flag}=",
            )
        else:
            continue
        if target.text.startswith("-") or target.text in ("/dev/null", "/dev/stdout", "-"):
            continue
        path, why = _path_of(target, cwd)
        if path is None:
            reading.unknown.append((target.raw, f"promised_{why or 'unknown'}"))
            continue
        promise = {"source": "output flag", "line": " ".join(text.split())[:300], "context": []}
        reading.targets.append(Target(path=path, how=how, any_of=(path,), promise=promise))


def _add(reading: ShellReading, word: Word, cwd: str | None, how: str) -> None:
    if word.text.startswith(_NOT_FILES) or word.text in ("/dev/null", "-"):
        return
    path, why = _path_of(word, cwd)
    if path is None:
        reading.unknown.append((word.raw, why or "unknown"))
        return
    if path.startswith(_NOT_FILES):
        return
    reading.targets.append(Target(path=path, how=how, any_of=(path,)))


def _copy_targets(reading: ShellReading, name: str, words: list[Word], cwd: str | None) -> None:
    args = [w.text for w in words[1:]]
    by_text = {w.text: w for w in words[1:]}
    options, operands = _options_split(args, _CP_VALUE_OPTIONS)
    directory = next(
        (options[i + 1] for i, o in enumerate(options[:-1]) if o in ("-t", "--target-directory")),
        None,
    ) or next((o.split("=", 1)[1] for o in options if o.startswith("--target-directory=")), None)
    if directory is not None:
        sources, destination = operands, directory
        into = True
    else:
        if len(operands) < 2:
            return
        sources, destination = operands[:-1], operands[-1]
        into = len(sources) > 1 or destination.endswith("/")
    dest_word = by_text.get(destination, Word(destination, destination))
    dest, why = _path_of(dest_word, cwd)
    if dest is None:
        reading.unknown.append((dest_word.raw, why or "unknown"))
        return
    for source in sources:
        source_word = by_text.get(source, Word(source, source))
        if source_word.expands or source_word.globs:
            reading.targets.append(Target(dest, name, (dest,), tree=True))
            continue
        inside = resolve(f"{dest}/{basename(source)}", "/") or dest
        any_of = (inside,) if into else (dest, inside)
        reading.targets.append(Target(dest if not into else inside, name, any_of, tree=True))


# -- writes promised inside Python code -------------------------------------------------------

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp")
_OPEN_MODULES = {"io", "codecs", "gzip", "bz2", "lzma"}
_PATH_CLASSES = {"Path", "PurePath", "PosixPath"}
_BLOCKS = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.FunctionDef,
    ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
)  # fmt: skip


@dataclass(frozen=True)
class Promise:
    """A write in Python code: the path (None when it is built from variables, an f-string or
    a join), the form, the code line, and the blocks it sits in (a write under an `if` or in a
    function may never run)."""

    path: str | None
    how: str
    line: str
    context: tuple[str, ...]


def _arg(call: ast.Call, position: int, *keywords: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((k.value for k in call.keywords if k.arg in keywords), None)


def _written_arg(call: ast.Call) -> tuple[ast.expr, str] | None:
    """The path argument of a call that writes a file, and the form, for the forms v0 reads."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    owner = func.value if isinstance(func, ast.Attribute) else None
    if name == "open" and (owner is None or getattr(owner, "id", None) in _OPEN_MODULES):
        mode = _arg(call, 1, "mode")
        path = _arg(call, 0, "file", "filename")
        if (
            isinstance(mode, ast.Constant)
            and isinstance(mode.value, str)
            and any(c in mode.value for c in "wax")
            and path is not None
        ):
            return path, f"open {mode.value!r}"
        return None
    if name in ("write_text", "write_bytes") and isinstance(owner, ast.Call):
        inner = owner.func
        if getattr(inner, "id", None) in _PATH_CLASSES or getattr(inner, "attr", None) in (
            _PATH_CLASSES
        ):
            path = _arg(owner, 0)
            return (path, f"Path().{name}") if path is not None else None
    if name in ("to_csv", "to_json") and owner is not None:
        path = _arg(call, 0, "path_or_buf")
        return (path, name) if path is not None else None  # no path: it returns a string
    if name == "savefig":
        path = _arg(call, 0, "fname")
        return (path, "savefig") if path is not None else None
    if name == "save" and getattr(owner, "id", None) in ("np", "numpy"):
        path = _arg(call, 0, "file")
        return (path, "np.save") if path is not None else None
    if name == "save" and owner is not None:
        path = _arg(call, 0, "fp")
        if isinstance(path, ast.Constant) and isinstance(path.value, str):
            if path.value.lower().endswith(_IMAGE_SUFFIXES):
                return path, ".save"
        return None  # `.save(name)`: not known to be an image, so not a promise either way
    return None


def python_promises(code: str, cwd: str | None) -> tuple[list[Promise], bool]:
    """The writes Python `code` promises, and whether it parsed. A path is read only when it is
    written literally; relative paths are against the process's directory, `cwd`."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return [], False
    lines = code.splitlines()
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: list[tuple[int, Promise]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        written = _written_arg(node)
        if written is None:
            continue
        arg, how = written
        context: list[str] = []
        up = parents.get(node)
        while up is not None:
            if isinstance(up, _BLOCKS):
                label = type(up).__name__
                context.append(f"{label} {up.name}" if hasattr(up, "name") else label)
            up = parents.get(up)
        line = lines[node.lineno - 1].strip()[:300] if node.lineno <= len(lines) else ""
        path = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
            path = resolve(arg.value, cwd)
            if path is not None and how == "np.save" and not path.endswith(".npy"):
                path += ".npy"  # numpy adds the suffix
        found.append((node.lineno, Promise(path, how, line, tuple(reversed(context)))))
    return [promise for _, promise in sorted(found, key=lambda f: f[0])], True


def apply_diff(old: list[str] | None, diff: str) -> list[str] | None:
    """The lines after a unified diff from `checkpoint/diffs.py`, or None if `old` is unknown or
    does not match it."""
    if old is None:
        return None
    out: list[str] = []
    pos = 0
    in_hunk = False
    for line in diff.split("\n") if diff else []:
        hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
        if hunk:
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            at = start - 1 if count > 0 else start
            if at < pos or at > len(old):
                return None
            out.extend(old[pos:at])
            pos, in_hunk = at, True
        elif not in_hunk:
            continue  # the --- and +++ header
        elif line.startswith("+"):
            out.append(line[1:])
        elif line.startswith((" ", "-")) or line == "":
            text = line[1:]
            if pos >= len(old) or old[pos] != text:
                return None
            if not line.startswith("-"):
                out.append(text)
            pos += 1
    out.extend(old[pos:])
    return out


# -- self-kill ---------------------------------------------------------------------------------

_PKILL_VALUE_OPTIONS = {"-d", "-g", "-G", "-P", "-s", "-t", "-u", "-U", "-F", "--signal"}


def _kill_pattern(simple: SimpleCommand) -> tuple[str, str] | None:
    """(tool, pattern) for a `pkill -f` or `pgrep -f` command, else None."""
    args = simple.args_from()
    if not args or args[0] not in ("pkill", "pgrep"):
        return None
    options, operands = _options_split(args[1:], _PKILL_VALUE_OPTIONS)
    full = any(
        o == "--full" or (o.startswith("-") and not o.startswith("--") and "f" in o[1:])
        for o in options
        if not re.fullmatch(r"-(?:\d+|[A-Z]+)", o)  # -9, -TERM: signals, not flags
    )
    if not full or not operands:
        return None
    if any(o == "-x" or (o[:1] == "-" and o[1:2] != "-" and "x" in o) for o in options):
        return None  # -x: the whole command line must match, which a shell's never does
    return args[0], operands[0]


def find_self_kill(script: Script, command: str) -> dict[str, Any] | None:
    """The first kill whose pattern matches the command's own text, with what never ran after
    it. Patterns are read as Python regexes, which agree with pkill's extended regexes on the
    patterns these runs use."""
    for simple in script.commands:
        found: list[tuple[str, str, str]] = []
        kill = _kill_pattern(simple)
        if kill is not None:
            found.append((*kill, "direct"))
        for sub in simple.substitutions:
            for inner in sub.commands:
                kill = _kill_pattern(inner)
                if kill is not None and kill[0] == "pgrep" and simple.name == "kill":
                    found.append((*kill, "kill $(pgrep)"))
        for tool, pattern, form in found:
            try:
                match = re.search(pattern, command)
            except re.error:
                continue
            if match is None:
                continue
            rest = command[simple.end :].lstrip(" \t;&|\n")
            return {
                "tool": tool,
                "form": form,
                "pattern": pattern,
                "matched": command[max(0, match.start() - 20) : match.end() + 20],
                "never_ran": rest[:400],
                "kill_command": command[simple.start : simple.end].strip()[:200],
            }
    return None


# -- what counts as a check, for unverified_completion -----------------------------------------

# A file the instruction names, that exists at the start, with one of these suffixes, is a
# provided script: running or importing it is a check (`/app/render.py`, `/app/run_pretrain.sh`).
SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl")
_MODULE_FILE = re.compile(r"(?:\.cpython-[\w-]+|\.abi3)?\.(?:py|pyx|so|pyd)$")
_INSTRUCTION_SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)|^[ \t]*\$ (.+)$", re.MULTILINE)
_COMMAND_PROGRAMS = {
    "make", "cmake", "ctest", "pytest", "tox", "nox", "npm", "yarn", "pnpm", "cargo", "go",
    "bash", "sh", "node", "deno", "bun",
}  # fmt: skip


def instruction_commands(instruction: str) -> list[Invocation]:
    """The shell commands an instruction shows, in backticks or as `$ ` lines, that run a
    program (`make -C /app repro`, `bash /app/run_pretrain.sh`). Code such as
    `import fast_networkx as fnx` is not a command."""
    found: list[Invocation] = []
    for match in _INSTRUCTION_SPAN.finditer(instruction):
        span = (match.group(1) or match.group(2) or "").strip()
        try:
            script = parse_shell(span)
        except ShellParseError:
            continue
        for simple in script.commands:
            words = simple.words[len(simple.words) - len(simple.args_from()) :]
            for invocation in _invocations(words, simple.redirects, [], None)[:1]:
                name = invocation.name
                if name in _COMMAND_PROGRAMS or _INTERPRETER.match(name):
                    if invocation not in found:
                        found.append(invocation)
    return found


def _same_program(a: str, b: str) -> bool:
    return a == b or (_PYTHON.match(a) is not None and _PYTHON.match(b) is not None)


def runs_command(invocation: Invocation, wanted: Invocation) -> bool:
    """Whether a call's command is an instruction's command: the same program, in the same
    directory when the instruction gives one (`-C /app`), with all its operands; options and
    extra operands are allowed (`make -C /app repro 2>&1 | tail`)."""
    if not _same_program(invocation.name, wanted.name):
        return False
    if wanted.workdir is not None and invocation.workdir != wanted.workdir:
        return False
    have = set(invocation.operands) | set(invocation.paths)
    have |= {p for p in (resolve(o, invocation.workdir) for o in invocation.operands) if p}
    return all(operand in have for operand in wanted.operands)


def mentions(code: str, path: str, cwd: str | None) -> bool:
    """Whether interpreter code names `path` (absolute, or relative to `cwd`) or imports it as
    a Python module (`import fast_networkx` uses `/app/fast_networkx/core.py`)."""
    end = r"(?![\w/-]|\.\w)"
    if re.search(rf"(?<![\w./-]){re.escape(path)}{end}", code):
        return True
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        relative = path[len(cwd.rstrip("/")) + 1 :]
        if re.search(rf"(?<![\w./-])(?:\./)?{re.escape(relative)}{end}", code):
            return True
    if _MODULE_FILE.search(path):
        stem = _MODULE_FILE.sub("", path) + "/"
        return any(f"/{m.replace('.', '/')}/" in stem for m in imported_modules(code))
    return False


def uses(invocation: Invocation, paths: Iterable[str]) -> str | None:
    """The first of `paths` that a command runs, passes to an interpreter or a comparer, or
    names or imports in interpreter code; None if it uses none of them."""
    wanted = [p for p in paths if p]
    for path in invocation.paths:
        if path in wanted:
            return path
    for code in invocation.code:
        for path in wanted:
            if mentions(code, path, invocation.cwd):
                return path
    return None


# -- the checker -------------------------------------------------------------------------------


@dataclass(frozen=True)
class CallView:
    """What the watcher holds for one answered call when its hook fires."""

    record: CallRecord
    hook_input: Mapping[str, Any] | None
    agent_id: str | None = None
    changes: CallChanges | None = None
    snapshot: ProcessSnapshot | None = None


@dataclass(frozen=True)
class StopFacts:
    """What the instruction gives the stop checks: the absolute file paths it names, whether
    each exists in the start image and at the stop (None: could not be read), and the commands
    it shows. A path absent at the start is an output (`missing_output`); one present at the
    start with a script suffix is a provided script (`unverified_completion`)."""

    paths: tuple[str, ...] = ()
    at_start: Mapping[str, bool | None] = field(default_factory=dict)
    at_stop: Mapping[str, bool | None] = field(default_factory=dict)
    commands: tuple[Invocation, ...] = ()

    @property
    def scripts(self) -> tuple[str, ...]:
        return tuple(
            p for p in self.paths if self.at_start.get(p) is True and p.endswith(SCRIPT_SUFFIXES)
        )


@dataclass
class _Seen:
    """What the checker keeps of an earlier call."""

    seq: int
    tool_call_id: str
    test: bool  # matched the shared test-command list
    failed: bool  # exited non-zero, or its output shows a failed test or a crash
    changed: tuple[str, ...]
    changed_total: int | None
    invocations: tuple[Invocation, ...] = ()


def _exit_code(hook_input: Mapping[str, Any], record: CallRecord) -> tuple[int | None, str]:
    """The call's exit code, and how it is known. Claude Code fails every Bash call that exits
    non-zero, so a call that did not fail exited 0, unless it went to the background or Claude
    Code read its exit code as a non-failure (grep's 1: `returnCodeInterpretation`)."""
    if record.tool_failed or hook_input.get("hook_event_name") == "PostToolUseFailure":
        match = _EXIT_CODE.search(str(hook_input.get("error") or ""))
        return (int(match.group(1)), "failed") if match else (None, "failed")
    response = hook_input.get("tool_response")
    if isinstance(response, dict):
        if response.get("backgroundTaskId"):
            return None, "background"
        if response.get("interrupted"):
            return None, "interrupted"
        if response.get("returnCodeInterpretation"):
            return None, "nonzero_not_failed"
    return 0, "ok"


def _output(hook_input: Mapping[str, Any]) -> str:
    response = hook_input.get("tool_response")
    if isinstance(response, dict):
        return f"{response.get('stdout') or ''}\n{response.get('stderr') or ''}"
    if hook_input.get("error"):
        return str(hook_input["error"])
    return str(response or "")


def _agent_pids(snapshot: ProcessSnapshot | None) -> dict[int, str]:
    if snapshot is None:
        return {}
    return {p.pid: p.cmd for p in snapshot.processes if not HARNESS_PROCESS.search(p.cmd)}


class Checker:
    """One trial's checker. Feed it every answered call in `call_seq` order."""

    def __init__(
        self,
        *,
        start_cwd: str | None = None,
        stop_facts: Callable[[], StopFacts] | StopFacts | None = None,
        uses_changed_file: bool = True,
    ) -> None:
        self.start_cwd = start_cwd
        self.stop_facts = stop_facts
        # The task-agnostic check: a later call runs, imports, renders or compares a file the
        # last change touched (ADR-0023).
        self.uses_changed_file = uses_changed_file
        self.seen: list[_Seen] = []
        self.cwd: dict[str | None, str | None] = {}
        self.previous_pids: dict[int, str] | None = None
        # For before-hashes against an older version (the off corpus's PR #32 fault):
        # the last hash read per path, and whether a call changed it unread since.
        self.read_hash: dict[str, str] = {}
        self.unread_since: dict[str, int] = {}
        self.text: dict[str, list[str] | None] = {}  # `.py` and `.json` files, from the diffs

    def check(self, view: CallView, now: datetime) -> tuple[list[FlagRecord], CheckRecord]:
        start = time.perf_counter()
        if view.record.trigger == "stop":
            flags, held, unknown, notes = self._stop(view, now)
        else:
            flags, held, unknown, notes = self._call(view, now)
        check_ms = round((time.perf_counter() - start) * 1000, 3)
        return flags, CheckRecord(
            tool_call_id=view.record.tool_call_id,
            call_seq=view.record.call_seq,
            event=view.record.trigger,
            check_ms=check_ms,
            flags=len(flags),
            held=tuple(held),
            unknown=tuple(unknown),
            notes=tuple(notes),
            detector_version=DETECTOR_VERSION,
            checked_at=now,
        )

    def _flag(
        self,
        view: CallView,
        now: datetime,
        pattern: str,
        subtype: str | None,
        evidence: dict[str, Any],
        message: str,
    ) -> FlagRecord:
        if view.agent_id:
            evidence = {**evidence, "agent_id": view.agent_id}
        return FlagRecord(
            tool_call_id=view.record.tool_call_id,
            call_seq=view.record.call_seq,
            event=view.record.trigger,
            pattern=pattern,  # type: ignore[arg-type]
            subtype=subtype,
            evidence=evidence,
            message=message,
            detector_version=DETECTOR_VERSION,
            flagged_at=now,
        )

    # -- one tool call -------------------------------------------------------------------------

    def _call(self, view: CallView, now: datetime):  # type: ignore[no-untyped-def]
        flags: list[FlagRecord] = []
        held: list[HeldWrite] = []
        unknown: list[UnknownItem] = []
        notes: list[str] = []
        record = view.record
        hook = view.hook_input or {}
        tool = hook.get("tool_name") or record.tool_name
        tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
        cwd = self.cwd.get(view.agent_id, self.start_cwd)
        pids = _agent_pids(view.snapshot)
        new_pids = {pid: cmd for pid, cmd in pids.items() if pid not in (self.previous_pids or {})}
        self._learn_text(view)
        if view.hook_input is None:
            unknown.append(UnknownItem(check="silent_no_write", reason="no_hook_input"))
            self._remember(view, test=False, failed=False)
            return flags, held, unknown, notes
        code, how = _exit_code(hook, record)
        output = _output(hook)
        crashes = crash_lines(output)
        crash = crashes[0] if crashes else None
        command = str(tool_input.get("command") or "") if tool == "Bash" else ""
        script: Script | None = None
        targets: list[Target] = []
        invocations: tuple[Invocation, ...] = ()
        background = bool(tool_input.get("run_in_background")) or how == "background"
        if tool == "Bash":
            script, reading = read_command(command, cwd)
            targets = reading.targets
            invocations = tuple(reading.invocations)
            background = background or reading.background
            for text, reason in reading.unknown:
                unknown.append(
                    UnknownItem(check="silent_no_write", reason=reason, detail=text[:200])
                )
            targets = targets + self._promised(reading, targets, unknown, notes)
        elif tool in WRITE_TOOLS or tool in NOTEBOOK_TOOLS:
            key = WRITE_TOOLS.get(tool) or NOTEBOOK_TOOLS[tool]
            raw = str(tool_input.get(key) or "")
            path = resolve(raw, cwd) if raw else None
            if path is None:
                unknown.append(UnknownItem(check="silent_no_write", reason="no_path", detail=raw))
            else:
                targets = [Target(path=path, how=tool, any_of=(path,))]

        # self_kill
        if script is not None and code in SELF_KILL_EXITS:
            kill = find_self_kill(script, command)
            if kill is not None:
                flags.append(
                    self._flag(
                        view,
                        now,
                        "self_kill",
                        kill["form"],
                        {**kill, "exit_code": code},
                        f"Your `{kill['tool']} -f {kill['pattern']}` also matched this "
                        "command's own shell, so the command was killed and nothing after it "
                        "ran.",
                    )
                )

        # masked_error, leaving out the answers to import probes
        if code == 0 and crashes and tool == "Bash":
            probes = probe_modules(script) if script is not None else set()
            shown = [line for line in crashes if not _probe_answer(line, probes)]
            if len(shown) < len(crashes):
                notes.append(f"import_probe: {', '.join(sorted(probes))}")
            if shown:
                flags.append(
                    self._flag(
                        view,
                        now,
                        "masked_error",
                        None,
                        {"error_line": shown[0][:300], "exit_code": 0, "command": command[:300]},
                        f"This command exited 0, but its output shows an error: {shown[0][:200]}",
                    )
                )

        # silent_no_write
        self._writes(
            view, now, targets, code, how, crash, background, new_pids, flags, held, unknown, notes
        )

        test = bool(command) and TEST_COMMAND.search(command) is not None
        failed = code != 0 or _TEST_FAILED.search(output) is not None or crash is not None
        self._remember(view, test=test, failed=failed, invocations=invocations)
        if hook.get("cwd"):
            self.cwd[view.agent_id] = str(hook["cwd"])
        return flags, held, unknown, notes

    def _learn_text(self, view: CallView) -> None:
        """Keep the text of `.py` and `.json` files from the recorded diffs, for the scripts a
        call runs and the JSON a promise writes. A version the diffs cannot rebuild is unknown."""
        if view.changes is None:
            return
        for entry in view.changes.files:
            if not entry.path.endswith(_TEXT_KEPT):
                continue
            if entry.op == "removed":
                self.text.pop(entry.path, None)
            elif entry.kind != "file" or entry.diff is None:
                self.text[entry.path] = None
            else:
                old = [] if entry.op == "added" else self.text.get(entry.path)
                self.text[entry.path] = apply_diff(old, entry.diff)

    def _promised(
        self,
        reading: ShellReading,
        shell_targets: list[Target],
        unknown: list[UnknownItem],
        notes: list[str],
    ) -> list[Target]:
        """Writes promised in the Python code a call runs: `-c`, a heredoc, or a script whose
        text the records hold (written by this call or an earlier one)."""
        taken = {t.path for t in shell_targets}
        found: list[Target] = []
        for invocation in reading.invocations:
            if not _PYTHON.match(invocation.name):
                continue
            sources = [("heredoc or -c", code) for code in invocation.code]
            if not invocation.code and invocation.operands and invocation.operands[0] != "-":
                script = resolve(invocation.operands[0], invocation.cwd)
                text = self.text.get(script) if script else None
                if text is None:
                    notes.append(
                        f"promised: script text unknown {script or invocation.operands[0]}"
                    )
                else:
                    sources.append((f"script {script}", "\n".join(text)))
            for source, code in sources:
                promises, parsed = python_promises(code, invocation.cwd)
                if not parsed:
                    unknown.append(
                        UnknownItem(
                            check="silent_no_write", reason="promised_unparsable", detail=source
                        )
                    )
                for promise in promises:
                    if promise.path is None:
                        unknown.append(
                            UnknownItem(
                                check="silent_no_write",
                                reason="promised_variable",
                                detail=promise.line[:200],
                            )
                        )
                        continue
                    if promise.path in taken or promise.path.startswith(_NOT_FILES):
                        continue
                    taken.add(promise.path)
                    gone = any(
                        later > invocation.index
                        and (promise.path == path or promise.path.startswith(path + "/"))
                        for later, path in reading.removals
                    )
                    found.append(
                        Target(
                            path=promise.path,
                            how=f"promised {promise.how}",
                            any_of=(promise.path,),
                            removed_later=gone,
                            promise={
                                "source": source,
                                "line": promise.line,
                                "context": list(promise.context),
                            },
                        )
                    )
        return found

    def _writes(  # type: ignore[no-untyped-def]
        self,
        view,
        now,
        targets,
        code,
        how,
        crash,
        background,
        new_pids,
        flags,
        held,
        unknown,
        notes,
    ) -> None:
        record = view.record
        changes = view.changes
        by_path: dict[str, FileChange] = {f.path: f for f in changes.files} if changes else {}
        changed = set(by_path) | {p[1:] for p in record.changed_paths}
        incomplete = None
        if record.change == "baseline" or (changes is not None and changes.baseline):
            incomplete = "baseline"
        elif changes is not None and changes.files_total > len(changes.files):
            incomplete = "capped"
        elif record.changed_paths_total is not None and record.changed_paths_total > len(
            record.changed_paths
        ):
            incomplete = "capped"
        elif changes is None and record.changed_paths_total is None:
            incomplete = "no_record"
        stale = self._stale(view)
        for target in targets:
            path = target.path
            if code is None and how != "background" or (code is not None and code != 0):
                kind = "visible_failure" if record.tool_failed else how
                notes.append(f"{kind}: {target.how} {path}")
                continue
            if target.removed_later:
                notes.append(f"removed_later: {target.how} {path}")
                continue
            if excluded(path):
                unknown.append(UnknownItem(check="silent_no_write", reason="excluded", path=path))
                continue
            hit = self._hit(target, by_path, changed)
            if hit is not None:
                entry = by_path.get(hit)
                if entry is not None and entry.sha256 and entry.sha256 == entry.before_sha256:
                    if hit in stale:
                        unknown.append(
                            UnknownItem(check="silent_no_write", reason="stale_before", path=hit)
                        )
                    else:
                        notes.append(f"identical: {target.how} {hit}")
                else:
                    notes.append(f"wrote: {target.how} {hit}")
                    if target.promise is not None and entry is not None:
                        self._promised_content(view, now, target, entry, flags, unknown)
                continue
            if incomplete is not None:
                unknown.append(UnknownItem(check="silent_no_write", reason=incomplete, path=path))
                continue
            if background and new_pids:
                pid, cmd = sorted(new_pids.items())[0]
                held.append(HeldWrite(path=path, pid=pid, cmd=cmd[:300]))
                if target.promise is not None:
                    notes.append(f"held: {target.how} {path}")
                continue
            if how == "background":
                unknown.append(
                    UnknownItem(
                        check="silent_no_write", reason="background_no_new_process", path=path
                    )
                )
                continue
            if target.promise is not None and crash:
                notes.append(f"promised_crash: {target.how} {path}")
                continue
            subtype = "promised" if target.promise is not None else "crash" if crash else "quiet"
            evidence: dict[str, Any] = {
                "path": path,
                "how": target.how,
                "checked_paths": list(target.any_of),
                "exit_code": code,
                "files_total": changes.files_total if changes else None,
                "changed_paths_total": record.changed_paths_total,
            }
            if crash:
                evidence["error_line"] = crash[:300]
            if target.promise is not None:
                evidence["promise"] = dict(target.promise)
            if view.hook_input and (view.hook_input.get("tool_input") or {}).get("command"):
                evidence["command"] = str(view.hook_input["tool_input"]["command"])[:300]
            flags.append(
                self._flag(
                    view,
                    now,
                    "silent_no_write",
                    subtype,
                    evidence,
                    f"`{path}` didn't change on this call, although the command looked like it "
                    "wrote it.",
                )
            )

    def _promised_content(self, view, now, target, entry, flags, unknown) -> None:  # type: ignore[no-untyped-def]
        """A promised file that did change: `empty` at 0 bytes, `broken` for JSON that does
        not parse."""
        if entry.op == "removed" or entry.kind != "file":
            return
        evidence = {"path": entry.path, "how": target.how, "size": entry.size,
                    "promise": dict(target.promise)}  # fmt: skip
        if entry.size == 0:
            flags.append(
                self._flag(
                    view,
                    now,
                    "silent_no_write",
                    "empty",
                    evidence,
                    f"`{entry.path}` was written on this call, but it's empty.",
                )
            )
            return
        if not entry.path.endswith(".json"):
            return
        text = self.text.get(entry.path)
        if text is None:
            unknown.append(
                UnknownItem(check="silent_no_write", reason="promised_no_text", path=entry.path)
            )
            return
        try:
            json.loads("\n".join(text))
        except ValueError as error:
            flags.append(
                self._flag(
                    view,
                    now,
                    "silent_no_write",
                    "broken",
                    {**evidence, "json_error": str(error)[:200]},
                    f"`{entry.path}` was written on this call, but it isn't valid JSON.",
                )
            )

    @staticmethod
    def _hit(target: Target, by_path: Mapping[str, FileChange], changed: set[str]) -> str | None:
        for path in target.any_of:
            if path in by_path or path in changed:
                return path
            if target.tree:
                prefix = path.rstrip("/") + "/"
                inside = next((p for p in sorted(changed) if p.startswith(prefix)), None)
                if inside is not None:
                    return inside
        return None

    def _stale(self, view: CallView) -> set[str]:
        """Paths whose before-hash on this call is the hash read before an unread change, so
        it is against an older version (fixed after the off corpus; ADR-0015 §2)."""
        stale: set[str] = set()
        changes = view.changes
        if changes is None:
            return stale
        for entry in changes.files:
            if (
                entry.before_sha256
                and entry.path in self.unread_since
                and self.read_hash.get(entry.path) == entry.before_sha256
            ):
                stale.add(entry.path)
        return stale

    def _remember(
        self,
        view: CallView,
        *,
        test: bool,
        failed: bool,
        invocations: tuple[Invocation, ...] = (),
    ) -> None:
        record = view.record
        changes = view.changes
        if changes is not None:
            for entry in changes.files:
                if entry.sha256:
                    self.read_hash[entry.path] = entry.sha256
                    self.unread_since.pop(entry.path, None)
                elif entry.op != "removed":
                    self.unread_since[entry.path] = record.call_seq
        if view.snapshot is not None:
            self.previous_pids = _agent_pids(view.snapshot)
        listed = tuple(f.path for f in changes.files) if changes else ()
        own = tuple(p[1:] for p in record.changed_paths)
        work = tuple(p for p in (listed or own) if not _CACHE_PATH.search(p))
        self.seen.append(
            _Seen(
                seq=record.call_seq,
                tool_call_id=record.tool_call_id,
                test=test,
                failed=failed,
                changed=work,
                changed_total=record.changed_paths_total if work or not (listed or own) else 0,
                invocations=invocations,
            )
        )

    # -- the stop ------------------------------------------------------------------------------

    def _stop(self, view: CallView, now: datetime):  # type: ignore[no-untyped-def]
        flags: list[FlagRecord] = []
        unknown: list[UnknownItem] = []
        notes: list[str] = []
        facts = self.stop_facts() if callable(self.stop_facts) else self.stop_facts
        self._unverified(view, now, facts, flags, unknown, notes)
        if facts is None:
            unknown.append(UnknownItem(check="missing_output", reason="no_stop_facts"))
        else:
            for path in facts.paths:
                start, stop = facts.at_start.get(path), facts.at_stop.get(path)
                if start is True:
                    notes.append(f"missing_output: {path} existed at the start, not an output")
                elif start is None or stop is None:
                    unknown.append(
                        UnknownItem(
                            check="missing_output",
                            reason="unreadable_start" if start is None else "unreadable_stop",
                            path=path,
                        )
                    )
                elif stop is False:
                    flags.append(
                        self._flag(
                            view,
                            now,
                            "missing_output",
                            None,
                            {"path": path, "at_start": False, "at_stop": False},
                            f"The task asks for `{path}`, and it doesn't exist.",
                        )
                    )
        self.seen.append(
            _Seen(view.record.call_seq, view.record.tool_call_id, False, False, (), None)
        )
        return flags, [], unknown, notes

    def check_rules(self, facts: StopFacts | None) -> dict[str, Any]:
        """The ways this trial's checks are recognized: the shared test commands always, the
        instruction's commands and provided scripts, and the task-agnostic rule if on."""
        return {
            "instruction_commands": [
                " ".join([c.name, *c.operands]) for c in (facts.commands if facts else ())
            ],
            "instruction_scripts": list(facts.scripts) if facts else [],
            "uses_changed_file": self.uses_changed_file,
        }

    def _check_kind(
        self, seen: _Seen, last: _Seen, facts: StopFacts | None
    ) -> tuple[str, str] | None:
        """(rule, detail) if `seen` checks the work of the last change, else None. The named
        checks count from the last change's own call on; the task-agnostic rule only after it."""
        if seen.seq >= last.seq:
            if seen.test:
                return "test_command", ""
            for invocation in seen.invocations:
                for wanted in facts.commands if facts else ():
                    if runs_command(invocation, wanted):
                        return "instruction_command", " ".join([wanted.name, *wanted.operands])
                script = uses(invocation, facts.scripts) if facts else None
                if script is not None:
                    return "instruction_script", script
        if self.uses_changed_file and seen.seq > last.seq:
            for invocation in seen.invocations:
                path = uses(invocation, last.changed)
                if path is not None:
                    return "uses_changed_file", path
        return None

    def _unverified(self, view, now, facts, flags, unknown, notes) -> None:  # type: ignore[no-untyped-def]
        calls = self.seen
        changing = [s for s in calls if s.changed_total]
        rules = self.check_rules(facts)
        if not changing:
            notes.append("unverified_completion: no file-changing call")
            return
        named = rules["instruction_commands"] or rules["instruction_scripts"]
        if not named and not self.uses_changed_file:
            # The shared test list alone does not define a check for a task: these runs never
            # use it, so the flag would track the task, not the agent.
            if facts is None:
                unknown.append(UnknownItem(check="unverified_completion", reason="no_stop_facts"))
            else:
                notes.append("unverified_completion: no check defined")
            return
        last = changing[-1]
        path = last.changed[0] if last.changed else "?"
        checks = [(s, k) for s in calls if (k := self._check_kind(s, last, facts)) is not None]
        evidence: dict[str, Any] = {
            "last_change_call": last.seq,
            "last_change_tool_call_id": last.tool_call_id,
            "last_change_path": path,
            "last_change_paths": list(last.changed[:20]),
            "rules": rules,
        }
        if not checks:
            flags.append(
                self._flag(
                    view,
                    now,
                    "unverified_completion",
                    "no_test",
                    evidence,
                    f"You're stopping, but no test ran after your last change ({path}, "
                    f"call {last.seq}).",
                )
            )
            return
        final, (kind, detail) = checks[-1]
        notes.append(f"unverified_completion: last check call {final.seq} ({kind} {detail})")
        if final.failed:
            flags.append(
                self._flag(
                    view,
                    now,
                    "unverified_completion",
                    "last_test_failed",
                    {
                        **evidence,
                        "last_test_call": final.seq,
                        "last_test_tool_call_id": final.tool_call_id,
                        "last_test_rule": kind,
                        "last_test_detail": detail,
                    },
                    f"You're stopping, but your last test (call {final.seq}) failed.",
                )
            )


def instruction_paths(instruction: str) -> list[str]:
    """Absolute file paths named in an instruction: no directories (a path ending in `/` may be
    needed only in some cases) and no URL paths. The start image decides what each is: absent
    there, an output; present, an input or a provided script (ADR-0023)."""
    found: list[str] = []
    for match in _INSTRUCTION_PATH.finditer(instruction):
        path = match.group(0).rstrip(".,:;")
        if path.endswith("/") or path in found or not path.strip("/") or "//" in path:
            continue
        found.append(path)
    return found


# -- replay ------------------------------------------------------------------------------------


def _first_lines[M: BaseModel](path: Path, model: type[M]) -> dict[str, M]:
    """A JSONL file's first valid record per `tool_call_id`, as `trajlab trace` reads it."""
    out: dict[str, M] = {}
    if not path.is_file():
        return out
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = model.model_validate_json(line)
        except ValidationError:
            logger.warning("%s:%d: not a %s; skipped", path, number, model.__name__)
            continue
        out.setdefault(record.tool_call_id, record)  # type: ignore[attr-defined]
    return out


@dataclass
class TrialRecords:
    trial_dir: Path
    views: list[CallView]
    checkpoints: dict[int, CheckpointRecord]
    timed_out: list[str]
    start_cwd: str | None
    instruction: str | None


def read_trial(trial_dir: Path) -> TrialRecords:
    """A finished trial's records, one `CallView` per answered call in `call_seq` order."""
    paths = TrialPaths(trial_dir)
    checkpoints_dir = paths.agent_dir / CHECKPOINTS_DIRNAME
    extras = trial_dir / TRAJLAB_DIRNAME
    calls = sorted(
        _first_lines(checkpoints_dir / CALLS_FILENAME, CallRecord).values(),
        key=lambda c: c.call_seq,
    )
    changes = _first_lines(extras / CHANGES_FILENAME, CallChanges)
    snapshots = _first_lines(extras / PROCESSES_FILENAME, ProcessSnapshot)
    checkpoints: dict[int, CheckpointRecord] = {}
    path = checkpoints_dir / CHECKPOINT_RECORDS_FILENAME
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                record = CheckpointRecord.model_validate_json(line)
                checkpoints.setdefault(record.seq, record)
    views = []
    for call in calls:
        hook = _json(checkpoints_dir / f"{call.tool_call_id}{CALL_INPUT_SUFFIX}")
        request = _json(checkpoints_dir / f"{call.tool_call_id}.req") or {}
        views.append(
            CallView(
                record=call,
                hook_input=hook,
                agent_id=request.get("agent_id") or None,
                changes=changes.get(call.tool_call_id),
                snapshot=snapshots.get(call.tool_call_id),
            )
        )
    timed_out = sorted(
        p.name.removesuffix(TIMEOUT_SUFFIX) for p in checkpoints_dir.glob(f"*{TIMEOUT_SUFFIX}")
    )
    return TrialRecords(
        trial_dir=trial_dir,
        views=views,
        checkpoints=checkpoints,
        timed_out=timed_out,
        start_cwd=stream_start_cwd(paths.agent_dir / "claude-code.txt"),
        instruction=trajectory_instruction(paths.agent_dir / "trajectory.json"),
    )


def _json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stream_start_cwd(stream: Path) -> str | None:
    """The agent's working directory from the `system`/`init` event of Claude Code's
    stream-json output, written before any call."""
    try:
        with stream.open() as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    return event.get("cwd")
    except OSError:
        return None
    return None


def trajectory_instruction(path: Path) -> str | None:
    """The first user step's message: the task instruction (read with Harbor's models)."""
    if not path.is_file():
        return None
    trajectory = Trajectory.model_validate_json(path.read_bytes())
    for step in trajectory.steps:
        if step.source == "user":
            return step.message if isinstance(step.message, str) else str(step.message)
    return None


PathProbe = Callable[[str, Sequence[str]], dict[str, bool | None]]
_PROBE = 'for p in "$@"; do if [ -e "$p" ]; then echo "1 $p"; else echo "0 $p"; fi; done'


def docker_path_probe(
    docker: str = "docker",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: float = 120,
) -> PathProbe:
    """Whether paths exist in an image, by a short read-only container with no network."""

    def probe(image: str, paths: Sequence[str]) -> dict[str, bool | None]:
        if not paths:
            return {}
        argv = [docker, "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image,
                "-c", _PROBE, "sh", *paths]  # fmt: skip
        try:
            done = runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return dict.fromkeys(paths)
        found: dict[str, bool | None] = dict.fromkeys(paths)
        if done.returncode == 0:
            for line in done.stdout.splitlines():
                flag, _, path = line.partition(" ")
                if path in found:
                    found[path] = flag == "1"
        return found

    return probe


def read_stop_facts(
    instruction: str, probe: PathProbe, *, start_image: str | None, stop_image: str | None
) -> StopFacts:
    """The stop checks' facts, the same way in a replay and live: the instruction's paths, read
    in the start image and, for those absent there, in the stop's checkpoint image."""
    paths = tuple(instruction_paths(instruction))
    at_start = probe(start_image, paths) if start_image else dict.fromkeys(paths)
    candidates = tuple(p for p in paths if at_start.get(p) is False)
    at_stop = probe(stop_image, candidates) if stop_image else dict.fromkeys(candidates)
    return StopFacts(
        paths=paths,
        at_start=at_start,
        at_stop=at_stop,
        commands=tuple(instruction_commands(instruction)),
    )


def task_instruction(trial_dir: Path) -> str | None:
    """The instruction Harbor gives the agent, read live from the trial's `config.json` and
    Harbor's cached task (`Task.instruction`, canary lines stripped). None if unreadable."""
    try:
        config = TrialConfig.model_validate_json(TrialPaths(trial_dir).config_path.read_text())
        return Task(
            config.task.get_local_path(), extra_instruction_paths=config.extra_instruction_paths
        ).instruction
    except (OSError, ValueError):
        logger.warning("no instruction for %s", trial_dir, exc_info=True)
        return None


def replay(
    records: TrialRecords,
    *,
    start_image: str | None = None,
    probe: PathProbe | None = None,
    limit: int | None = None,
    uses_changed_file: bool = True,
) -> tuple[list[FlagRecord], list[CheckRecord]]:
    """Run the checker over a finished trial's calls, one at a time, as the watcher would.

    `limit` stops after that many calls (the prefix check). A flag's `flagged_at` and a check's
    `checked_at` are the call's `answered_at`, so a replay is reproducible.
    """

    def stop_facts(stop: CallRecord) -> StopFacts | None:
        if probe is None or records.instruction is None:
            return None
        checkpoint = records.checkpoints.get(stop.checkpoint_seq or 0)
        return read_stop_facts(
            records.instruction,
            probe,
            start_image=start_image,
            stop_image=checkpoint.checkpoint_id if checkpoint is not None else None,
        )

    checker = Checker(start_cwd=records.start_cwd, uses_changed_file=uses_changed_file)
    flags: list[FlagRecord] = []
    checks: list[CheckRecord] = []
    for view in records.views[:limit]:
        if view.record.trigger == "stop":
            checker.stop_facts = lambda stop=view.record: stop_facts(stop)
        found, check = checker.check(view, view.record.answered_at)
        flags.extend(found)
        checks.append(check)
    return flags, checks


def write_jsonl(path: Path, records: Iterable[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.model_dump_json() + "\n" for r in records))
