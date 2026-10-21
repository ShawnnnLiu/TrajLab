"""One trial's trace for the `trajlab trace` site: what the agent said, ran, and saw, next to
what each call changed, joined by `tool_call_id` (ADR-0015).

Sources, all read-only:

- Harbor's ATIF trajectory (`agent/trajectory.json`): the agent's messages, its tool calls, and
  the outputs as the agent saw them (`transcript.seen_output`). A trial that is still running,
  or was killed, has none; its calls then come from the hook's `<tool_call_id>.call.json`
  files, without messages.
- The watcher's records under `agent/checkpoints/`: each hooked call's `CallRecord` (whether it
  changed the filesystem, and which paths) and `CheckpointRecord`.
- With `trajlab watch --extras`, `<trial>/trajlab/`: each call's `CallChanges` (hashes and
  diffs) and `ProcessSnapshot`.

`build_trace` returns facts from those files, plus a few cross-references under each event's
`hints`: the same command as an earlier call, a file back to an earlier version, the calls that
changed a path a message names, and, for a success claim, the last test-like command before it.
They resemble the detector's rules, so the site hides them unless asked (ADR-0015 §8: labels
are made blind).
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from harbor.models.trajectories import Trajectory
from harbor.models.trial.paths import TrialPaths
from pydantic import BaseModel, ValidationError

from trajlab.capture.discover import iter_trial_dirs
from trajlab.capture.transcript import as_text, seen_output, shorten
from trajlab.contracts import (
    CALL_INPUT_SUFFIX,
    CALLS_FILENAME,
    CHANGES_FILENAME,
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    POLICY_FILENAME,
    PROCESSES_FILENAME,
    TIMEOUT_SUFFIX,
    TRAJLAB_DIRNAME,
    CallChanges,
    CallRecord,
    CheckpointPolicy,
    CheckpointRecord,
    ProcessSnapshot,
)

logger = logging.getLogger(__name__)

# Harbor's name for the ATIF file (trajlab.atif.load, which capture/ does not import).
TRAJECTORY_FILENAME = "trajectory.json"
# The tools the checkpoint hook matches (configs/claude-code/settings.hooks.json).
HOOKED_TOOLS = frozenset({"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"})
# How much of each text the page gets: a tool output keeps its head and tail.
OUTPUT_HEAD, OUTPUT_TAIL = 12_000, 6_000
ARGUMENT_CHARS = 20_000
DIFF_CHARS = 100_000
SUMMARY_CHARS = 160
MAX_MENTIONS = 20
# Statuses of calls known to have changed the filesystem (a baseline call's change is unknown).
CHANGING = ("changed", "checkpoint")

# Harbor adds `[exit_code] N` when Claude Code reports one; Claude Code itself starts a failed
# Bash call's output with `Exit code N` (harbor/agents/installed/claude_code.py).
_EXIT_CODE = re.compile(r"^\[exit_code\] (-?\d+)$", re.MULTILINE)
_EXIT_LINE = re.compile(r"Exit code (-?\d+)\b")
_TOOL_ERROR = "[error] tool reported failure"
# A path in a message: absolute, relative with a slash, or a bare file name with a common
# extension (so `Node.js` or `e.g.` are not taken for files).
_EXTENSIONS = (
    "py|pyi|ipynb|js|mjs|cjs|ts|tsx|jsx|json|jsonl|yaml|yml|toml|ini|cfg|conf|env|md|rst|txt"
    "|csv|tsv|log|sh|bash|zsh|c|h|cc|cpp|cxx|hpp|rs|go|java|kt|scala|rb|php|pl|lua|r|jl|swift"
    "|html|htm|css|scss|xml|sql|db|sqlite|proto|lock|mk|cmake|gradle|bas|cls|frm|vba|xlsm|xlsx"
    "|docx|pdf|png|jpg|jpeg|svg|gif|step|stp|stl|dxf|obj|parquet|npy|npz|pt|pth|bin|tar|gz|zip"
)
_PATH = re.compile(
    r"(?<![\w/.@+:-])(?:/[\w.@+-]+)+/?"
    r"|(?<![\w/.@+:-])[\w.@+-]+(?:/[\w.@+-]+)+/?"
    rf"|(?<![\w/.@+:-])[\w-][\w.-]*\.(?:{_EXTENSIONS})\b",
    re.IGNORECASE,
)
# Commands that run tests or checks, for the success-claim hint.
_TEST_COMMAND = re.compile(
    r"\b(?:pytest|unittest|tox|nox|ctest|jest|vitest|mocha)\b"
    r"|\b(?:npm|yarn|pnpm)(?: run)? test\b|\b(?:cargo|go) test\b"
    r"|\bmake(?: -C \S+)? (?:test|check)\b|\btest\.sh\b|\brun_tests\b"
)
_SUCCESS_CLAIM = re.compile(
    r"\b(?:all (?:\d+ )?tests?|tests? (?:now )?(?:pass|passes|passed|passing)|verified"
    r"|works?|working|succeeded|successfully|fixed|done|complete[ds]?)\b",
    re.IGNORECASE,
)
# Processes of the harness, not the agent's: Claude Code and Harbor's launcher (whose command
# lines name claude), the checkpoint hook (whose script names itself) and its wait loop, and
# the container's keep-alive.
_HARNESS_PROCESS = re.compile(r"claude|trajlab hook|^sleep (?:0\.2|1|infinity)$")

Offset = Callable[[datetime | str | None], float | None]


def trial_dirs(job_dir: Path) -> list[Path]:
    """A job dir's trial dirs. Job dirs have a `config.json` too, so a trial dir is also
    required to have `agent/`; that keeps a jobs root from passing for a job."""
    return [d for d in iter_trial_dirs(job_dir) if TrialPaths(d).agent_dir.is_dir()]


def find_jobs(root: Path) -> dict[str, Path]:
    """The job dirs under `root`, by name: `root` itself if it holds trial dirs, else each
    subdirectory that does."""
    if trial_dirs(root):
        return {root.name: root}
    return {
        child.name: child
        for child in sorted(root.iterdir())
        if child.is_dir() and trial_dirs(child)
    }


def _read_jsonl[M: BaseModel](path: Path, model: type[M]) -> list[M]:
    """The valid records of a JSONL file; any other line is logged and skipped."""
    if not path.is_file():
        return []
    records = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValidationError:
            logger.warning("%s:%d: not a %s; skipped", path, number, model.__name__)
    return records


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def _time(stamp: datetime | str | None) -> datetime | None:
    if stamp is None or isinstance(stamp, datetime):
        return stamp
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutes(execution: dict[str, Any]) -> float | None:
    start, end = _time(execution.get("started_at")), _time(execution.get("finished_at"))
    return (end - start).total_seconds() / 60 if start and end else None


def _clip(value: Any) -> Any:
    """A tool-call argument with every long string cut to ARGUMENT_CHARS characters."""
    if isinstance(value, str) and len(value) > ARGUMENT_CHARS:
        cut = len(value) - ARGUMENT_CHARS
        return f"{value[:ARGUMENT_CHARS]}\n[... {cut:,} characters not shown ...]"
    if isinstance(value, dict):
        return {key: _clip(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip(item) for item in value]
    return value


def trial_summary(trial_dir: Path) -> dict[str, Any]:
    """A trial's row on the site's job page, from small files only (no trajectory parse)."""
    paths = TrialPaths(trial_dir)
    result = _read_json(paths.result_path)
    checkpoints_dir = paths.agent_dir / CHECKPOINTS_DIRNAME
    calls = _lines(checkpoints_dir / CALLS_FILENAME)
    stops = sum('"trigger":"stop"' in line for line in calls)
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    timeouts = (
        len(list(checkpoints_dir.glob(f"*{TIMEOUT_SUFFIX}"))) if checkpoints_dir.is_dir() else 0
    )
    return {
        "name": trial_dir.name,
        "task": result.get("task_name") or trial_dir.name.rsplit("__", 1)[0],
        "finished": paths.result_path.is_file(),
        "died": not paths.result_path.is_file() and paths.exception_message_path.is_file(),
        "has_trajectory": (paths.agent_dir / TRAJECTORY_FILENAME).is_file(),
        "reward": rewards.get("reward"),
        "exception": (result.get("exception_info") or {}).get("exception_type"),
        "minutes": _minutes(result.get("agent_execution") or {}),
        "cost_usd": (result.get("agent_result") or {}).get("cost_usd"),
        "hooked_calls": len(calls) - stops,
        "stops": stops,
        "checkpoints": len(_lines(checkpoints_dir / CHECKPOINT_RECORDS_FILENAME)),
        "timeouts": timeouts,
        "extras": (trial_dir / TRAJLAB_DIRNAME / CHANGES_FILENAME).is_file(),
    }


def job_summary(job_dir: Path) -> dict[str, int]:
    """A job's counts for the site's experiments page; reads only each trial's result.json."""
    counts = {"trials": 0, "finished": 0, "passed": 0, "extras": 0, "captured": 0}
    for trial in trial_dirs(job_dir):
        paths = TrialPaths(trial)
        calls = paths.agent_dir / CHECKPOINTS_DIRNAME / CALLS_FILENAME
        counts["trials"] += 1
        counts["extras"] += (trial / TRAJLAB_DIRNAME / CHANGES_FILENAME).is_file()
        counts["captured"] += calls.is_file() and calls.stat().st_size > 0
        if paths.result_path.is_file():
            counts["finished"] += 1
            result = _read_json(paths.result_path)
            rewards = (result.get("verifier_result") or {}).get("rewards") or {}
            counts["passed"] += rewards.get("reward") == 1
    return counts


