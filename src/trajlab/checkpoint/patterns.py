"""The checker: rule-based failure-pattern flags for Claude Code runs (ADR-0015 §3, v0 ADR-0022).

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
- `masked_error`: the call exits 0 but its output shows a crash.
- at the stop, `unverified_completion` (no test after the last change, or the last test
  failed) and `missing_output` (a path the instruction asks for is absent).

Left out of v0, with ADR-0022's reasons: `loop` and `stall` (the off corpus's six loops were
deliberate undos); `false_edit_claim` (2 of 104 missing writes were claims in the agent's own
words); `false_test_claim` (covered for now by `unverified_completion`); `server_state` (agents
kill and restart their own servers on purpose, so it needs labels first); `forbidden_action`
(test edits are sometimes asked for, which needs task-specific rules). ADR-0020's attribution
of late writes uses later calls, so the checker cannot use it.

`checkpoint/` imports other trajlab packages only through `contracts/` (CLAUDE.md
constraint 3).
"""

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

from harbor.models.trajectories import Trajectory
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
# (ADR-0022). "command not found" and "No such file or directory" are left out: in those runs
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
# The checker's test commands: the shared list, plus what the off runs' agents ran to check
# their work. The additions after the first line are task-specific (ADR-0022).
TEST_COMMAND_V0 = re.compile(
    TEST_COMMAND_PATTERN
    + r"|\bmake(?: -C \S+)? repro\b|\bpython3? -m unittest\b|(?:^|[\s/])test\w*\.py\b"
    + r"|\bplaywright\b|\bcurl\b[^\n|;]*\b(?:localhost|127\.0\.0\.1)\b"
    + r"|\brender\.py\b|\bimport render\b|\bfrom render import\b|\brun_pretrain\.sh\b"
    + r"|\bapply\.py\b|\bfrom apply import\b|\bscore\.(?:sh|py)\b"
)
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


def crash_line(output: str) -> str | None:
    """The first line that shows a crash, or None. A traceback whose only exception is a
    `BrokenPipeError` (output piped into `head`) is not a crash."""
    if _TRACEBACK.search(output):
        lines = list(_EXCEPTION_LINE.finditer(output))
        real = next((m for m in lines if m.group(1) != "BrokenPipeError"), None)
        if real is not None:
            return output[real.start() :].split("\n", 1)[0].strip()
        if not lines:
            return "Traceback (most recent call last)"
    match = _CRASH_LINES.search(output)
    return match.group(0).strip() if match else None


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


@dataclass
class ShellReading:
    targets: list[Target] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)  # (written text, reason)
    background: bool = False  # `&` or `nohup` at some command
    cwd_known: bool = True


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
    for index, simple in enumerate(script.commands):
        first_target = len(reading.targets)
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
    return script, reading


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
    """For `missing_output`: the paths the instruction asks the agent to create, and whether
    each exists in the start image and at the stop (None: could not be read)."""

    outputs: tuple[str, ...] = ()
    at_start: Mapping[str, bool | None] = field(default_factory=dict)
    at_stop: Mapping[str, bool | None] = field(default_factory=dict)


