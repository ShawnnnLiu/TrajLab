"""Mechanical evidence that a retry used what only its root failure's record held.

Written 2026-11-06 to pick Exp 2 roots whose record holds information a fresh retry lacks
(regression, silent wrong edit, costly discovery, large rewrites). No Docker, no network,
read-only.

Reads a root failure's rows in a store (`trials`, `steps`, `deltas`, `job_notices` of
`record.duckdb`, opened read-only with DuckDB alone, so a store of any schema from 4 on) and the
root's trial dir, then each retry made from that root in any arm (its `result.json` and
`agent/trajectory.json`). Prints JSON lines: one `{"kind": "root", ...}` per root, then one
`{"kind": "attempt", ...}` per attempt.

Calls. A call's text is a Bash call's command, a Write call's file_path and content, an Edit
call's file_path, old_string, and new_string (MultiEdit: every edit), and the JSON of any other
tool's arguments. Its written text is the command, the content, or the new strings. Its result is
its observation content, with every string inside any JSON in it decoded and appended, so a hunk
returned inside a query's JSON line reads as lines. A record read is a call whose text names
/logs/agent/record or /logs/agent/bin/query. Calls are numbered from 1.

Facts are tokens of a call's text (FACT_PATTERNS): `path`, an absolute path of two or more parts
outside EXCLUDED_PREFIXES, not starting with a number and not ending in .diff (pieces of the
record bundle's paths after a shell variable); `file`, a bare file name with an extension in
FILE_EXTENSIONS; `flag`, a long option; `number`, an integer of 4 or more digits or a decimal
with 3 or more decimals.
Each fact an attempt uses gets one source, at the first call that uses it: `given` (its prompt
holds it), else the earliest earlier result that holds it, `record` (a record read) or `own`
(any other call), else `unsourced`. For every `record` fact: `root_first_call`, the root's first
call whose text or result holds it (of `root_calls`), and `others`, the first call at which each
other attempt of the same root used it (null if never), with that attempt's arm and reward.

Lines are lines of a call's written text, stripped, at least MIN_LINE characters long, with a
letter or digit. A line is from the record when an earlier record read returned it and neither
the prompt nor an earlier own result held it. Each such line is looked up in the root's hunks:
`start_file` (a " " or "-" line the root had not added: the file's text before the root changed
it, which a fresh environment also holds), `root_added` (a "+" line of one of the root's hunks),
and `root_removed` (a "+" line at some step and a "-" line of a later hunk of the same path:
content the root wrote, then took out), and in the other attempts' written text
(`others_wrote`, the arms and rewards of those that wrote it). The counts leave out
`start_file` lines.

Per root: tool calls, reward, exception type, its own test runs (calls whose result has a
pytest-style "N passed" or "N failed" count, the last of each in the result) with the best and
the last, background job notices, and every text file it changed in place at least twice
(in-place changes, largest size after, steps), outside CACHE_MARKERS.

Per attempt: arm, attempt_idx, label (the chain log's outcome, or the probe's variant), trial,
reward, exception type, agent seconds, tool calls, record reads, the first call that writes a
file outside /logs/agent (`first_write_call`, by WRITE_TOOLS or BASH_WRITE, an approximation),
fact counts by source and kind, the record facts, the lines from the record,
`record_facts_no_control` (the record facts no control attempt of the root used),
`record_lines_root_removed`, and `record_lines_no_control` (lines from the record that no
control attempt wrote).

    # chains of an Exp 2 run (tb40-exp2, on the capture host); every root when --root is left out
    uv run python scripts/2026-11-06_record_info_candidates.py chains \\
        --chains corpus/exp2/tb40-exp2 --jobs corpus/jobs \\
        --corpus-job corpus/jobs/tb40-sonnet-v4 --store corpus/stores/tb40-sonnet-v4 \\
        --root freecad-platform-drawing__ibz96hK --root pretrain-shard-corruption__R4QX4a7
    # probe attempts named by plan files (development Mac)
    uv run python scripts/2026-11-06_record_info_candidates.py plans \\
        corpus/exp2/probe-jobs/_plans/wide-mvcc20.jsonl --probe-jobs corpus/exp2/probe-jobs
"""

import argparse
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult

from trajlab.atif.load import load_trial_trajectory

log = logging.getLogger("record_info_candidates")

