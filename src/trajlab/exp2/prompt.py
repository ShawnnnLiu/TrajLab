"""The retry prompt (ADR-0016 item 1; ADR-0022; the record arms' text, ADR-0023).

An attempt's agent receives, in order and apart by blank lines, the note, the arm's text, and the
task instruction. `RepairClaudeCode` pipes `<repair_note>\\n\\n<instruction>` into Claude Code, so
the loop passes everything before the instruction as the agent's `repair_note` (`prefix`);
`render` is the whole prompt, which the loop checks against the attempt's trajectory once it has
run.

- `control` gets round 2's four protocol steps (`PROTOCOL_STEPS`), as in `tb40-exp2`.
- The two record arms get one text (ADR-0023): what the record holds (`RECORD_INTRO`), how to
  reach it (the arm's only own paragraph: `ENGINE_ACCESS` or `FILES_ACCESS`), protocol steps
  that work from the record (`RECORD_STEPS`), the failed attempts' conclusions as claims to check
  (`CLAIMS`), a guide to the tables (`TABLE_GUIDE`), and six questions (`QUESTIONS`), each
  answered for record-engine by ready queries and for record-files by the files that hold the
  answer. The tables' full descriptions are one query away in record-engine (the store's
  comments, `trajlab.store.describe.comment_statements`) and one file away in record-files (the
  bundle's README, the same text).

The prefix depends only on the arm and the number of prior attempts, so the preregistration fixes
every prefix's hash; the instruction is the root failure's own (`trials.prompt` of attempt 0).
"""

import hashlib
from dataclasses import dataclass

from trajlab.capture.repair import DIAGNOSIS_PATH, REPAIR_PROTOCOL, repair_instruction
from trajlab.contracts import QUERY_TOOL_RELPATH, RECORD_MOUNT, Exp2Arm
from trajlab.store.export import (
    HUNK_SUFFIX,
    HUNKS_DIRNAME,
    README_FILENAME,
    VERSIONS_DIRNAME,
)
from trajlab.store.query import DEFAULT_ROW_CAP

NOTE = (
    "{k} previous attempt(s) at this task FAILED the task's tests. The tests are not visible to "
    "you and may check cases beyond the examples in the environment."
)
# The clause ADR-0016 item 1 removes from round 2's step 1: a fresh environment holds no earlier
# work.
EARLIER_WORK_CLAUSE = ", and your earlier work on this task if you can see it"
QUERY_COMMAND = f"/logs/agent/{QUERY_TOOL_RELPATH}"


class PromptError(ValueError):
    """The retry prompt cannot be rendered as ADR-0016 and ADR-0023 define it."""


def protocol_steps(protocol: str = REPAIR_PROTOCOL) -> str:
    """Round 2's protocol without its first paragraph (round 1's note, which `NOTE` replaces)
    and without the clause about earlier work: "Work in this order:" and the four steps."""
    _, sep, steps = protocol.partition("\n\n")
    if not sep or not steps.startswith("Work in this order:"):
        raise PromptError("REPAIR_PROTOCOL no longer has its note, then its steps")
    if steps.count(EARLIER_WORK_CLAUSE) != 1:
        raise PromptError(f"REPAIR_PROTOCOL's step 1 no longer holds {EARLIER_WORK_CLAUSE!r}")
    return steps.replace(EARLIER_WORK_CLAUSE, "")


# control's steps.
PROTOCOL_STEPS = protocol_steps()

# -- the record arms (ADR-0023) ---------------------------------------------------------------

