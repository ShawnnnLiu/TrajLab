"""Run one Exp 2 retry attempt by hand and report how its agent used the record.

Built to answer whether record-engine agents in `tb40-exp2` used the query tool little because
the engine is unhelpful or because the prompt never asks them to use it and never shows how.
It launches one attempt exactly as `trajlab.exp2.loop.Exp2Runner.launch` does (the same job
config from `trajlab.exp2.arms.job_config`, the same `RepairClaudeCode` note, the same in-process
query server), without the full run's gates: no preregistration, no write recorder (macOS has no
fanotify), no disk hold. A variant prompt replaces only the arm's note (`--variant`); the task
instruction is the root failure's own. The arms are record-engine, record-files (the chain's
bundle mounted read-only), and control.

Variants (`VARIANTS`; the run note `docs/experiments/exp2/2026-11-02_engine-uptake-probe.md`
describes each): `baseline` (the note `tb40-exp2` ran), `guided`, `guided2`, `guided3`, and
`guided4` (record-engine), and `guided4-files` (record-files). `guided2` to `guided4` first put
the schema text's descriptions on the chain store as DuckDB comments (`comment_store`). Those
five were run under store schema 4, before ADR-0023's code.

Under ADR-0023's code (store schema 5): `v2`, `trajlab.exp2.prompt.prefix` itself, for either
record arm (the shared text, whole files included); and `v2-nofile`, the same text with every
mention of whole files taken out (`without_whole_files`) and the chain store's `text_after`
emptied before the attempt (`drop_whole_files`), so the bundle has no `versions/` either. The two
differ only in the whole files, which is the comparison ADR-0023 item 2 asks for. A `v2-nofile`
store still has the empty column and its description, in both arms.

    launch   seed the chain store from the corpus store (as the loop does), render the note,
             write the config and prompt under <jobs>/_exp2-probe/<name>/, start the query
             server or export the bundle, run `trajlab run` (a watcher must hold the jobs dir),
             stop the server, and print the report.
    report   print the report of a finished attempt: reward, tool calls in order (each query
             with its SQL and row count), and the agent's diagnosis file.
    summary  print one JSON line of uptake measures per finished attempt (`uptake`): reward,
             tool calls, agent minutes, queries, characters of record seen, queries after work
             began, full reads, which playbook queries ran, and where the diagnosis was written.

The report is JSON on stdout: `reward`, `tool_calls`, `queries` (from `agent/query_log.jsonl`),
`query_calls` (Bash calls that ran the query tool), `steps` (one line per tool call), and
`diagnosis` (`/logs/agent/diagnosis.md` as the agent wrote it).

    uv run trajlab watch corpus/exp2/probe-jobs --every 1000 --gate none &
    uv run python scripts/2026-11-02_exp2_engine_probe.py launch \\
        --corpus-store corpus/stores/mvcc-local \\
        --root corpus/jobs/probe-mvcc-first/mvcc-lsm-compaction__CsiwPnL \\
        --jobs-dir corpus/exp2/probe-jobs --arm record-engine --variant guided4 \\
        --name probe-mv-eng-g4
    uv run python scripts/2026-11-02_exp2_engine_probe.py summary corpus/exp2/probe-jobs/probe-*
"""

import argparse
import json
import logging
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from harbor.models.trial.result import TrialResult

from trajlab.atif.load import load_trial_trajectory
from trajlab.contracts import QUERY_LOG_FILENAME, RECORD_MOUNT, Exp2Arm, QueryLogEntry
from trajlab.exp2 import prompt
from trajlab.exp2.arms import QueryServer, arm_config, job_config
from trajlab.exp2.chain import bundle_dir, export_bundle, seed_store
from trajlab.store.describe import _descriptions, describe, relations
from trajlab.store.schema import connect, connect_agent, record_path

logger = logging.getLogger("exp2_engine_probe")

CORPUS_CONFIG = Path("configs/harbor/tb40-sonnet-v4.json")
PROBE_DIRNAME = "_exp2-probe"


def baseline(arm: Exp2Arm, k: int, store: Path) -> str:
    """The note `tb40-exp2` ran (ADR-0022 item 2)."""
    schema = None
    if arm == "record-engine":
        with connect_agent(store) as con:
            schema = describe(con)
    return prompt.prefix(arm, k, schema)