RECORD_MARKERS = ("/logs/agent/record", "/logs/agent/bin/query")
EXCLUDED_PREFIXES = (
    "/logs/",
    "/tmp/",
    "/proc/",
    "/dev/",
    "/sys/",
    "/bin/",
    "/usr/bin/",
    "/usr/local/bin/",
)
FILE_EXTENSIONS = (
    "py|json|jsonl|csv|tsv|txt|sh|yaml|yml|toml|cfg|ini|cc|cpp|h|hpp|c|rs|go|js|mjs|ts|md|xml|"
    "html|svg|png|log|FCStd|step|stp|stl|brep|dxf|pt|pth|bin|npy|npz|parquet|safetensors|gz|tar|"
    "zip|whl|so|vba|bas|frm|cls"
)
FACT_PATTERNS = {
    "path": re.compile(r"(?<![\w.:/~-])/(?:[\w.+@-]+/)+[\w.+@-]*[\w+@-]"),
    "file": re.compile(rf"(?<![\w./-])[\w-]+(?:\.[\w-]+)*\.(?:{FILE_EXTENSIONS})(?![\w.])"),
    "flag": re.compile(r"(?<![\w-])--[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*"),
    "number": re.compile(r"(?<![\w.])(?:\d+\.\d{3,}|\d{4,})(?![\w.])"),
}
MIN_LINE = 20
HAS_WORD = re.compile(r"[A-Za-z0-9]")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# A redirect or tee to a file outside /logs/agent (where the diagnosis goes), an in-place sed, a
# copy or move, or a Python write.
BASH_WRITE = re.compile(
    r"(?:^|[^0-9&>])>{1,2}\s*(?!&|/dev/null|/logs/agent/)[\w./~$\"'-]"
    r"|\btee\s+(?:-a\s+)?(?!/logs/agent/)\S|\bsed\s+-i|\bcp\s|\bmv\s"
    r"|\bopen\([^)]*,\s*['\"][wa]|\.write_text\(|\.write\("
)
TEST_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?)\b")
CACHE_MARKERS = ("__pycache__", "/.cache/", "/node_modules/", ".pyc", "/.git/")


@dataclass
class Call:
    function: str
    text: str
    written: str
    result: str
    is_record: bool


@dataclass
class Attempt:
    arm: str
    attempt_idx: int
    trial_dir: Path
    label: str | None = None  # the chain's outcome, or the probe's prompt variant
    prompt: str = ""
    calls: list[Call] = field(default_factory=list)
    uses: dict[str, tuple[int, str]] = field(default_factory=dict)  # fact -> (first call, kind)
    written: str = ""
    reward: float | None = None


# --- reading calls ------------------------------------------------------------------------------


def call_text(function: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """A call's text and its written text."""
    if function == "Bash":
        command = str(arguments.get("command", ""))
        return command, command
    if function == "Write":
        content = str(arguments.get("content", ""))
        return f"{arguments.get('file_path', '')}\n{content}", content
    if function == "Edit":
        new = str(arguments.get("new_string", ""))
        text = f"{arguments.get('file_path', '')}\n{arguments.get('old_string', '')}\n{new}"
        return text, new
    if function == "MultiEdit":
        edits = arguments.get("edits") or []
        new = "\n".join(str(e.get("new_string", "")) for e in edits)
        old = "\n".join(str(e.get("old_string", "")) for e in edits)
        return f"{arguments.get('file_path', '')}\n{old}\n{new}", new
    return json.dumps(arguments), ""


def _strings(value: Any, into: list[str]) -> None:
    if isinstance(value, str):
        into.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, into)
    elif isinstance(value, list):
        for item in value:
            _strings(item, into)


def decoded(text: str) -> str:
    """The text with every string inside any JSON line of it, or the whole of it, appended."""
    found: list[str] = []
    candidates = [text] + [line for line in text.splitlines() if line[:1] in "{["]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(value, str):
            _strings(value, found)
    return text if not found else text + "\n" + "\n".join(found)


def content_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps([part.model_dump(mode="json") for part in content])  # type: ignore[union-attr]


def is_record(text: str) -> bool:
    return any(marker in text for marker in RECORD_MARKERS)