RECORD_INTRO = (
    "You are in a fresh environment: none of the earlier work is in it. What the earlier "
    "attempts did is kept in a record, and the record is your best evidence of why they failed. "
    "It holds every tool call each attempt made, with its full command and its full output; "
    "every file each attempt created, changed, or deleted, as a diff and as the whole file after "
    "each change; and each attempt's final message, including what it said it could not do or "
    "did not check."
)
ENGINE_ACCESS = (
    f'Read the record with {QUERY_COMMAND} "SELECT ...". It is read-only SQL (DuckDB). Rows print '
    f"as JSON lines, at most {DEFAULT_ROW_CAP} per query, so a long text value prints as one JSON "
    "string; shorten it with left(column, n), and narrow with WHERE or LIMIT."
)
FILES_ACCESS = (
    f"The record is under {RECORD_MOUNT}/. Each table below is a file there, <table>.jsonl, one "
    "JSON object per line. Each change is also there as files: its diff in "
    f"{HUNKS_DIRNAME}/<trial_id>/<step_idx>/<path>{HUNK_SUFFIX}, and the whole file as that step "
    f"left it in {VERSIONS_DIRNAME}/<trial_id>/<step_idx>/<path>."
)
RECORD_STEPS = f"""\
Work in this order:
1. Diagnose from the record, before anything else. Answer questions 1 to 3 below from the \
record, read the calls and files they point to, and work out the most likely reasons the \
previous attempts failed. Write them to {DIAGNOSIS_PATH} before your first command in the \
environment: each suspected cause, the evidence for it (the attempt and step_idx), and how you \
will check it.
2. List every requirement in the task, each with a check you can run that would fail if the \
requirement were not met. Whatever checks the failed attempts ran (question 2), they did not \
catch why the attempt failed, so those checks passing proves little: add checks for the cases \
they did not cover, including inputs other than the ones provided.
3. Fix, run the checks, and repeat until they all pass. Before you try an approach, look up \
whether an earlier attempt tried it and what came of it.
4. Before finishing, run every check again, and compare your deliverables with the failed \
attempts' versions (question 5): a result that matches a failed one fails again. If you would \
have to say a requirement is untested, test it instead."""
CLAIMS = (
    "Every attempt in the record FAILED, so treat what it concluded as a claim to check, not as "
    "a fact: a requirement it called impossible, a reading of the task it settled on, a check it "
    "said passed."
)


@dataclass(frozen=True)
class GuideEntry:
    """One table of `TABLE_GUIDE`: what a row is, and its columns that matter most, each
    `name` or `name = gloss`."""

    table: str
    row: str
    columns: tuple[str, ...]


GUIDE: tuple[GuideEntry, ...] = (
    GuideEntry(
        "trials", "attempt", ("attempt_idx", "reward", "prompt = the task as given", "cost_usd")
    ),
    GuideEntry(
        "trial_summary",
        "attempt, as counts",
        ("tool_calls", "mutating_calls", "first_error_step", "last_mutating_step"),
    ),
    GuideEntry(
        "steps",
        "tool call or message, in order",
        (
            "step_idx",
            "tool",
            "args_json = the call in full",
            "command_shell = a Bash command without its inline scripts",
            "output",
            "is_error",
            "message",
        ),
    ),
    GuideEntry(
        "step_flags", "step, as flags", ("is_mutating", "has_error_marker", "is_test_command")
    ),
    GuideEntry(
        "deltas",
        "change to one path at one step",
        ("path", "op", "hunk = unified diff", "text_after = the whole file after the change"),
    ),
    GuideEntry(
        "path_versions",
        "path an attempt changed",
        ("versions", "steps", "last_hunk", "names_prompt_path = the task names the path"),
    ),
)
GUIDE_REST = (
    "delta_flags, delta_attribution, writes, step_jobs, job_notices, store_meta: more about "
    "changes, background jobs, and how the record was built"
)


def table_guide() -> str:
    lines = ["Tables, one row per:"]
    lines += [f"- {e.table}: {e.row} ({', '.join(e.columns)})" for e in GUIDE]
    lines.append(f"- {GUIDE_REST}")
    return "\n".join(lines)


TABLE_GUIDE = table_guide()
ENGINE_DESCRIPTIONS = (
    f'Every table and column has a description: {QUERY_COMMAND} "SELECT table_name, column_name, '
    "data_type, comment FROM duckdb_columns() WHERE table_name = 'steps'\"."
)
FILES_DESCRIPTIONS = f"{README_FILENAME} there describes every table and column."


@dataclass(frozen=True)
class Question:
    """One of the questions both record arms are given: record-engine gets `queries` to run,
    record-files `files`, where the answer is."""

    text: str
    queries: tuple[str, ...]
    files: str