QUERY = prompt.QUERY_COMMAND
PLAYBOOK = (
    (
        "How each attempt ended",
        "SELECT t.attempt_idx, t.reward, s.tool_calls, s.mutating_calls, "
        "s.unverified_completion FROM trials t JOIN trial_summary s USING (trial_id) "
        "ORDER BY t.attempt_idx",
    ),
    (
        "What each attempt told the user when it stopped: what it claimed, and what it admits "
        "it did not do or did not check",
        "SELECT t.attempt_idx, s.step_idx, s.message FROM steps s JOIN trials t USING "
        "(trial_id) WHERE s.tool IS NULL AND s.message <> '' QUALIFY row_number() OVER "
        "(PARTITION BY s.trial_id ORDER BY s.step_idx DESC) = 1 ORDER BY t.attempt_idx",
    ),
    (
        "Every call each attempt made, in order, with the ones that changed files or hit an "
        "error marked",
        "SELECT t.attempt_idx, s.step_idx, s.tool, left(coalesce(s.command_shell, s.args_json), "
        "200) AS call, f.is_mutating, f.has_error_marker, left(s.output, 200) AS output FROM "
        "steps s JOIN trials t USING (trial_id) JOIN step_flags f USING (trial_id, step_idx) "
        "WHERE f.is_tool_call ORDER BY t.attempt_idx, s.step_idx",
    ),
    (
        "Which files each attempt changed, the files the task names first",
        "SELECT t.attempt_idx, p.path, p.versions, p.steps, p.names_prompt_path FROM "
        "path_versions p JOIN trials t USING (trial_id) WHERE NOT p.is_cache ORDER BY "
        "p.names_prompt_path DESC, t.attempt_idx, p.first_step LIMIT 100",
    ),
    (
        "What an attempt wrote into one file, version by version (a hunk is a unified diff; "
        "for a file the step added, it is the whole file)",
        "SELECT t.attempt_idx, d.step_idx, d.op, d.hunk FROM deltas d JOIN trials t USING "
        "(trial_id) WHERE d.path = '/path/to/file' ORDER BY t.attempt_idx, d.step_idx",
    ),
    (
        "One call in full: its command, scripts written inline included, and its whole output",
        "SELECT s.args_json, s.output FROM steps s JOIN trials t USING (trial_id) WHERE "
        "t.attempt_idx = 0 AND s.step_idx = 8",
    ),
)
GUIDED_RECORD = (
    "You are in a fresh environment: none of the earlier work is in it. What the earlier "
    "attempts did is kept in a record, and the record is your best evidence of why they failed. "
    "It holds every tool call each attempt made, with its full command and its full output; "
    "every file each attempt created, changed, or deleted, as a diff; and each attempt's final "
    "message, including what it said it could not do or did not check.\n\n"
    f'Query the record with {QUERY} "SELECT ...". It is read-only SQL (DuckDB). Rows print as '
    "JSON lines, at most 200 per query; shorten long text with left(column, n), and narrow with "
    "WHERE or LIMIT."
)
GUIDED_USE = (
    "Every attempt in the record FAILED, so treat what it concluded as a claim to check, not as "
    "a fact: a requirement it called impossible, a reading of the task it settled on, a check it "
    "said passed. Use the record while you work, too: before you try an approach, look up "
    "whether an earlier attempt tried it and what came of it; before you finish, compare your "
    "deliverables with the earlier attempts' versions (query 5), since a result that matches a "
    "failed one fails again."
)
GUIDED_STEPS = """\
Work in this order:
1. Diagnose. Query the record (at least queries 1 to 5) and work out the most likely reasons \
the previous attempts failed. Before changing any other file, write them to \
/logs/agent/diagnosis.md: each suspected cause, the evidence for it (the attempt and step_idx in \
the record), and how you will check it.
2. List every requirement in the task, each with a check you can run that would fail if the \
requirement were not met. Include inputs other than the ones provided.
3. Fix, run the checks, and repeat until they all pass.
4. Before finishing, run every check again, and compare your deliverables with the failed \
attempts' versions in the record. If you would have to say a requirement is untested, test it \
instead."""


def guided(arm: Exp2Arm, k: int, store: Path) -> str:
    """The record-engine note with the record named as the evidence, a playbook of queries that
    each answer one question, and protocol steps that send the agent to the record."""
    if arm != "record-engine":
        raise SystemExit("the guided variant is written for record-engine only")
    with connect_agent(store) as con:
        schema = describe(con)
    playbook = "These queries answer the questions that matter most:\n\n" + "\n".join(
        f'{i}. {question}:\n   {QUERY} "{sql}"' for i, (question, sql) in enumerate(PLAYBOOK, 1)
    )
    reference = "The record's tables, for other queries:\n\n" + schema.strip()
    parts = [prompt.note(k), GUIDED_RECORD, playbook, GUIDED_USE, reference, GUIDED_STEPS]
    return "\n\n".join(parts)