def read_attempt(attempt: Attempt) -> None:
    trajectory = load_trial_trajectory(attempt.trial_dir)
    for step in trajectory.steps:
        if step.source == "user" and not attempt.prompt:
            message = step.message
            attempt.prompt = message if isinstance(message, str) else content_text(message)
        if step.source != "agent":
            continue
        results = {
            r.source_call_id: decoded(content_text(r.content))
            for r in (step.observation.results if step.observation else [])
        }
        for call in step.tool_calls or []:
            text, written = call_text(call.function_name, call.arguments)
            attempt.calls.append(
                Call(
                    call.function_name,
                    text,
                    written,
                    results.get(call.tool_call_id, ""),
                    is_record(text),
                )
            )


def facts(text: str) -> dict[str, str]:
    """Each fact token in a text, with its kind."""
    found: dict[str, str] = {}
    for kind, pattern in FACT_PATTERNS.items():
        for match in pattern.finditer(text):
            token = match.group(0)
            if kind == "path" and (
                token.startswith(EXCLUDED_PREFIXES)
                or token.count("/") < 2
                or token[1].isdigit()
                or token.endswith(".diff")
            ):
                continue
            found.setdefault(token, kind)
    return found


def lines(written: str) -> list[str]:
    out = []
    for line in written.splitlines():
        stripped = line.strip()
        if len(stripped) >= MIN_LINE and HAS_WORD.search(stripped):
            out.append(stripped)
    return out


def source_of(token: str, calls: list[Call], before: int, prompt: str) -> str:
    if token in prompt:
        return "given"
    for call in calls[:before]:
        if token in call.result:
            return "record" if call.is_record else "own"
    return "unsourced"


def first_use(calls: list[Call]) -> dict[str, tuple[int, str]]:
    """Each fact's first call (1-based) and kind."""
    seen: dict[str, tuple[int, str]] = {}
    for number, call in enumerate(calls, start=1):
        for token, kind in facts(call.text).items():
            seen.setdefault(token, (number, kind))
    return seen


def writes(call: Call) -> bool:
    if call.is_record:
        return False
    if call.function in WRITE_TOOLS:
        return not call.text.startswith("/logs/agent/")
    return call.function == "Bash" and BASH_WRITE.search(call.text) is not None


def first_write_call(calls: list[Call]) -> int | None:
    return next((number for number, call in enumerate(calls, start=1) if writes(call)), None)


# --- the root -----------------------------------------------------------------------------------


@dataclass
class Root:
    name: str
    trial_dir: Path
    calls: list[tuple[str, str]] = field(default_factory=list)  # (text, output)
    added: dict[str, list[tuple[int, str]]] = field(default_factory=dict)  # line -> (step, path)
    removed: set[str] = field(default_factory=set)
    start: set[str] = field(default_factory=set)  # lines of files before the root changed them
    summary: dict[str, Any] = field(default_factory=dict)


def read_root(store: Path, trial_dir: Path) -> Root:
    root = Root(trial_dir.name, trial_dir)
    with duckdb.connect(str(store / "record.duckdb"), read_only=True) as con:
        row = con.execute(
            "SELECT trial_id, reward FROM trials WHERE trial_name = ? AND attempt_idx = 0",
            [root.name],
        ).fetchone()
        if row is None:
            raise SystemExit(f"{root.name} is not a first attempt in {store}")
        trial_id, store_reward = row
        steps = con.execute(
            "SELECT step_idx, tool, args_json, output FROM steps "
            "WHERE trial_id = ? AND tool IS NOT NULL ORDER BY step_idx",
            [trial_id],
        ).fetchall()
        deltas = con.execute(
            "SELECT step_idx, path, op, hunk, bytes_after FROM deltas "
            "WHERE trial_id = ? ORDER BY step_idx, path",
            [trial_id],
        ).fetchall()
        notices = con.execute(
            "SELECT count(*) FROM job_notices WHERE trial_id = ?", [trial_id]
        ).fetchone()
    for _, tool, args_json, output in steps:
        arguments = json.loads(args_json) if args_json else {}
        text, _ = call_text(tool, arguments if isinstance(arguments, dict) else {})
        root.calls.append((text, decoded(output or "")))

    added_at: dict[tuple[str, str], int] = {}
    rewrites: dict[str, dict[str, Any]] = defaultdict(lambda: {"modifies": 0, "max_bytes": 0})
    for step_idx, path, op, hunk, bytes_after in deltas:
        if hunk is None or any(marker in path for marker in CACHE_MARKERS):
            continue
        if op == "modify":
            entry = rewrites[path]
            entry["modifies"] += 1
            entry["max_bytes"] = max(entry["max_bytes"], bytes_after or 0)
            entry.setdefault("steps", []).append(step_idx)
        for raw in hunk.splitlines():
            if raw.startswith(("+++", "---")):
                continue
            stripped = raw[1:].strip()
            if len(stripped) < MIN_LINE or not HAS_WORD.search(stripped):
                continue
            if raw.startswith("+"):
                root.added.setdefault(stripped, []).append((step_idx, path))
                added_at.setdefault((path, stripped), step_idx)
            elif raw.startswith(("-", " ")):
                if (path, stripped) not in added_at:
                    root.start.add(stripped)
                elif raw.startswith("-") and added_at[(path, stripped)] < step_idx:
                    root.removed.add(stripped)

    test_runs = []
    for number, (_, output) in enumerate(root.calls, start=1):
        counts: dict[str, int] = {}
        for value, word in TEST_COUNT.findall(output):
            counts["failed" if word != "passed" else "passed"] = int(value)
        if counts:
            test_runs.append({"call": number, **counts})
    reward, exception = store_reward, None
    if TrialPaths(trial_dir).result_path.is_file():
        reward, exception, _ = outcome_of(trial_dir)
    best = max(test_runs, key=lambda r: (r.get("passed", 0), -r.get("failed", 0)), default=None)
    root.summary = {
        "kind": "root",
        "root": root.name,
        "reward": reward,
        "exception": exception,
        "root_calls": len(root.calls),
        "test_runs": test_runs,
        "best_test_run": best,
        "last_test_run": test_runs[-1] if test_runs else None,
        "job_notices": notices[0] if notices else 0,
        "rewrites": {p: e for p, e in sorted(rewrites.items()) if e["modifies"] >= 2},
        "root_added_lines": len(root.added),
        "root_removed_lines": len(root.removed),
        "start_lines": len(root.start),
    }
    return root