def _summary(tool: str, arguments: dict[str, Any]) -> str:
    """One line naming what a call did, for lists and exports."""
    if tool == "Bash":
        text = str(arguments.get("command", ""))
    else:
        text = str(
            arguments.get("file_path")
            or arguments.get("notebook_path")
            or arguments.get("pattern")
            or arguments.get("path")
            or arguments.get("url")
            or arguments.get("description")
            or (json.dumps(arguments, ensure_ascii=False, default=str) if arguments else "")
        )
    line = " ".join(text.split())
    return line if len(line) <= SUMMARY_CHARS else line[: SUMMARY_CHARS - 1] + "…"


def _command_key(tool: str, arguments: dict[str, Any]) -> str:
    """What makes two calls the same command: a Bash command up to whitespace, else all args."""
    if tool == "Bash":
        return "Bash " + " ".join(str(arguments.get("command", "")).split())
    return f"{tool} " + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def _status(record: CallRecord | None, hooked: bool, timed_out: bool, captured: bool) -> str:
    if record is None:
        if not hooked:
            return "not_hooked"
        if timed_out:
            return "timeout"
        return "missing" if captured else "uncaptured"
    if record.outcome == "unchanged":
        return "unchanged"
    if record.outcome == "deferred":
        return "deferred"
    return record.change if record.change in ("baseline", "changed") else "checkpoint"