TABLE_GUIDE = """\
Tables, one row per:
- trials: attempt (attempt_idx, reward, prompt = the task as given, cost_usd)
- trial_summary: attempt, as counts (tool_calls, mutating_calls, first_error_step, \
unverified_completion)
- steps: tool call or message, in order (step_idx, tool, args_json = the call in full, \
command_shell = a Bash command without its inline scripts, output, is_error, message)
- step_flags: step, as flags (is_mutating, has_error_marker, is_test_command)
- deltas: change to one path at one step (path, op, hunk = unified diff, the whole file when \
op is 'add', sha256_after)
- path_versions: path an attempt changed (versions, steps, last_hunk, names_prompt_path = the \
task names the path)
- delta_flags, delta_attribution, writes, step_jobs, job_notices, store_meta: more about \
changes, background jobs, and how the record was built
Every table and column has a description: \
{query} "SELECT table_name, column_name, data_type, comment FROM duckdb_columns() WHERE \
table_name = 'steps'"."""
PLAYBOOK2 = (
    (
        "How each attempt ended, and what it told the user when it stopped: what it claimed, "
        "and what it admits it did not do or did not check",
        "SELECT t.attempt_idx, t.reward, s.step_idx, s.message FROM trials t JOIN steps s USING "
        "(trial_id) WHERE s.tool IS NULL AND s.message <> '' QUALIFY row_number() OVER "
        "(PARTITION BY s.trial_id ORDER BY s.step_idx DESC) = 1 ORDER BY t.attempt_idx",
    ),
    PLAYBOOK[2],
    PLAYBOOK[3],
    PLAYBOOK[4],
    PLAYBOOK[5],
)
GUIDED2_USE = GUIDED_USE.replace("(query 5)", "(query 4)")
GUIDED2_STEPS = """\
Work in this order:
1. Diagnose from the record. Run queries 1 to 3, read the calls and files they point to, and \
work out the most likely reasons the previous attempts failed. Before changing any file, write \
them to /logs/agent/diagnosis.md: each suspected cause, the evidence for it (the attempt and \
step_idx), and how you will check it.
2. List every requirement in the task, each with a check you can run that would fail if the \
requirement were not met. Include inputs other than the ones provided.
3. Fix, run the checks, and repeat until they all pass.
4. Before finishing, run every check again, and compare your deliverables with the failed \
attempts' versions in the record. If you would have to say a requirement is untested, test it \
instead."""


def guided2(arm: Exp2Arm, k: int, store: Path) -> str:
    """`guided` made short: a table guide instead of the schema text, whose descriptions the
    store carries as column comments (`comment_store`), and the playbook cut to five."""
    if arm != "record-engine":
        raise SystemExit("the guided2 variant is written for record-engine only")
    playbook = "These queries answer the questions that matter most:\n\n" + "\n".join(
        f'{i}. {question}:\n   {QUERY} "{sql}"' for i, (question, sql) in enumerate(PLAYBOOK2, 1)
    )
    parts = [
        prompt.note(k),
        GUIDED_RECORD,
        TABLE_GUIDE.format(query=QUERY),
        playbook,
        GUIDED2_USE,
        GUIDED2_STEPS,
    ]
    return "\n\n".join(parts)


GUIDED3_STEPS = """\
Work in this order:
1. Diagnose from the record, before anything else. Run queries 1 to 3 below, read the calls and \
files they point to, and work out the most likely reasons the previous attempts failed. Write \
them to /logs/agent/diagnosis.md before your first command in the environment: each suspected \
cause, the evidence for it (the attempt and step_idx), and how you will check it.
2. List every requirement in the task, each with a check you can run that would fail if the \
requirement were not met. Include inputs other than the ones provided.
3. Fix, run the checks, and repeat until they all pass. Before you try an approach, look up \
whether an earlier attempt tried it and what came of it.
4. Before finishing, run every check again, and compare your deliverables with the failed \
attempts' versions (query 4): a result that matches a failed one fails again. If you would \
have to say a requirement is untested, test it instead."""
GUIDED3_CLAIMS = (
    "Every attempt in the record FAILED, so treat what it concluded as a claim to check, not as "
    "a fact: a requirement it called impossible, a reading of the task it settled on, a check it "
    "said passed."
)