def root_first_call(root: Root, token: str) -> int | None:
    for number, (text, output) in enumerate(root.calls, start=1):
        if token in text or token in output:
            return number
    return None


# --- attempts -----------------------------------------------------------------------------------


def outcome_of(trial_dir: Path) -> tuple[float | None, str | None, float | None]:
    result = TrialResult.model_validate_json(TrialPaths(trial_dir).result_path.read_text())
    reward = None
    if result.verifier_result is not None and result.verifier_result.rewards:
        reward = float(result.verifier_result.rewards.get("reward", 0.0))
    exception = result.exception_info.exception_type if result.exception_info else None
    seconds = None
    timing = result.agent_execution
    if timing and timing.started_at and timing.finished_at:
        seconds = round((timing.finished_at - timing.started_at).total_seconds(), 1)
    return reward, exception, seconds


def describe(root: Root, attempt: Attempt, others: list[Attempt]) -> dict[str, Any]:
    reward, exception, seconds = outcome_of(attempt.trial_dir)
    counts: dict[str, int] = defaultdict(int)
    record_facts = []
    for token, (number, kind) in attempt.uses.items():
        source = source_of(token, attempt.calls, number - 1, attempt.prompt)
        counts[f"{source}:{kind}"] += 1
        if source != "record":
            continue
        record_facts.append(
            {
                "fact": token,
                "kind": kind,
                "call": number,
                "root_first_call": root_first_call(root, token),
                "others": [
                    {
                        "arm": other.arm,
                        "attempt_idx": other.attempt_idx,
                        "reward": other.reward,
                        "first_call": other.uses.get(token, (None, ""))[0],
                    }
                    for other in others
                ],
            }
        )
    record_lines = []
    seen: set[str] = set()
    for number, call in enumerate(attempt.calls, start=1):
        if call.is_record:
            continue
        for line in lines(call.written):
            if line in seen:
                continue
            seen.add(line)
            if line in attempt.prompt:
                continue
            earlier = [c for c in attempt.calls[: number - 1] if line in c.result]
            if not earlier or not all(c.is_record for c in earlier):
                continue
            record_lines.append(
                {
                    "line": line[:200],
                    "call": number,
                    "start_file": line in root.start,
                    "root_added": line in root.added,
                    "root_removed": line in root.removed,
                    "others_wrote": [
                        f"{other.arm}/a{other.attempt_idx}/r{other.reward}"
                        for other in others
                        if line in other.written
                    ],
                }
            )
    controls = [other.uses for other in others if other.arm == "control"]
    return {
        "kind": "attempt",
        "root": root.name,
        "arm": attempt.arm,
        "attempt_idx": attempt.attempt_idx,
        "label": attempt.label,
        "trial": attempt.trial_dir.name,
        "reward": reward,
        "exception": exception,
        "agent_s": seconds,
        "tool_calls": len(attempt.calls),
        "record_reads": sum(c.is_record for c in attempt.calls),
        "first_write_call": first_write_call(attempt.calls),
        "fact_counts": dict(sorted(counts.items())),
        "record_facts_no_control": sorted(
            f["fact"]
            for f in record_facts
            if controls and all(f["fact"] not in u for u in controls)
        ),
        "record_lines": sum(not r["start_file"] for r in record_lines),
        "record_lines_root_removed": sum(
            r["root_removed"] and not r["start_file"] for r in record_lines
        ),
        "record_lines_no_control": sum(
            not r["start_file"] and not any(w.startswith("control/") for w in r["others_wrote"])
            for r in record_lines
        ),
        "record_facts": record_facts,
        "record_line_list": record_lines,
    }