def _exit_code(
    tool: str, arguments: dict[str, Any], harbor_content: str, output: str, failed: bool | None
) -> int | None:
    """A Bash call's exit code: Harbor's `[exit_code]` line, else a failed call's `Exit code N`.
    Claude Code fails every Bash call that exits non-zero, so one that did not fail exited 0,
    unless it went to the background. `failed` is None when the call has no result."""
    match = _EXIT_CODE.search(harbor_content) or _EXIT_LINE.match(output)
    if match:
        return int(match.group(1))
    if tool == "Bash" and failed is False and not arguments.get("run_in_background"):
        return 0
    return None


def _call_event(
    *,
    tool: str,
    call_id: str,
    arguments: Any,
    output: str,
    harbor_content: str,
    failed: bool | None,
    t: float | None,
    step_id: int | None,
) -> dict[str, Any]:
    arguments = arguments if isinstance(arguments, dict) else {"arguments": arguments}
    text, cut = shorten(output, OUTPUT_HEAD, OUTPUT_TAIL)
    return {
        "kind": "call",
        "id": call_id,
        "tool": tool,
        "input": _clip(arguments),
        "summary": _summary(tool, arguments),
        "output": text,
        "output_cut": cut,
        "exit_code": _exit_code(tool, arguments, harbor_content, output, failed),
        "is_error": bool(failed),
        "t": t,
        "step_id": step_id,
        "key": _command_key(tool, arguments),
    }


def _trajectory_events(trajectory: Trajectory, offset: Offset) -> list[dict[str, Any]]:
    """Messages and calls in the trajectory's order; the first user step is the instruction."""
    events: list[dict[str, Any]] = []
    first_user = next((s.step_id for s in trajectory.steps if s.source == "user"), None)
    for step in trajectory.steps:
        text = as_text(step.message).strip()
        if step.source == "user":
            if step.step_id != first_user and text:
                events.append({"kind": "user", "text": text, "t": offset(step.timestamp)})
            continue
        if step.source != "agent":
            continue
        reasoning = (step.reasoning_content or "").strip()
        if text or reasoning:
            events.append(
                {
                    "kind": "message",
                    "text": text,
                    "reasoning": reasoning,
                    "t": offset(step.timestamp),
                    "step_id": step.step_id,
                }
            )
        results = step.observation.results if step.observation else []
        outputs = {r.source_call_id: r for r in results}
        for call in step.tool_calls or []:
            result = outputs.get(call.tool_call_id)
            content = as_text(result.content) if result else ""
            events.append(
                _call_event(
                    tool=call.function_name,
                    call_id=call.tool_call_id,
                    arguments=call.arguments,
                    output=seen_output(result) if result else "",
                    harbor_content=content,
                    failed=(_TOOL_ERROR in content) if result else None,
                    t=offset(step.timestamp),
                    step_id=step.step_id,
                )
            )
    return events