def guided3(arm: Exp2Arm, k: int, store: Path) -> str:
    """`guided2` reordered: the protocol steps right after the record paragraph, with the
    diagnosis due before the first command in the environment, then the claims sentence, the
    table guide, and the playbook; the use-while-working advice moves into steps 3 and 4."""
    if arm != "record-engine":
        raise SystemExit("the guided3 variant is written for record-engine only")
    playbook = "Queries for the questions that matter most:\n\n" + "\n".join(
        f'{i}. {question}:\n   {QUERY} "{sql}"' for i, (question, sql) in enumerate(PLAYBOOK2, 1)
    )
    parts = [
        prompt.note(k),
        GUIDED_RECORD,
        GUIDED3_STEPS,
        GUIDED3_CLAIMS,
        TABLE_GUIDE.format(query=QUERY),
        playbook,
    ]
    return "\n\n".join(parts)


PLAYBOOK4 = (
    PLAYBOOK2[0],
    (
        "The checks each attempt ran, and everything it did after its last change to a file: "
        "what it relied on when it stopped",
        "SELECT t.attempt_idx, s.step_idx, f.is_test_command, left(s.command_shell, 300) AS "
        "command, left(s.output, 800) AS output FROM steps s JOIN step_flags f USING (trial_id, "
        "step_idx) JOIN trials t USING (trial_id) JOIN trial_summary m USING (trial_id) WHERE "
        "f.is_tool_call AND (f.is_test_command OR s.step_idx >= m.last_mutating_step) ORDER BY "
        "t.attempt_idx, s.step_idx",
    ),
    (
        PLAYBOOK2[1][0],
        PLAYBOOK2[1][1]
        .replace("200) AS call", "300) AS call")
        .replace("left(s.output, 200)", "left(s.output, 500)"),
    ),
    PLAYBOOK2[2],
    PLAYBOOK2[3],
    PLAYBOOK2[4],
)
GUIDED4_STEPS = """\
Work in this order:
1. Diagnose from the record, before anything else. Run queries 1 to 3 below, read the calls and \
files they point to, and work out the most likely reasons the previous attempts failed. Write \
them to /logs/agent/diagnosis.md before your first command in the environment: each suspected \
cause, the evidence for it (the attempt and step_idx), and how you will check it.
2. List every requirement in the task, each with a check you can run that would fail if the \
requirement were not met. Whatever checks the failed attempts ran (query 2), they did not catch \
why the attempt failed, so those checks passing proves little: add checks for the cases they did \
not cover, including inputs other than the ones provided.
3. Fix, run the checks, and repeat until they all pass. Before you try an approach, look up \
whether an earlier attempt tried it and what came of it.
4. Before finishing, run every check again, and compare your deliverables with the failed \
attempts' versions (query 5): a result that matches a failed one fails again. If you would \
have to say a requirement is untested, test it instead."""


def guided4(arm: Exp2Arm, k: int, store: Path) -> str:
    """`guided3` with the record as a negative oracle on checks: a query for the checks each
    failed attempt ran and what it did after its last change, step 2 saying those checks were
    not enough, and the timeline's previews widened (300 and 500 characters)."""
    if arm != "record-engine":
        raise SystemExit("the guided4 variant is written for record-engine only")
    playbook = "Queries for the questions that matter most:\n\n" + "\n".join(
        f'{i}. {question}:\n   {QUERY} "{sql}"' for i, (question, sql) in enumerate(PLAYBOOK4, 1)
    )
    parts = [
        prompt.note(k),
        GUIDED_RECORD,
        GUIDED4_STEPS,
        GUIDED3_CLAIMS,
        TABLE_GUIDE.format(query=QUERY),
        playbook,
    ]
    return "\n\n".join(parts)