def report(store: Path, root_dir: Path, attempts: list[Attempt]) -> None:
    root = read_root(store, root_dir)
    print(json.dumps(root.summary, default=str))
    usable = []
    for attempt in attempts:
        if not TrialPaths(attempt.trial_dir).result_path.is_file():
            log.warning("no result.json in %s", attempt.trial_dir)
            continue
        try:
            read_attempt(attempt)
        except (OSError, ValueError) as error:
            log.warning("cannot read %s: %s", attempt.trial_dir, error)
            continue
        attempt.reward = outcome_of(attempt.trial_dir)[0]
        attempt.uses = first_use(attempt.calls)
        attempt.written = "\n".join(call.written for call in attempt.calls)
        usable.append(attempt)
    for attempt in usable:
        others = [other for other in usable if other is not attempt]
        print(json.dumps(describe(root, attempt, others), default=str))
    log.info("%s: %d attempts", root.name, len(usable))


# --- inputs -------------------------------------------------------------------------------------


def chain_attempts(chains: Path, jobs: Path, root: str) -> list[Attempt]:
    found = []
    for arm_dir in sorted((chains / root).iterdir()):
        log_path = arm_dir / "attempts.jsonl"
        if not log_path.is_file():
            continue
        for line in log_path.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if not entry.get("trial_name"):
                continue
            trial_dir = jobs / entry["job_name"] / entry["trial_name"]
            found.append(
                Attempt(entry["arm"], entry["attempt_idx"], trial_dir, entry.get("outcome"))
            )
    return found


def run_chains(args: argparse.Namespace) -> None:
    roots = args.root or sorted(p.name for p in args.chains.iterdir() if p.is_dir())
    for root in roots:
        attempts = chain_attempts(args.chains, args.jobs, root)
        report(args.store, args.corpus_job / root, attempts)


def run_plans(args: argparse.Namespace) -> None:
    by_root: dict[tuple[str, str], list[Attempt]] = defaultdict(list)
    for plan_file in args.plan:
        for line in plan_file.read_text().splitlines():
            if not line.strip():
                continue
            plan = json.loads(line)
            job_dir = args.probe_jobs / plan["name"]
            trials = (
                sorted(p for p in job_dir.glob("*__*") if p.is_dir()) if job_dir.is_dir() else []
            )
            if not trials:
                log.warning("no trial dir for %s", plan["name"])
                continue
            by_root[(plan["store"], plan["root"])].append(
                Attempt(plan["arm"], 1, trials[-1], plan.get("variant"))
            )
    for (store, root), attempts in sorted(by_root.items()):
        report(Path(store), Path(root), attempts)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    chains = sub.add_parser("chains", help="Chains of an Exp 2 run.")
    chains.add_argument("--chains", type=Path, required=True, help="<root>/<run> of the chains.")
    chains.add_argument("--jobs", type=Path, required=True, help="Dir holding the retries' jobs.")
    chains.add_argument("--corpus-job", type=Path, required=True, help="The roots' job dir.")
    chains.add_argument("--store", type=Path, required=True, help="Store holding the roots.")
    chains.add_argument("--root", action="append", help="A root trial name (repeatable).")
    plans = sub.add_parser("plans", help="Probe attempts named by plan files.")
    plans.add_argument("plan", type=Path, nargs="+")
    plans.add_argument("--probe-jobs", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "chains":
        run_chains(args)
    else:
        run_plans(args)


if __name__ == "__main__":
    main()