def _response_text(response: Any) -> str:
    """A hook input's `tool_response` as text: a Bash call's stdout and stderr, else JSON."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict) and ("stdout" in response or "stderr" in response):
        stdout, stderr = str(response.get("stdout") or ""), str(response.get("stderr") or "")
        return stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
    if isinstance(response, dict):
        response = {k: v for k, v in response.items() if k not in ("originalFile", "content")}
    return json.dumps(response, ensure_ascii=False, indent=1, default=str) if response else ""


def _hook_events(
    checkpoints_dir: Path, calls: list[CallRecord], timed_out: set[str], offset: Offset
) -> list[dict[str, Any]]:
    """Calls from the hook's own files, for a trial without a trajectory: each answered call in
    `call_seq` order, then each call that timed out, with input and output from `.call.json`."""
    found: list[tuple[str, str, datetime | None]] = [
        (c.tool_call_id, c.tool_name, c.requested_at)
        for c in sorted(calls, key=lambda c: c.call_seq)
        if c.trigger == "tool_call"
    ]
    answered = {c.tool_call_id for c in calls}
    for call_id in sorted(timed_out - answered):
        request = checkpoints_dir / f"{call_id}.req"
        stamp = (
            datetime.fromtimestamp(request.stat().st_mtime).astimezone()
            if request.is_file()
            else None
        )
        found.append((call_id, _read_json(request).get("tool_name") or "?", stamp))
    events = []
    for call_id, tool, stamp in found:
        hook_input = _read_json(checkpoints_dir / f"{call_id}{CALL_INPUT_SUFFIX}")
        response = hook_input.get("tool_response", hook_input.get("error", ""))
        event = hook_input.get("hook_event_name")
        events.append(
            _call_event(
                tool=hook_input.get("tool_name") or tool,
                call_id=call_id,
                arguments=hook_input.get("tool_input") or {},
                output=_response_text(response),
                harbor_content="",
                failed=None if event is None else event == "PostToolUseFailure",
                t=offset(stamp),
                step_id=None,
            )
        )
    return events


def _insert_by_time(events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    """Put `event` before the first event that happened later; at the end without a time."""
    at = len(events)
    if event["t"] is not None:
        at = next(
            (i for i, e in enumerate(events) if e.get("t") is not None and e["t"] > event["t"]),
            len(events),
        )
    events.insert(at, event)


def _file_payload(change: CallChanges) -> dict[str, Any]:
    """A call's CallChanges for the page, each diff cut to DIFF_CHARS characters."""
    data = change.model_dump(mode="json")
    for entry in data["files"]:
        diff = entry.get("diff")
        if diff and len(diff) > DIFF_CHARS:
            cut = len(diff) - DIFF_CHARS
            entry["diff"] = f"{diff[:DIFF_CHARS]}\n[... {cut:,} characters not shown ...]"
    return data


def _process_delta(
    previous: ProcessSnapshot | None, current: ProcessSnapshot, harness_ports: set[int]
) -> dict[str, Any]:
    """The agent's processes started and stopped since the previous snapshot, by command line
    (a restarted server is a new pid with the same command), and the ports opened or closed;
    harness processes and ports are left out."""

    def commands(snapshot: ProcessSnapshot | None) -> list[str]:
        if snapshot is None:
            return []
        return [p.cmd for p in snapshot.processes if not _HARNESS_PROCESS.search(p.cmd)]

    def ports(snapshot: ProcessSnapshot | None) -> set[tuple[str, int]]:
        if snapshot is None:
            return set()
        return {(p.proto, p.port) for p in snapshot.listening if p.port not in harness_ports}

    def minus(left: list[str], right: list[str]) -> list[str]:
        rest = list(left)
        for cmd in right:
            if cmd in rest:
                rest.remove(cmd)
        return rest

    def listed(found: set[tuple[str, int]]) -> list[dict[str, Any]]:
        return [
            {"proto": proto, "port": port} for proto, port in sorted(found, key=lambda p: p[::-1])
        ]

    before, after = commands(previous), commands(current)
    return {
        "started": minus(after, before),
        "stopped": minus(before, after),
        "opened": listed(ports(current) - ports(previous)),
        "closed": listed(ports(previous) - ports(current)),
        "running": after,
        "listening": listed(ports(current)),
    }


def _known_names(events: list[dict[str, Any]]) -> set[str]:
    """The file names the trial's calls touch or name: their path arguments, the paths in Bash
    commands, and every changed path."""
    names: set[str] = set()
    for event in events:
        if event["kind"] != "call":
            continue
        arguments = event["input"]
        paths = [arguments.get(k) for k in ("file_path", "notebook_path", "path")]
        if event["tool"] == "Bash":
            paths += [m.group(0) for m in _PATH.finditer(str(arguments.get("command", "")))]
        paths += [f["path"] for f in (event["changes"] or {}).get("files", [])]
        paths += [p["path"] for p in event["paths"]]
        names.update(p.rstrip("/").rsplit("/", 1)[-1] for p in paths if isinstance(p, str))
    return names