@dataclass
class _Seen:
    """What the checker keeps of an earlier call."""

    seq: int
    tool_call_id: str
    test: bool
    test_failed: bool
    changed: tuple[str, ...]
    changed_total: int | None


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
    ) -> None:
        self.start_cwd = start_cwd
        self.stop_facts = stop_facts
        self.seen: list[_Seen] = []
        self.cwd: dict[str | None, str | None] = {}
        self.previous_pids: dict[int, str] | None = None
        # For before-hashes against an older version (the off corpus's PR #32 fault):
        # the last hash read per path, and whether a call changed it unread since.
        self.read_hash: dict[str, str] = {}
        self.unread_since: dict[str, int] = {}

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
        if view.hook_input is None:
            unknown.append(UnknownItem(check="silent_no_write", reason="no_hook_input"))
            self._remember(view, test=False, test_failed=False)
            return flags, held, unknown, notes
        code, how = _exit_code(hook, record)
        output = _output(hook)
        crash = crash_line(output)
        command = str(tool_input.get("command") or "") if tool == "Bash" else ""
        script: Script | None = None
        targets: list[Target] = []
        background = bool(tool_input.get("run_in_background")) or how == "background"
        if tool == "Bash":
            script, reading = read_command(command, cwd)
            targets = reading.targets
            background = background or reading.background
            for text, reason in reading.unknown:
                unknown.append(
                    UnknownItem(check="silent_no_write", reason=reason, detail=text[:200])
                )
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

        # masked_error
        if code == 0 and crash is not None and tool == "Bash":
            flags.append(
                self._flag(
                    view,
                    now,
                    "masked_error",
                    None,
                    {"error_line": crash[:300], "exit_code": 0, "command": command[:300]},
                    f"This command exited 0, but its output shows an error: {crash[:200]}",
                )
            )

        # silent_no_write
        self._writes(
            view, now, targets, code, how, crash, background, new_pids, flags, held, unknown, notes
        )

        test = bool(command) and TEST_COMMAND_V0.search(command) is not None
        test_failed = test and (
            code != 0 or _TEST_FAILED.search(output) is not None or crash is not None
        )
        self._remember(view, test=test, test_failed=test_failed)
        if hook.get("cwd"):
            self.cwd[view.agent_id] = str(hook["cwd"])
        return flags, held, unknown, notes

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
                continue
            if incomplete is not None:
                unknown.append(UnknownItem(check="silent_no_write", reason=incomplete, path=path))
                continue
            if background and new_pids:
                pid, cmd = sorted(new_pids.items())[0]
                held.append(HeldWrite(path=path, pid=pid, cmd=cmd[:300]))
                continue
            if how == "background":
                unknown.append(
                    UnknownItem(
                        check="silent_no_write", reason="background_no_new_process", path=path
                    )
                )
                continue
            subtype = "crash" if crash else "quiet"
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

    def _remember(self, view: CallView, *, test: bool, test_failed: bool) -> None:
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
                test_failed=test_failed,
                changed=work,
                changed_total=record.changed_paths_total if work or not (listed or own) else 0,
            )
        )

    # -- the stop ------------------------------------------------------------------------------

    def _stop(self, view: CallView, now: datetime):  # type: ignore[no-untyped-def]
        flags: list[FlagRecord] = []
        unknown: list[UnknownItem] = []
        notes: list[str] = []
        calls = [s for s in self.seen]
        changing = [s for s in calls if s.changed_total]
        tests = [s for s in calls if s.test]
        if not changing:
            notes.append("unverified_completion: no file-changing call")
        else:
            last = changing[-1]
            after = [t for t in tests if t.seq >= last.seq]
            path = last.changed[0] if last.changed else "?"
            if not after:
                flags.append(
                    self._flag(
                        view,
                        now,
                        "unverified_completion",
                        "no_test",
                        {
                            "last_change_call": last.seq,
                            "last_change_tool_call_id": last.tool_call_id,
                            "last_change_path": path,
                            "last_test_call": tests[-1].seq if tests else None,
                        },
                        f"You're stopping, but no test ran after your last change ({path}, "
                        f"call {last.seq}).",
                    )
                )
            elif after[-1].test_failed:
                flags.append(
                    self._flag(
                        view,
                        now,
                        "unverified_completion",
                        "last_test_failed",
                        {
                            "last_change_call": last.seq,
                            "last_change_path": path,
                            "last_test_call": after[-1].seq,
                            "last_test_tool_call_id": after[-1].tool_call_id,
                        },
                        f"You're stopping, but your last test (call {after[-1].seq}) failed.",
                    )
                )
        facts = self.stop_facts() if callable(self.stop_facts) else self.stop_facts
        if facts is None:
            unknown.append(UnknownItem(check="missing_output", reason="no_stop_facts"))
        else:
            for path in facts.outputs:
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


def instruction_outputs(instruction: str) -> list[str]:
    """Absolute paths named in an instruction, as candidate outputs: no directories (a path
    ending in `/` may be needed only in some cases) and no URL paths. Which of them are outputs
    is decided by the start image: a path that exists there is an input (ADR-0022)."""
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
    docker: str = "docker", runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> PathProbe:
    """Whether paths exist in an image, by a short read-only container with no network."""

    def probe(image: str, paths: Sequence[str]) -> dict[str, bool | None]:
        if not paths:
            return {}
        argv = [docker, "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image,
                "-c", _PROBE, "sh", *paths]  # fmt: skip
        try:
            done = runner(argv, capture_output=True, text=True, timeout=120, check=False)
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


def replay(
    records: TrialRecords,
    *,
    start_image: str | None = None,
    probe: PathProbe | None = None,
    limit: int | None = None,
) -> tuple[list[FlagRecord], list[CheckRecord]]:
    """Run the checker over a finished trial's calls, one at a time, as the watcher would.

    `limit` stops after that many calls (the prefix check). A flag's `flagged_at` and a check's
    `checked_at` are the call's `answered_at`, so a replay is reproducible.
    """

    def stop_facts(stop: CallRecord) -> StopFacts | None:
        if probe is None or records.instruction is None:
            return None
        outputs = tuple(instruction_outputs(records.instruction))
        at_start = probe(start_image, outputs) if start_image else dict.fromkeys(outputs)
        candidates = tuple(p for p in outputs if at_start.get(p) is False)
        checkpoint = records.checkpoints.get(stop.checkpoint_seq or 0)
        at_stop = (
            probe(checkpoint.checkpoint_id, candidates)
            if checkpoint is not None
            else dict.fromkeys(candidates)
        )
        return StopFacts(outputs=outputs, at_start=at_start, at_stop=at_stop)

    checker = Checker(start_cwd=records.start_cwd)
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