FILES_RECORD = (
    "You are in a fresh environment: none of the earlier work is in it. What the earlier "
    "attempts did is kept in a record, and the record is your best evidence of why they failed. "
    "It holds every tool call each attempt made, with its full command and its full output; "
    "every file each attempt created, changed, or deleted, as a diff; and each attempt's final "
    "message, including what it said it could not do or did not check.\n\n"
    f"The record is under {RECORD_MOUNT}/, one JSON object per line in each `.jsonl` file; "
    "README.md there lists every field."
)
FILES_GUIDE = (
    "Where the answers to the questions that matter most are:\n\n"
    "1. How each attempt ended, and what it told the user when it stopped: `reward` in "
    "trials.jsonl; in steps.jsonl, the attempt's last row whose `tool` is null and whose "
    "`message` is not empty.\n"
    "2. The checks each attempt ran, and everything it did after its last change to a file: "
    "the rows of step_flags.jsonl with `is_test_command`, and the steps.jsonl rows from "
    "`last_mutating_step` in trial_summary.jsonl on.\n"
    "3. Every call each attempt made, in order: steps.jsonl (`command_shell`, `args_json`, "
    "`output`), with step_flags.jsonl marking `is_mutating` and `has_error_marker`.\n"
    "4. Which files each attempt changed: path_versions.jsonl (`names_prompt_path` marks the "
    "files the task names).\n"
    "5. What an attempt wrote into one file, version by version: "
    "deltas/<trial_id>/<step_idx>/<path>.diff (for a file the step added, the whole file).\n"
    "6. One call in full: its steps.jsonl row (`args_json`, `output`)."
)
GUIDED4_FILES_STEPS = (
    GUIDED4_STEPS.replace("Run queries 1 to 3 below", "Answer questions 1 to 3 below")
    .replace("(query 2)", "(question 2)")
    .replace("(query 5)", "(question 5)")
)


def guided4_files(arm: Exp2Arm, k: int, store: Path) -> str:
    """`guided4` for record-files: the same note, record paragraph, steps, and claims, with
    the questions answered by file pointers instead of queries."""
    if arm != "record-files":
        raise SystemExit("the guided4-files variant is written for record-files only")
    parts = [
        prompt.note(k),
        FILES_RECORD,
        GUIDED4_FILES_STEPS,
        GUIDED3_CLAIMS,
        FILES_GUIDE,
    ]
    return "\n\n".join(parts)


