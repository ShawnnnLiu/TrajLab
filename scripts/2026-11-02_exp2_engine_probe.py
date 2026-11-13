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
five were run under store schema 4, before ADR-0023's code. Since ADR-0023, `baseline` is
control's text only (`prompt.prefix("control", k)`, unchanged since `tb40-exp2`) and refuses the
record arms, whose `tb40-exp2` text ADR-0023 retired (it is at 879e75d).

Under ADR-0023's code (store schema 5): `v2`, `trajlab.exp2.prompt.prefix` itself, for either
record arm (the shared text, whole files included); and `v2-nofile`, the same text with every
mention of whole files taken out (`without_whole_files`) and the chain store's `text_after`
emptied before the attempt (`drop_whole_files`), so the bundle has no `versions/` either. The two
differ only in the whole files, which is the comparison ADR-0023 item 2 asks for. A `v2-nofile`
store still has the empty column and its description, in both arms. Both ran under store
schema 5 and are at 7a4001a; schema 6 has no `text_after`, so they refuse to run here.

Under ADR-0023 as amended on 2026-11-04 (store schema 6, every hunk holding the whole file): `v3`,
`trajlab.exp2.prompt.prefix` itself, for either record arm; and `v3-ctx3`, the same text with
every mention of whole-file diffs taken out (`with_3_line_hunks`) and every hunk of the chain
store cut to 3 lines of context (`narrow_store`, with `trajlab.store.reconcile.narrow`, which
gives schema 5's hunks byte for byte), its description put back to a plain unified diff in the
store's comments and, for record-files, in the bundle's README (`readme_3_lines`). The two
differ only in how much of each file a diff shows. Both ran on 2026-11-05 (02:39Z to 05:53Z) with
the text before the change below; each attempt's prompt is in its `<jobs>/_exp2-probe/<name>/
prompt.txt`, and both now refuse to run.

After that run (2026-11-05, ADR-0023's amendment, item 7): `v4`, `trajlab.exp2.prompt.prefix`
again, whose step 1 asks for the diagnosis before any file is changed (not before the first
command, which a record-files agent's first read of the record already is) and whose question 3
points record-files agents at the `.diff` files first, with a command that lists them.

    launch   seed the chain store from the corpus store (as the loop does), render the note,
             write the config and prompt under <jobs>/_exp2-probe/<name>/, start the query
             server or export the bundle, run `trajlab run` (a watcher must hold the jobs dir),
             stop the server, and print the report.
    report   print the report of a finished attempt: reward, tool calls in order (each query
             with its SQL and row count), and the agent's diagnosis file.
    summary  print one JSON line of uptake measures per finished attempt (`uptake`): reward,
             tool calls, agent minutes, queries, characters of record seen, queries after work
             began, full reads, which playbook queries ran, and where the diagnosis was written.
    check    without Docker: for each root and record arm, seed a chain store, prepare it and
             render the prompt of each variant asked for (default `v3` and `v3-ctx3`), check the
             prompt against the task's tests (`trajlab.exp2.leaks`), run every query it gives on
             the chain store, and for record-files export the bundle and find every file it
             names; prints one JSON line per prompt.

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
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from harbor.models.trial.result import TrialResult

from trajlab.atif.load import load_trial_trajectory
from trajlab.contracts import QUERY_LOG_FILENAME, RECORD_MOUNT, Exp2Arm, QueryLogEntry
from trajlab.exp2 import leaks, prompt
from trajlab.exp2.arms import QueryServer, arm_config, job_config
from trajlab.exp2.chain import bundle_dir, export_bundle, seed_store
from trajlab.store.describe import _descriptions, describe, relations
from trajlab.store.export import HUNKS_DIRNAME, README_FILENAME
from trajlab.store.query import DEFAULT_ROW_CAP, render, run_query
from trajlab.store.reconcile import narrow
from trajlab.store.schema import connect, connect_agent, record_path

logger = logging.getLogger("exp2_engine_probe")

CORPUS_CONFIG = Path("configs/harbor/tb40-sonnet-v4.json")
PROBE_DIRNAME = "_exp2-probe"


def baseline(arm: Exp2Arm, k: int, store: Path) -> str:
    """control's note, as `tb40-exp2` ran it (ADR-0022 item 2); ADR-0023 left it unchanged.

    The record arms' `tb40-exp2` text (one sentence and, for record-engine, the schema text) was
    retired by ADR-0023; it is `trajlab.exp2.prompt.prefix` at 879e75d, which is where the
    run note's record-arm `baseline` attempts ran."""
    if arm != "control":
        raise SystemExit(
            f"baseline is control's text now; {arm}'s tb40-exp2 text was retired by ADR-0023 "
            "(it is at 879e75d)"
        )
    return prompt.prefix("control", k)


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


def retired_v2(arm: Exp2Arm, k: int, store: Path) -> str:
    """`v2` and `v2-nofile` ran under store schema 5, whose `deltas.text_after` schema 6 drops."""
    raise SystemExit("v2 and v2-nofile ran under store schema 5; they are at 7a4001a")


def retired_v3(arm: Exp2Arm, k: int, store: Path) -> str:
    """`v3` and `v3-ctx3` ran with the text before item 7 of ADR-0023's amendment."""
    raise SystemExit(
        "v3 and v3-ctx3 ran on 2026-11-05 with the text before the item 7 change; their prompts "
        "are in each attempt's _exp2-probe/<name>/prompt.txt"
    )


def v3(arm: Exp2Arm, k: int, store: Path) -> str:
    """The record arms' text in `trajlab.exp2.prompt` (as `v3`, it ran before item 7)."""
    if arm == "control":
        raise SystemExit("this is the record arms' text; control's prompt did not change")
    return prompt.prefix(arm, k)


# Each mention of whole-file diffs in the v3 text, and what `v3-ctx3` says instead.
_CTX3_SHARED = (
    (" as a diff that holds the whole file with the changed lines marked;", " as a diff;"),
    (
        "hunk = unified diff holding the whole file, the changed lines marked)",
        "hunk = unified diff)",
    ),
    (
        "the files the task names first: each change as a diff that holds the whole file, the "
        "changed lines marked:",
        "the files the task names first: each change as a diff:",
    ),
)
CTX3_MENTIONS: dict[Exp2Arm, tuple[tuple[str, str], ...]] = {
    "record-engine": _CTX3_SHARED,
    "record-files": (
        *_CTX3_SHARED,
        ("each change's diff, which holds the whole file, is ", "each change's diff is "),
    ),
}


def with_3_line_hunks(arm: Exp2Arm, text: str) -> str:
    """The v3 text with each mention of whole-file diffs replaced; each must occur once."""
    for old, new in CTX3_MENTIONS[arm]:
        if text.count(old) != 1:
            raise SystemExit(f"the v3 text holds {old!r} {text.count(old)} times, not once")
        text = text.replace(old, new)
    if "whole file" in text:
        raise SystemExit("v3-ctx3 still mentions 'whole file'")
    return text


def v3_ctx3(arm: Exp2Arm, k: int, store: Path) -> str:
    """`v3` with 3-line hunks: the text says nothing of whole files, and the store has none."""
    return with_3_line_hunks(arm, v3(arm, k, store))


# What the record says of `deltas` and its hunks when they have 3 lines of context, in place of
# schema 6's whole-file descriptions: schema 5's words, with the context made explicit.
CTX3_TABLE = (
    "One row per path whose state changed at a step: what changed, with a unified diff for text."
)
CTX3_HUNK = (
    "Unified diff from the earlier to the later version, for text whose content (sha256) "
    "changed, with 3 lines of context around each change; empty only when neither version has "
    "a line."
)
CTX3_CONTEXT = 3


def narrow_store(store: Path) -> None:
    """Cut every hunk of the chain store to 3 lines of context, and say so in its comments."""
    with connect(record_path(store), read_only=False) as con:
        rows = con.execute(
            "SELECT trial_id, step_idx, path, hunk FROM deltas WHERE hunk IS NOT NULL"
        ).fetchall()
        con.executemany(
            "UPDATE deltas SET hunk = ? WHERE trial_id = ? AND step_idx = ? AND path = ?",
            [(narrow(hunk, CTX3_CONTEXT), t, s, path) for t, s, path, hunk in rows],
        )
        con.execute(f"COMMENT ON TABLE deltas IS {_sql_string(CTX3_TABLE)}")
        con.execute(f"COMMENT ON COLUMN deltas.hunk IS {_sql_string(CTX3_HUNK)}")


def readme_3_lines(bundle: Path) -> None:
    """The bundle's README with the 3-line descriptions (`narrow_store` changed the store's
    comments; the README is written from the code's descriptions)."""
    readme = bundle / README_FILENAME
    text = readme.read_text(encoding="utf-8")
    table, notes = _descriptions("deltas")
    for old, new in (
        (table, CTX3_TABLE),
        (notes["hunk"], CTX3_HUNK),
        ("`hunk`, a diff\nholding the whole file, is also in", "`hunk` is also in"),
    ):
        if text.count(old) != 1:
            raise SystemExit(f"the bundle README holds {old!r} {text.count(old)} times, not once")
        text = text.replace(old, new)
    readme.write_text(text, encoding="utf-8")


VARIANTS: dict[str, Callable[[Exp2Arm, int, Path], str]] = {
    "baseline": baseline,
    "guided": guided,
    "guided2": guided2,
    "guided3": guided3,
    "guided4": guided4,
    "guided4-files": guided4_files,
    "v2": retired_v2,
    "v2-nofile": retired_v2,
    "v3": retired_v3,
    "v3-ctx3": retired_v3,
    "v4": v3,
}
# Variants whose chain store needs preparing before the attempt starts.
PREPARE: dict[str, Callable[[Path], None]] = {
    **{name: comment_store for name in ("guided2", "guided3", "guided4")},
    "v3-ctx3": narrow_store,
}
# Variants whose record-files bundle needs changing after it is exported.
AFTER_EXPORT: dict[str, Callable[[Path], None]] = {"v3-ctx3": readme_3_lines}


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
    return [(tool, args) for tool, args, _ in _kinds(trial)[0]]


# A Bash command that writes a file, by its shell text: a redirect into a file, an in-place
# edit, or a command that makes or moves files. A heuristic for "changing a file" in a probe
# attempt, which has no checkpoints to say so.
WRITES_FILE = re.compile(r"(^|[^2&])>\s*[/\w.$]|\btee\b|\bsed -i|\bmkdir\b|\bcp\b|\bmv\b|\btouch\b")


# Claude Code 2.1.278's note for a result past its inline cap (`prompt.BASH_OUTPUT_CHARS`):
# "Output too large (...). Full output saved to: <path>", then a 2,000-byte preview.
SAVED_TO = re.compile(r"Full output saved to: (\S+)")


def _kind(tool: str, args: dict[str, Any], saved: set[str]) -> str:
    """`query` (reads the record: runs the query tool, for record-files any call naming the
    bundle's mount, or a read of a file Claude Code saved a record read's whole output to,
    `saved`), `diagnosis` (writes the diagnosis file), `write` (writes some other file), or `env`
    (any other call)."""
    command = args.get("command") or ""
    path = str(args.get("file_path") or args.get("path") or "")
    if QUERY in command or RECORD_MOUNT in command or path.startswith(RECORD_MOUNT):
        return "query"
    if any(name in command or name == path for name in saved):
        return "query"
    if args.get("file_path") == DIAGNOSIS_PATH or (DIAGNOSIS_PATH in command and ">" in command):
        return "diagnosis"
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit") or WRITES_FILE.search(command):
        return "write"
    return "env"


def _kinds(trial: Path) -> tuple[list[tuple[str, dict[str, Any], int]], list[str], list[str]]:
    """Each call (tool, arguments, characters of its result), its kind, and its result's text."""
    calls: list[tuple[str, dict[str, Any], int]] = []
    texts: list[str] = []
    for step in load_trial_trajectory(trial).steps:
        seen = {
            r.source_call_id: str(r.content or "")
            for r in (step.observation.results if step.observation else [])
        }
        for call in step.tool_calls or []:
            args = call.arguments if isinstance(call.arguments, dict) else {}
            text = seen.get(call.tool_call_id, "")
            calls.append((call.function_name, args, len(text)))
            texts.append(text)
    saved: set[str] = set()
    kinds: list[str] = []
    for (tool, args, _), text in zip(calls, texts, strict=True):
        kind = _kind(tool, args, saved)
        kinds.append(kind)
        if kind == "query":
            saved.update(SAVED_TO.findall(text))
    return calls, kinds, texts


# A record read that returned diffs: its result (what the agent saw) holds a hunk header. Naming
# a diff is not reading one: agents list `deltas/` beside other reads, and a query can name
# `hunk` and return no row. A query that cuts the hunk with left(/substr( reads it in part.
HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
HUNK_CUT = re.compile(r"\b(left|substr|substring)\(\s*[\w.]*hunk\b", re.I)


def _hunk_reads(
    calls: list[tuple[str, dict[str, Any], int]], kinds: list[str], texts: list[str]
) -> tuple[list[int], list[int]]:
    """The record reads whose result held a diff, by call index, and those that did not cut it.

    A record read (`_kind` `query`: a query-tool call, a call on the bundle, or a read of a
    file Claude Code saved such a call's output to) counts when its result holds a hunk header
    (`HUNK_HEADER`); for a call past the output cap, whose result is a 2,000-byte preview, the
    later read of the saved file counts. It reads the diffs whole unless it is a query that cuts
    the hunk (`HUNK_CUT`); a file read in parts still counts as whole."""
    hunk_calls: list[int] = []
    whole_calls: list[int] = []
    for i, ((_, args, _), kind) in enumerate(zip(calls, kinds, strict=True)):
        if kind != "query" or not HUNK_HEADER.search(texts[i]):
            continue
        hunk_calls.append(i)
        command = str(args.get("command") or "")
        if not (QUERY in command and HUNK_CUT.search(command)):
            whole_calls.append(i)
    return hunk_calls, whole_calls


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
    - `playbook`: which v3 questions' queries ran, as written or trimmed (agents drop an
      `ORDER BY` or a column), by a fragment only the given query has (`fragments`: 1 to 5 by
      their number in `trajlab.exp2.prompt.QUESTIONS`; 3 is the diffs query, whose join on
      `delta_flags` agents' own diff queries lack); for earlier variants the numbers are not
      their playbooks'.
    - `hunk_reads`, `hunk_reads_whole`: the record reads whose result held a diff, and those
      that did not cut it (`_hunk_reads`); `hunk_chars`: characters of their results.
    - `cites_steps`: the diagnosis names a step (`step` followed by a number).
    - `record_chars`: characters of record content the agent saw: the results of its query
      calls (record-engine) or of its calls on the bundle's mount (record-files), and of its
      reads of a file Claude Code saved such a result to when it passed the output cap.
    """
    calls, kinds, texts = _kinds(trial)
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
    # The v3 questions (`trajlab.exp2.prompt.QUESTIONS`), by a fragment of their queries.
    fragments = {
        1: "QUALIFY row_number()",
        2: "is_test_command",
        3: "JOIN delta_flags f USING (trial_id, step_idx, path) WHERE d.hunk IS NOT NULL",
        4: "f.is_mutating, f.has_error_marker",
        5: "s.args_json, s.output",
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
    hunk_calls, whole_calls = _hunk_reads(calls, kinds, texts)
    return {
        "queries": len(sqls),
        "hunk_reads": len(hunk_calls),
        "hunk_reads_whole": len(whole_calls),
        "hunk_chars": sum(calls[i][2] for i in hunk_calls),
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
        cost = result.agent_result.cost_usd if result.agent_result else None
        rows.append(
            {
                "job": job_dir.name,
                "reward": rewards.get("reward"),
                "tool_calls": len(_calls(trial)),
                "agent_minutes": minutes,
                "cost_usd": None if cost is None else round(cost, 4),
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
    if bundle is not None and args.variant in AFTER_EXPORT:
        AFTER_EXPORT[args.variant](bundle)
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


def check(args: argparse.Namespace) -> list[dict[str, Any]]:
    """The dry check of every prompt a comparison gives (the `check` command): one row per
    root, record arm, and variant. Chain stores and bundles go to a temporary dir, removed at
    the end."""
    if len(args.corpus_store) != len(args.root):
        raise SystemExit("give one --corpus-store per --root")
    with tempfile.TemporaryDirectory(prefix="probe-check-") as tmp:
        return _check(args, Path(tmp))


def _check(args: argparse.Namespace, scratch: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for corpus_store, root_dir in zip(args.corpus_store, args.root, strict=True):
        trial_id, task = root_of(root_dir)
        found = leaks.needles(root_dir)
        for arm in ("record-engine", "record-files"):
            for variant in args.variants:
                work = scratch / f"{root_dir.name}-{arm}-{variant}"
                store = work / "store"
                seed_store(corpus_store, store, trial_id)
                if variant in PREPARE:
                    PREPARE[variant](store)
                note = VARIANTS[variant](arm, args.k, store)
                instruction = instruction_of(store, trial_id)
                text = prompt.render(note, instruction)
                queries = [  # every query the text gives, not the access sentence's "SELECT ..."
                    sql
                    for sql in re.findall(rf'{re.escape(QUERY)} "(SELECT [^"]*)"', note)
                    if sql != "SELECT ..."
                ]
                answers = []
                for sql in queries:
                    outcome = run_query(store, sql)
                    answers.append(
                        {
                            "rows": None if outcome.rows is None else len(outcome.rows),
                            "chars": None
                            if outcome.rows is None
                            else len(render(outcome, DEFAULT_ROW_CAP)),
                            "error": outcome.error,
                        }
                    )
                missing: list[str] = []
                if arm == "record-files":
                    bundle = export_bundle(store, work / "bundle")
                    if variant in AFTER_EXPORT:
                        AFTER_EXPORT[variant](bundle)
                    names = set(re.findall(r"[a-z_]+\.jsonl", note)) - {"<table>.jsonl"}
                    missing = sorted(
                        n for n in names | {README_FILENAME} if not (bundle / n).is_file()
                    )
                    missing += [] if (bundle / HUNKS_DIRNAME).is_dir() else [HUNKS_DIRNAME]
                with connect(record_path(store), read_only=True) as con:
                    (comment,) = con.execute(
                        "SELECT comment FROM duckdb_columns() WHERE table_name = 'deltas' "
                        "AND column_name = 'hunk'"
                    ).fetchone() or (None,)
                    hunks = [
                        h
                        for (h,) in con.execute(
                            "SELECT hunk FROM deltas WHERE hunk <> ''"
                        ).fetchall()
                    ]
                rows.append(
                    {
                        "root": root_dir.name,
                        "task": task,
                        "arm": arm,
                        "variant": variant,
                        "prompt_bytes": len(text.encode()),
                        "prefix_bytes": len(note.encode()),
                        "leaks": leaks.leaks(text, instruction, found),
                        "queries": answers,
                        "files_missing": missing,
                        "hunks": len(hunks),
                        "hunks_with_one_header": sum(h.count("\n@@ ") == 1 for h in hunks),
                        "hunk_chars": sum(len(h) for h in hunks),
                        "hunk_comment_whole_file": "whole file" in (comment or ""),
                    }
                )
    return rows


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
    dry = sub.add_parser("check", help="Leak-check every prompt and run every query it gives.")
    dry.add_argument("--corpus-store", type=Path, action="append", required=True)
    dry.add_argument("--root", type=Path, action="append", required=True)
    dry.add_argument("--variants", nargs="+", default=["v4"], choices=sorted(VARIANTS))
    dry.add_argument("--k", type=int, default=1)
    args = parser.parse_args()
    if args.command == "check":
        for row in check(args):
            print(json.dumps(row))
        return
    if args.command == "summary":
        for row in summary(args.job_dirs):
            print(json.dumps(row))
        return
    out = launch(args) if args.command == "launch" else report(args.job_dir)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