QUESTIONS: tuple[Question, ...] = (
    Question(
        "How each attempt ended, and what it told the user when it stopped: what it claimed, "
        "and what it admits it did not do or did not check",
        (
            "SELECT t.attempt_idx, t.reward, s.step_idx, s.message FROM trials t JOIN steps s "
            "USING (trial_id) WHERE s.tool IS NULL AND s.message <> '' QUALIFY row_number() OVER "
            "(PARTITION BY s.trial_id ORDER BY s.step_idx DESC) = 1 ORDER BY t.attempt_idx",
        ),
        "`reward` in trials.jsonl; in steps.jsonl, the attempt's last row whose `tool` is null "
        "and whose `message` is not empty",
    ),
    Question(
        "The checks each attempt ran, and everything it did after its last change to a file: "
        "what it relied on when it stopped",
        (
            "SELECT t.attempt_idx, s.step_idx, f.is_test_command, left(s.command_shell, 300) AS "
            "command, left(s.output, 800) AS output FROM steps s JOIN step_flags f USING "
            "(trial_id, step_idx) JOIN trials t USING (trial_id) JOIN trial_summary m USING "
            "(trial_id) WHERE f.is_tool_call AND (f.is_test_command OR s.step_idx >= "
            "m.last_mutating_step) ORDER BY t.attempt_idx, s.step_idx",
        ),
        "the step_flags.jsonl rows with `is_test_command`, and the steps.jsonl rows from the "
        "attempt's `last_mutating_step` (trial_summary.jsonl) on",
    ),
    Question(
        "Every call each attempt made, in order, with the ones that changed files or hit an "
        "error marked",
        (
            "SELECT t.attempt_idx, s.step_idx, s.tool, left(coalesce(s.command_shell, "
            "s.args_json), 300) AS call, f.is_mutating, f.has_error_marker, left(s.output, 500) "
            "AS output FROM steps s JOIN trials t USING (trial_id) JOIN step_flags f USING "
            "(trial_id, step_idx) WHERE f.is_tool_call ORDER BY t.attempt_idx, s.step_idx",
        ),
        "steps.jsonl (`command_shell`, `args_json`, `output`), with step_flags.jsonl marking "
        "`is_mutating` and `has_error_marker`",
    ),
    Question(
        "Which files each attempt changed, the files the task names first",
        (
            "SELECT t.attempt_idx, p.path, p.versions, p.steps, p.names_prompt_path FROM "
            "path_versions p JOIN trials t USING (trial_id) WHERE NOT p.is_cache ORDER BY "
            "p.names_prompt_path DESC, t.attempt_idx, p.first_step LIMIT 100",
        ),
        "path_versions.jsonl (`names_prompt_path` marks the files the task names)",
    ),
    Question(
        "What an attempt wrote into one file, version by version: each change as a diff, and the "
        "whole file as a step left it",
        # One row per change carries both, so every diff the agent reads comes with its file
        # (ADR-0023 item 4).
        (
            "SELECT t.attempt_idx, d.step_idx, d.op, d.hunk, d.text_after FROM deltas d JOIN "
            "trials t USING (trial_id) WHERE d.path = '/path/to/file' ORDER BY t.attempt_idx, "
            "d.step_idx",
        ),
        f"{HUNKS_DIRNAME}/<trial_id>/<step_idx>/<path>{HUNK_SUFFIX} and "
        f"{VERSIONS_DIRNAME}/<trial_id>/<step_idx>/<path>, with each attempt's `trial_id` in "
        "trials.jsonl",
    ),
    Question(
        "One call in full: its command, scripts written inline included, and its whole output",
        (
            "SELECT s.args_json, s.output FROM steps s JOIN trials t USING (trial_id) WHERE "
            "t.attempt_idx = 0 AND s.step_idx = 8",
        ),
        "its steps.jsonl row (`args_json`, `output`)",
    ),
)
QUESTIONS_HEADER = "The questions that matter most, and where the record answers them:"


def answer(arm: Exp2Arm, question: Question) -> str:
    if arm == "record-engine":
        return "\n".join(f'   {QUERY_COMMAND} "{sql}"' for sql in question.queries)
    return f"   {question.files}"


def questions(arm: Exp2Arm) -> str:
    """The six questions with the arm's answers."""
    items = [
        f"{i}. {question.text}:\n{answer(arm, question)}" for i, question in enumerate(QUESTIONS, 1)
    ]
    return "\n".join([QUESTIONS_HEADER, "", *items])


def record_text(arm: Exp2Arm) -> str:
    """A record arm's text after the note: the same for both arms but for `ENGINE_ACCESS` or
    `FILES_ACCESS`, the descriptions' pointer, and each question's answer."""
    if arm == "record-engine":
        access, descriptions = ENGINE_ACCESS, ENGINE_DESCRIPTIONS
    elif arm == "record-files":
        access, descriptions = FILES_ACCESS, FILES_DESCRIPTIONS
    else:
        raise PromptError(f"{arm} has no record")
    parts = [
        RECORD_INTRO,
        access,
        RECORD_STEPS,
        CLAIMS,
        f"{TABLE_GUIDE}\n{descriptions}",
        questions(arm),
    ]
    return "\n\n".join(parts)


def note(k: int) -> str:
    if k < 1:
        raise PromptError(f"a retry follows at least one attempt, not {k}")
    return NOTE.format(k=k)


def prefix(arm: Exp2Arm, k: int) -> str:
    """Everything before the task instruction: the agent's `repair_note`."""
    body = PROTOCOL_STEPS if arm == "control" else record_text(arm)
    return "\n\n".join([note(k), body])


def render(prefix_text: str, instruction: str) -> str:
    """The whole prompt the agent receives, exactly as `RepairClaudeCode.run` builds it."""
    return repair_instruction(prefix_text, instruction)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