def _mentions(text: str, changed: dict[str, list[int]], known: set[str]) -> list[dict[str, Any]]:
    """The paths a message names, each with the calls that changed a matching path. A bare file
    name counts if the trial's calls name it or it starts in lower case (`Node.js` does not)."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _PATH.finditer(text):
        token = match.group(0).rstrip(".,:;!?)'\"`")
        if token in seen or len(token) < 3 or "//" in token:
            continue
        if "/" not in token and token not in known and token[0].isupper():
            continue
        seen.add(token)
        bare = token.rstrip("/")
        if token.startswith("/"):
            paths = [p for p in changed if p == bare or p.startswith(bare + "/")]
        else:
            paths = [p for p in changed if p.endswith("/" + bare) or f"/{bare}/" in p]
        calls = sorted({n for path in paths for n in changed[path]})
        found.append(
            {
                "token": token,
                "paths": sorted(paths)[:5],
                "more": max(0, len(paths) - 5),
                "calls": calls,
            }
        )
        if len(found) == MAX_MENTIONS:
            break
    return found


def _trial_info(trial_dir: Path, trajectory: Trajectory | None) -> dict[str, Any]:
    paths = TrialPaths(trial_dir)
    result = _read_json(paths.result_path)
    exception = result.get("exception_info") or {}
    message = str(exception.get("exception_message") or "")
    if not message and paths.exception_message_path.is_file():
        message = paths.exception_message_path.read_text(errors="replace")
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    agent_info = result.get("agent_info") or {}
    instruction = ""
    if trajectory is not None:
        instruction = next((as_text(s.message) for s in trajectory.steps if s.source == "user"), "")
    model = (agent_info.get("model_info") or {}).get("name")
    version = agent_info.get("version")
    if trajectory is not None:
        model = model or trajectory.agent.model_name
        version = version or trajectory.agent.version
    return {
        "name": trial_dir.name,
        "task": result.get("task_name") or trial_dir.name.rsplit("__", 1)[0],
        "finished": paths.result_path.is_file(),
        "died": not paths.result_path.is_file() and paths.exception_message_path.is_file(),
        "reward": rewards.get("reward"),
        "exception": exception.get("exception_type"),
        "exception_message": message[-2000:],
        "minutes": _minutes(result.get("agent_execution") or {}),
        "cost_usd": (result.get("agent_result") or {}).get("cost_usd"),
        "model": model,
        "agent_version": version,
        "instruction": instruction,
    }


def _checks(trial_dir: Path) -> dict[str, Any] | None:
    """The verifier's tests from `verifier/ctrf.json`."""
    ctrf = _read_json(TrialPaths(trial_dir).verifier_dir / "ctrf.json")
    tests = (ctrf.get("results") or {}).get("tests") or []
    if not tests:
        return None
    failed = [
        {
            "name": str(t.get("name")),
            "message": str(t.get("message") or t.get("trace") or "")[:2000],
        }
        for t in tests
        if t.get("status") == "failed"
    ]
    passed = sum(1 for t in tests if t.get("status") == "passed")
    return {"total": len(tests), "passed": passed, "failed": failed[:200]}


def _add_capture_facts(
    events: list[dict[str, Any]],
    record_of: dict[str, CallRecord],
    checkpoint_of: dict[int, CheckpointRecord],
    changes: dict[str, CallChanges],
    snapshots: dict[str, ProcessSnapshot],
    timed_out: set[str],
    captured: bool,
) -> set[int]:
    """Number the calls and give each call and stop the watcher's facts; returns the harness
    ports (those listening at the trial's first process snapshot)."""
    number = 0
    previous: ProcessSnapshot | None = None
    harness_ports: set[int] | None = None
    for event in events:
        if event["kind"] not in ("call", "stop"):
            continue
        record = record_of.get(event["id"])
        if event["kind"] == "call":
            number += 1
            event["n"] = number
        event["hooked"] = record is not None or event["tool"] in HOOKED_TOOLS
        event["status"] = _status(record, event["hooked"], event["id"] in timed_out, captured)
        event["record"] = record.model_dump(mode="json") if record else None
        snapshot_record = (
            checkpoint_of.get(record.checkpoint_seq)
            if record and record.outcome == "checkpoint" and record.checkpoint_seq
            else None
        )
        event["checkpoint"] = (
            snapshot_record.model_dump(mode="json", include={"seq", "capture_ms", "bytes"})
            if snapshot_record
            else None
        )
        event["pause_ms"] = (
            round((record.answered_at - record.requested_at).total_seconds() * 1000)
            if record
            else None
        )
        call_changes = changes.get(event["id"])
        event["changes"] = _file_payload(call_changes) if call_changes else None
        ops = {"+": "added", "-": "removed"}
        event["paths"] = (
            [{"path": p[1:], "op": ops.get(p[0], "modified")} for p in record.changed_paths]
            if record and call_changes is None
            else []
        )
        event["paths_total"] = record.changed_paths_total if record else None
        snapshot = snapshots.get(event["id"])
        event["processes"] = None
        if snapshot is not None:
            if harness_ports is None:
                harness_ports = {p.port for p in snapshot.listening}
            event["processes"] = _process_delta(previous, snapshot, harness_ports)
            previous = snapshot
        event["hints"] = {}
    return harness_ports or set()