def _sql_string(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def comment_store(store: Path) -> None:
    """Put the schema text's descriptions on the chain store's tables, views, and columns, so
    the agent reads them with `duckdb_columns()` and `duckdb_tables()`/`duckdb_views()`."""
    with connect(record_path(store), read_only=False) as con:
        kinds = dict(
            con.execute(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()
        )
        for name in relations():
            description, columns = _descriptions(name)
            kind = "VIEW" if kinds[name] == "VIEW" else "TABLE"
            con.execute(f"COMMENT ON {kind} {name} IS {_sql_string(description)}")
            for column, note in columns.items():
                con.execute(f"COMMENT ON COLUMN {name}.{column} IS {_sql_string(note)}")


def v2(arm: Exp2Arm, k: int, store: Path) -> str:
    """The record arms' text as ADR-0023 has it in `trajlab.exp2.prompt`."""
    if arm == "control":
        raise SystemExit("v2 is the record arms' text; control's prompt did not change")
    return prompt.prefix(arm, k)


# Each mention of whole files in the v2 text, and what `v2-nofile` says instead.
WHOLE_FILE_MENTIONS: dict[Exp2Arm, tuple[tuple[str, str], ...]] = {
    "record-engine": (
        (" as a diff and as the whole file after each change;", " as a diff;"),
        (", text_after = the whole file after the change)", ")"),
        (
            ": each change as a diff, and the whole file as a step left it:",
            ": each change as a diff:",
        ),
        ("d.op, d.hunk, d.text_after FROM deltas", "d.op, d.hunk FROM deltas"),
    ),
    "record-files": (
        (" as a diff and as the whole file after each change;", " as a diff;"),
        (
            "Each change is also there as files: its diff in "
            "deltas/<trial_id>/<step_idx>/<path>.diff, and the whole file as that step left it in "
            "versions/<trial_id>/<step_idx>/<path>.",
            "Each change's diff is also there as a file: deltas/<trial_id>/<step_idx>/<path>.diff.",
        ),
        (", text_after = the whole file after the change)", ")"),
        (
            ": each change as a diff, and the whole file as a step left it:",
            ": each change as a diff:",
        ),
        (
            "deltas/<trial_id>/<step_idx>/<path>.diff and versions/<trial_id>/<step_idx>/<path>, "
            "with each attempt's",
            "deltas/<trial_id>/<step_idx>/<path>.diff, with each attempt's",
        ),
    ),
}


def without_whole_files(arm: Exp2Arm, text: str) -> str:
    """The v2 text with each mention of whole files replaced; each must occur exactly once."""
    for old, new in WHOLE_FILE_MENTIONS[arm]:
        if text.count(old) != 1:
            raise SystemExit(f"the v2 text holds {old!r} {text.count(old)} times, not once")
        text = text.replace(old, new)
    for word in ("text_after", "versions/", "whole file"):
        if word in text:
            raise SystemExit(f"v2-nofile still mentions {word!r}")
    return text


def v2_nofile(arm: Exp2Arm, k: int, store: Path) -> str:
    """`v2` without whole files: the text says nothing of them, and the store has none."""
    return without_whole_files(arm, v2(arm, k, store))


def drop_whole_files(store: Path) -> None:
    """Empty `deltas.text_after` in the chain store, so neither arm can read a whole file."""
    with connect(record_path(store), read_only=False) as con:
        con.execute("UPDATE deltas SET text_after = NULL")


VARIANTS: dict[str, Callable[[Exp2Arm, int, Path], str]] = {
    "baseline": baseline,
    "guided": guided,
    "guided2": guided2,
    "guided3": guided3,
    "guided4": guided4,
    "guided4-files": guided4_files,
    "v2": v2,
    "v2-nofile": v2_nofile,
}
# Variants whose chain store needs preparing before the attempt starts.
PREPARE: dict[str, Callable[[Path], None]] = {
    **{name: comment_store for name in ("guided2", "guided3", "guided4")},
    "v2-nofile": drop_whole_files,
}


def root_of(trial_dir: Path) -> tuple[UUID, str]:
    """The root failure's trial id and Harbor task name."""
    result = TrialResult.model_validate_json((trial_dir / "result.json").read_text())
    return result.id, result.task_name


def instruction_of(store: Path, trial_id: UUID) -> str:
    with connect(record_path(store), read_only=True) as con:
        row = con.execute(
            "SELECT prompt FROM trials WHERE trial_id = ? AND attempt_idx = 0", [trial_id]
        ).fetchone()
    if row is None:
        raise SystemExit(f"{store} has no attempt 0 {trial_id}")
    return row[0]


def trial_dir_of(job_dir: Path) -> Path:
    found = [p for p in job_dir.iterdir() if p.is_dir() and (p / "agent").is_dir()]
    if len(found) != 1:
        raise SystemExit(f"{job_dir} holds {len(found)} trial dirs, not 1")
    return found[0]


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


DIAGNOSIS_PATH = "/logs/agent/diagnosis.md"


def _calls(trial: Path) -> list[tuple[str, dict[str, Any]]]:
    """Each tool call of the trajectory, in order: its tool name and arguments."""
    return [(tool, args) for tool, args, _ in _calls_with_results(trial)]


def _calls_with_results(trial: Path) -> list[tuple[str, dict[str, Any], int]]:
    """Each tool call, in order, with the characters of the result the agent saw."""
    out = []
    for step in load_trial_trajectory(trial).steps:
        seen = {
            r.source_call_id: len(str(r.content or ""))
            for r in (step.observation.results if step.observation else [])
        }
        for call in step.tool_calls or []:
            args = call.arguments if isinstance(call.arguments, dict) else {}
            out.append((call.function_name, args, seen.get(call.tool_call_id, 0)))
    return out


# A Bash command that writes a file, by its shell text: a redirect into a file, an in-place
# edit, or a command that makes or moves files. A heuristic for "changing a file" in a probe
# attempt, which has no checkpoints to say so.
WRITES_FILE = re.compile(r"(^|[^2&])>\s*[/\w.$]|\btee\b|\bsed -i|\bmkdir\b|\bcp\b|\bmv\b|\btouch\b")


def _kind(tool: str, args: dict[str, Any]) -> str:
    """`query` (reads the record: runs the query tool, or for record-files any call naming the
    bundle's mount), `diagnosis` (writes the diagnosis file), `write` (writes some other file),
    or `env` (any other call)."""
    command = args.get("command") or ""
    path = str(args.get("file_path") or args.get("path") or "")
    if QUERY in command or RECORD_MOUNT in command or path.startswith(RECORD_MOUNT):
        return "query"
    if args.get("file_path") == DIAGNOSIS_PATH or (DIAGNOSIS_PATH in command and ">" in command):
        return "diagnosis"
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit") or WRITES_FILE.search(command):
        return "write"
    return "env"


def uptake(trial: Path) -> dict[str, Any]:
    """How the attempt used the record, from its tool calls and query log.

    - `diagnosis_first`: the diagnosis file was written before the first call that writes any
      other file (step 1 of the protocol: "before changing any other file"; `WRITES_FILE`);
      null when it was never written.
    - `diagnosis_call`: the 1-based position of that write among the attempt's tool calls.
    - `query_calls_after_start`: query calls after the first call that is not a query and not
      the diagnosis: the record used while working, not only up front.
    - `full_reads`: queries that select `args_json`, `output`, or `hunk` without `left(`/
      `substr(` on it: a call or a file read whole rather than skimmed.
    - `playbook`: which v2 questions' queries ran (`fragments`: 1 to 6 by their number in
      `trajlab.exp2.prompt.QUESTIONS`, 7 any query naming `text_after`); for the earlier
      variants the numbers are not their playbooks'.
    - `whole_files`: reads of whole file versions: queries naming `text_after` (record-engine),
      or calls naming the bundle's `versions/` (record-files).
    - `cites_steps`: the diagnosis names a step (`step` followed by a number).
    - `record_chars`: characters of record content the agent saw: the results of its query
      calls (record-engine) or of its calls on the bundle's mount (record-files).
    """
    calls = _calls_with_results(trial)
    kinds = [_kind(tool, args) for tool, args, _ in calls]
    record_chars = sum(n for (_, _, n), k in zip(calls, kinds, strict=True) if k == "query")
    first_env = next((i for i, k in enumerate(kinds) if k in ("env", "write")), len(kinds))
    first_write = next((i for i, k in enumerate(kinds) if k == "write"), len(kinds))
    first_diag = next((i for i, k in enumerate(kinds) if k == "diagnosis"), None)
    log = trial / "agent" / QUERY_LOG_FILENAME
    sqls = [
        " ".join(QueryLogEntry.model_validate_json(line).sql.split())
        for line in (log.read_text().splitlines() if log.is_file() else [])
        if line.strip()
    ]
    # The v2 questions (`trajlab.exp2.prompt.QUESTIONS`), by a fragment of their queries; 7 is
    # any read of a whole file.
    fragments = {
        1: "QUALIFY row_number()",
        2: "is_test_command",
        3: "f.is_mutating, f.has_error_marker",
        4: "FROM path_versions",
        5: "d.op, d.hunk",
        6: "s.args_json, s.output",
        7: "text_after",
    }
    full = 0
    for sql in sqls:
        low = sql.lower()
        for column in ("args_json", "output", "hunk"):
            if re.search(rf"(?<![(\w.]){column}\b|\.{column}\b", low) and not re.search(
                rf"(left|substr|substring)\(\s*[\w.]*{column}", low
            ):
                full += 1
                break
    diagnosis = trial / "agent" / "diagnosis.md"
    text = diagnosis.read_text() if diagnosis.is_file() else ""
    versions = f"{RECORD_MOUNT}/versions"
    whole = sum(1 for sql in sqls if "text_after" in sql) + sum(
        1
        for tool, args, _ in calls
        if versions in str(args.get("command") or "")
        or versions in str(args.get("file_path") or "")
        or versions in str(args.get("path") or "")
        or versions in str(args.get("pattern") or "")
    )
    return {
        "queries": len(sqls),
        "whole_files": whole,
        "record_chars": record_chars,
        "query_calls": kinds.count("query"),
        "query_calls_after_start": sum(1 for k in kinds[first_env:] if k == "query"),
        "full_reads": full,
        "playbook": sorted(n for n, frag in fragments.items() if any(frag in s for s in sqls)),
        "diagnosis_written": bool(text),
        "diagnosis_first": None if first_diag is None else first_diag < first_write,
        "diagnosis_call": None if first_diag is None else first_diag + 1,
        "cites_steps": bool(re.search(r"step[_ ]?(idx)?\s*\d+|steps? \d+", text, re.I)),
    }


def summary(job_dirs: list[Path]) -> list[dict[str, Any]]:
    rows = []
    for job_dir in job_dirs:
        trial = trial_dir_of(job_dir)
        result = TrialResult.model_validate_json((trial / "result.json").read_text())
        rewards = (result.verifier_result.rewards if result.verifier_result else None) or {}
        agent = result.agent_execution
        minutes = (
            round((agent.finished_at - agent.started_at).total_seconds() / 60, 1)
            if agent and agent.started_at and agent.finished_at
            else None
        )
        rows.append(
            {
                "job": job_dir.name,
                "reward": rewards.get("reward"),
                "tool_calls": len(_calls(trial)),
                "agent_minutes": minutes,
                **uptake(trial),
            }
        )
    return rows


def report(job_dir: Path) -> dict[str, Any]:
    trial = trial_dir_of(job_dir)
    result = TrialResult.model_validate_json((trial / "result.json").read_text())
    rewards = (result.verifier_result.rewards if result.verifier_result else None) or {}
    steps: list[str] = []
    query_calls = 0
    for step in load_trial_trajectory(trial).steps:
        for call in step.tool_calls or []:
            args = call.arguments or {}
            command = args.get("command") if isinstance(args, dict) else None
            if command and QUERY in command:
                query_calls += 1
            steps.append(f"{step.step_id} {call.function_name}: {_short(command or args)}")
    log = trial / "agent" / QUERY_LOG_FILENAME
    queries = [
        QueryLogEntry.model_validate_json(line)
        for line in (log.read_text().splitlines() if log.is_file() else [])
        if line.strip()
    ]
    diagnosis = trial / "agent" / "diagnosis.md"
    return {
        "trial": trial.name,
        "reward": rewards.get("reward"),
        "exception": result.exception_info.exception_type if result.exception_info else None,
        "tool_calls": len(steps),
        "query_calls": query_calls,
        "queries": [
            {"sql": q.sql, "rows": q.rows, "truncated": q.truncated, "error": q.error}
            for q in queries
        ],
        "steps": steps,
        "diagnosis": diagnosis.read_text() if diagnosis.is_file() else None,
    }


def launch(args: argparse.Namespace) -> dict[str, Any]:
    arm: Exp2Arm = args.arm
    root_dir: Path = args.root
    trial_id, task = root_of(root_dir)
    chain = args.chains / args.name / arm
    store = chain / "store"
    seed_store(args.corpus_store, store, trial_id)
    if args.variant in PREPARE:
        PREPARE[args.variant](store)
    note = VARIANTS[args.variant](arm, args.k, store)
    instruction = instruction_of(store, trial_id)
    bundle = export_bundle(store, bundle_dir(chain, args.k)) if arm == "record-files" else None
    full = prompt.render(note, instruction)
    spec = arm_config(arm)
    config = job_config(
        json.loads(CORPUS_CONFIG.read_text()),
        task=task,
        arm=spec,
        name=args.name,
        jobs_dir=args.jobs_dir,
        repair_note=note,
        bundle=bundle,
    )
    inputs = args.jobs_dir / PROBE_DIRNAME / args.name
    inputs.mkdir(parents=True, exist_ok=False)
    config_path = inputs / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    (inputs / "prompt.txt").write_text(full)
    logger.info("prompt: %d bytes, variant %s, root %s", len(full.encode()), args.variant, trial_id)
    server = QueryServer(store, args.jobs_dir / args.name, spec) if spec.serves_queries else None
    if server is not None:
        server.start()
    command = [
        str(Path(sys.executable).parent / "trajlab"),
        "run",
        str(config_path),
        "--corpus-id",
        args.name,
        "--manifests-dir",
        str(inputs / "manifests"),
        "--allow-dirty",
    ]
    try:
        with (inputs / "run.log").open("wb") as log:
            code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
    finally:
        if server is not None:
            server.stop()
    logger.info("trajlab run exited %d", code)
    return report(args.jobs_dir / args.name)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("launch", help="Run one attempt and report it.")
    run.add_argument("--corpus-store", type=Path, required=True)
    run.add_argument("--root", type=Path, required=True, help="The root failure's trial dir.")
    run.add_argument("--name", required=True, help="Job name; also the chain's dir name.")
    run.add_argument(
        "--arm", default="record-engine", choices=["control", "record-files", "record-engine"]
    )
    run.add_argument("--variant", default="baseline", choices=sorted(VARIANTS))
    run.add_argument("--k", type=int, default=1, help="Prior attempts the note names.")
    run.add_argument("--jobs-dir", type=Path, default=Path("corpus/jobs"))
    run.add_argument("--chains", type=Path, default=Path("corpus/exp2/probe"))
    show = sub.add_parser("report", help="Report a finished attempt.")
    show.add_argument("job_dir", type=Path)
    table = sub.add_parser("summary", help="One row of uptake measures per finished attempt.")
    table.add_argument("job_dirs", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command == "summary":
        for row in summary(args.job_dirs):
            print(json.dumps(row))
        return
    out = launch(args) if args.command == "launch" else report(args.job_dir)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