def _add_call_hints(events: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, list[int]]]:
    """Each file's versions with reverts, and repeated commands; returns the file histories
    and, per path, the calls that changed it."""
    files: dict[str, list[dict[str, Any]]] = {}
    changed: dict[str, list[int]] = {}
    keys: dict[str, list[int]] = {}
    changing_calls: list[int] = []
    for index, event in enumerate(events):
        if event["kind"] not in ("call", "stop"):
            continue
        label = event.get("n")
        entries = (
            event["changes"]["files"]
            if event["changes"]
            else [{"path": p["path"], "op": p["op"]} for p in event["paths"]]
        )
        reverts = []
        for entry in entries:
            versions = files.setdefault(entry["path"], [])
            new_hash = entry.get("sha256")
            if new_hash and versions:
                original = versions[0].get("before_sha256")
                last = versions[-1].get("sha256")
                earlier = [v for v in versions[:-1] if v.get("sha256") == new_hash]
                if original and new_hash == original and last != original:
                    reverts.append({"path": entry["path"], "to": "original"})
                elif earlier and last != new_hash:
                    reverts.append({"path": entry["path"], "to": earlier[-1]["call"]})
            versions.append(
                {
                    "call": label,
                    "id": event["id"],
                    "event": index,
                    "op": entry["op"],
                    "kind": entry.get("kind"),
                    "sha256": new_hash,
                    "before_sha256": entry.get("before_sha256"),
                    "size": entry.get("size"),
                    "skipped": entry.get("skipped"),
                    "has_diff": bool(entry.get("diff")),
                }
            )
            if label is not None:
                changed.setdefault(entry["path"], []).append(label)
        if reverts:
            event["hints"]["reverts"] = reverts
        if event["kind"] == "call":
            earlier_calls = keys.setdefault(event["key"], [])
            if earlier_calls:
                last_n = earlier_calls[-1]
                event["hints"]["repeat"] = {
                    "of": last_n,
                    "count": len(earlier_calls) + 1,
                    "changed_between": any(last_n < n < event["n"] for n in changing_calls),
                }
            earlier_calls.append(event["n"])
            command = str(event["input"].get("command", "")) if event["tool"] == "Bash" else ""
            event["test_like"] = bool(_TEST_COMMAND.search(command))
            if event["status"] in CHANGING:
                changing_calls.append(event["n"])
        del event["key"]
    return files, changed


def _add_message_hints(events: list[dict[str, Any]], changed: dict[str, list[int]]) -> None:
    """The paths each message names, and for a success claim the last test-like call before."""
    known = _known_names(events)
    calls_seen: list[dict[str, Any]] = []
    for event in events:
        if event["kind"] == "call":
            calls_seen.append(event)
            continue
        if event["kind"] != "message":
            continue
        event["after_call"] = calls_seen[-1]["n"] if calls_seen else 0
        event["hints"] = {}
        if not event["text"]:
            continue
        mentions = _mentions(event["text"], changed, known)
        if mentions:
            event["hints"]["mentions"] = mentions
        if _SUCCESS_CLAIM.search(event["text"]):
            tests = [c for c in calls_seen if c.get("test_like")]
            last = tests[-1] if tests else None
            event["hints"]["claim"] = {
                "test_call": last["n"] if last else None,
                "test_command": last["summary"] if last else None,
                "test_exit_code": last["exit_code"] if last else None,
                "changed_since": [
                    c["n"]
                    for c in calls_seen
                    if c["status"] in CHANGING and (last is None or c["n"] > last["n"])
                ],
            }


def _add_timing(
    events: list[dict[str, Any]],
    trajectory: Trajectory | None,
    record_of: dict[str, CallRecord],
    offset: Offset,
) -> dict[str, Any]:
    """Each agent step's tokens and cost, and about how long the model took: from when the
    previous step's tools finished (its last hook answer, else the previous step) to this step.
    Each call gets `tool_s`, about how long its tool ran: from its step to the hook's request.
    Returns the steps by id."""
    steps: dict[str, Any] = {}
    if trajectory is None:
        return steps
    calls_of: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        if event["kind"] == "call" and event.get("step_id") is not None:
            calls_of.setdefault(event["step_id"], []).append(event)
    resume: datetime | None = None
    for step in trajectory.steps:
        moment = _time(step.timestamp)
        model_s = None
        if step.source == "agent" and moment and resume:
            model_s = round(max(0.0, (moment - resume).total_seconds()), 1)
        metrics = step.metrics
        steps[str(step.step_id)] = {
            "t": offset(step.timestamp),
            "model_s": model_s,
            "prompt_tokens": metrics.prompt_tokens if metrics else None,
            "completion_tokens": metrics.completion_tokens if metrics else None,
            "cached_tokens": metrics.cached_tokens if metrics else None,
            "cost_usd": metrics.cost_usd if metrics else None,
        }
        answers = []
        for event in calls_of.get(step.step_id, []):
            record = record_of.get(event["id"])
            if record is not None and moment is not None:
                event["tool_s"] = round(max(0.0, (record.requested_at - moment).total_seconds()), 1)
                answers.append(record.answered_at)
        resume = max(answers) if answers else (moment or resume)
    return steps


def _add_states(events: list[dict[str, Any]]) -> None:
    """Each call's and stop's `state_of`: the checkpoint that holds the filesystem after it. An
    unhooked call changes nothing, so it shares the state after the hooked call before it."""
    seq = None
    for event in events:
        if event["kind"] not in ("call", "stop"):
            continue
        record = event["record"]
        if record is not None and record.get("checkpoint_seq") is not None:
            seq = record["checkpoint_seq"]
        event["state_of"] = seq


def _argument_lines(arguments: Any) -> list[str]:
    """A tool call's arguments, one per line. A multi-line string (a file's content, an edit, a
    script) is printed as its own lines between markers, exactly as written."""
    if not isinstance(arguments, dict):
        return [json.dumps(arguments, ensure_ascii=False, indent=2, default=str)]
    lines = []
    for key, value in arguments.items():
        if isinstance(value, str) and "\n" in value:
            lines += [f"----- {key} -----", value, f"----- end of {key} -----"]
        elif isinstance(value, str | int | float | bool) or value is None:
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
    return lines


def full_transcript(trial_dir: Path, numbers: dict[str, int] | None = None) -> str:
    """The whole run as plain text, in order and with nothing cut: the task instruction, then
    each step's thinking, message, tool calls with their full arguments, and each output as the
    agent saw it. `numbers` maps tool_call_id to the call numbers the site shows. Raises
    FileNotFoundError when the trial has no trajectory."""
    trajectory = Trajectory.model_validate_json(
        (TrialPaths(trial_dir).agent_dir / TRAJECTORY_FILENAME).read_bytes()
    )
    info = _trial_info(trial_dir, trajectory)
    numbers = numbers or {}
    started = next((t for t in (_time(s.timestamp) for s in trajectory.steps) if t), None)
    lines = [
        f"Trial:  {info['name']}",
        f"Task:   {info['task']}",
        f"Model:  {info['model']}, Claude Code {info['agent_version']}",
        f"Reward: {'none' if info['reward'] is None else info['reward']}",
        "The whole run from Harbor's agent/trajectory.json, nothing cut. Tool outputs are the",
        "tool results the agent read.",
        "",
    ]
    for step in trajectory.steps:
        moment = _time(step.timestamp)
        when = ""
        if moment and started:
            seconds = int((moment - started).total_seconds())
            when = f" · +{seconds // 60}:{seconds % 60:02d}"
        lines.append(f"==== step {step.step_id} · {step.source}{when} ====")
        if (step.reasoning_content or "").strip():
            lines += ["[thinking]", step.reasoning_content.strip(), ""]
        text = as_text(step.message).strip()
        if text:
            lines += ["[message]", text, ""]
        results = {
            r.source_call_id: r for r in (step.observation.results if step.observation else [])
        }
        for call in step.tool_calls or []:
            label = f"#{numbers[call.tool_call_id]} " if call.tool_call_id in numbers else ""
            result = results.pop(call.tool_call_id, None)
            lines.append(f"[tool call {label}{call.function_name} · {call.tool_call_id}]")
            lines += _argument_lines(call.arguments)
            lines += [f"[output of {label}{call.function_name}]"]
            lines += [seen_output(result) if result else "(no output recorded)", ""]
        for result in results.values():
            lines += [f"[output · {result.source_call_id}]", seen_output(result), ""]
    return "\n".join(lines) + "\n"


def build_trace(trial_dir: Path) -> dict[str, Any]:
    """Everything the site shows for one trial."""
    paths = TrialPaths(trial_dir)
    checkpoints_dir = paths.agent_dir / CHECKPOINTS_DIRNAME
    extras_dir = trial_dir / TRAJLAB_DIRNAME
    calls = _read_jsonl(checkpoints_dir / CALLS_FILENAME, CallRecord)
    checkpoints = _read_jsonl(checkpoints_dir / CHECKPOINT_RECORDS_FILENAME, CheckpointRecord)
    changes = {c.tool_call_id: c for c in _read_jsonl(extras_dir / CHANGES_FILENAME, CallChanges)}
    snapshots = {
        p.tool_call_id: p for p in _read_jsonl(extras_dir / PROCESSES_FILENAME, ProcessSnapshot)
    }
    timed_out = (
        {p.name.removesuffix(TIMEOUT_SUFFIX) for p in checkpoints_dir.glob(f"*{TIMEOUT_SUFFIX}")}
        if checkpoints_dir.is_dir()
        else set()
    )
    policy_path = checkpoints_dir / POLICY_FILENAME
    policy = (
        CheckpointPolicy.model_validate_json(policy_path.read_text()).model_dump(mode="json")
        if policy_path.is_file()
        else None
    )
    record_of = {c.tool_call_id: c for c in calls}
    captured = bool(calls or checkpoints or timed_out)

    trajectory_file = paths.agent_dir / TRAJECTORY_FILENAME
    trajectory = (
        Trajectory.model_validate_json(trajectory_file.read_bytes())
        if trajectory_file.is_file()
        else None
    )
    started = _time((_read_json(paths.result_path).get("agent_execution") or {}).get("started_at"))
    if started is None and trajectory is not None:
        started = next((t for t in (_time(s.timestamp) for s in trajectory.steps) if t), None)
    if started is None and calls:
        started = min(c.requested_at for c in calls)

    def offset(stamp: datetime | str | None) -> float | None:
        moment = _time(stamp)
        return round((moment - started).total_seconds(), 1) if moment and started else None

    if trajectory is not None:
        events = _trajectory_events(trajectory, offset)
    else:
        events = _hook_events(checkpoints_dir, calls, timed_out, offset)
    # Records with no call among the events: stops (ADR-0011), and calls the trajectory lacks.
    present = {e["id"] for e in events if e["kind"] == "call"}
    for record in sorted(calls, key=lambda c: c.call_seq):
        if record.tool_call_id in present:
            continue
        stop = record.trigger == "stop"
        _insert_by_time(
            events,
            {
                "kind": "stop" if stop else "call",
                "id": record.tool_call_id,
                "tool": record.tool_name,
                "input": {},
                "summary": "end of the agent's turn" if stop else "(not in the trajectory)",
                "output": "",
                "output_cut": False,
                "exit_code": None,
                "is_error": record.tool_failed,
                "t": offset(record.requested_at),
                "step_id": None,
                "key": f"stop {record.tool_call_id}" if stop else f"record {record.tool_call_id}",
                "unmatched": not stop,
            },
        )

    harness_ports = _add_capture_facts(
        events,
        record_of,
        {c.seq: c for c in checkpoints},
        changes,
        snapshots,
        timed_out,
        captured,
    )
    files, changed = _add_call_hints(events)
    _add_message_hints(events, changed)
    steps = _add_timing(events, trajectory, record_of, offset)
    _add_states(events)
    final = trajectory.final_metrics if trajectory is not None else None
    timing = {
        "model_s": round(sum(s["model_s"] or 0 for s in steps.values()), 1),
        "tool_s": round(sum(e.get("tool_s") or 0 for e in events if e["kind"] == "call"), 1),
        "pause_s": round(sum(e.get("pause_ms") or 0 for e in events if "pause_ms" in e) / 1000, 1),
        "prompt_tokens": final.total_prompt_tokens if final else None,
        "completion_tokens": final.total_completion_tokens if final else None,
        "cached_tokens": final.total_cached_tokens if final else None,
        "cost_usd": final.total_cost_usd if final else None,
    }

    counts: dict[str, int] = {}
    for event in events:
        if event["kind"] == "call":
            counts[event["status"]] = counts.get(event["status"], 0) + 1
    return {
        "trial": _trial_info(trial_dir, trajectory),
        "checks": _checks(trial_dir),
        "source": "trajectory" if trajectory is not None else "hook_files",
        "capture": {
            "captured": captured,
            "extras": bool(changes or snapshots),
            "policy": policy,
            "hooked_calls": sum(1 for c in calls if c.trigger == "tool_call"),
            "stops": sum(1 for c in calls if c.trigger == "stop"),
            "checkpoints": len(checkpoints),
            "timeouts": len(timed_out),
            "harness_ports": sorted(harness_ports),
            "counts": counts,
        },
        "events": events,
        "files": files,
        "steps": steps,
        "timing": timing,
        "checkpoint_images": {str(c.seq): c.checkpoint_id for c in checkpoints},
    }
