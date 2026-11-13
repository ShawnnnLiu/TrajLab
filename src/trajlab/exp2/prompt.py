"""The retry prompt (ADR-0016 item 1; ADR-0022; the record arms' text, ADR-0024).

An attempt's agent receives, in order and apart by blank lines, the note, the arm's text, and the
task instruction. `RepairClaudeCode` pipes `<repair_note>\\n\\n<instruction>` into Claude Code, so
the loop passes everything before the instruction as the agent's `repair_note` (`prefix`);
`render` is the whole prompt, which the loop checks against the attempt's trajectory once it has
run.

- `control` gets round 2's four protocol steps (`PROTOCOL_STEPS`), as in `tb40-exp2`.
- The two record arms get one text (ADR-0024): what the record holds (`RECORD_INTRO`), how to
  reach it (the arm's only own paragraph: `ENGINE_ACCESS` or `FILES_ACCESS`, each ending with how
  to read a long value past the shell's output cap, `OUTPUT_CAP`), protocol steps that work from
  the record (`RECORD_STEPS`), the failed attempts' conclusions as claims to check (`CLAIMS`), a
  guide to the tables (`TABLE_GUIDE`), and five questions (`QUESTIONS`), each answered for
  record-engine by ready queries and for record-files by the files that hold the answer. The
  diagnosis answers the first four, which include what each attempt wrote into its files, as
  diffs holding the whole file (the amendment of 2026-11-04). The tables' full descriptions are
  one query away in record-engine (the store's comments,
  `trajlab.store.describe.comment_statements`) and one file away in record-files (the bundle's
  README, the same text).

The prefix depends only on the arm and the number of prior attempts, so the preregistration fixes
every prefix's hash; the instruction is the root failure's own (`trials.prompt` of attempt 0).
"""

import hashlib
from dataclasses import dataclass

from trajlab.capture.repair import DIAGNOSIS_PATH, REPAIR_PROTOCOL, repair_instruction
from trajlab.contracts import QUERY_TOOL_RELPATH, RECORD_MOUNT, Exp2Arm
from trajlab.store.export import HUNK_SUFFIX, HUNKS_DIRNAME, README_FILENAME
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
    """The retry prompt cannot be rendered as ADR-0016 and ADR-0024 define it."""


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

# -- the record arms (ADR-0024) ---------------------------------------------------------------

RECORD_INTRO = (
    "You are in a fresh environment: none of the earlier work is in it. What the earlier "
    "attempts did is kept in a record, and the record is your best evidence of why they failed. "
    "It holds every tool call each attempt made, with its full command and its full output; "
    "every file each attempt created, changed, or deleted, as a diff that holds the whole file "
    "with the changed lines marked; and each attempt's final message, including what it said it "
    "could not do or did not check."
)
# Claude Code 2.1.278 (`trajlab.capture.pins.CLAUDE_CODE_VERSION`) shows at most this many
# characters of a Bash command's output (its `bashOutputMaxChars` default, 30000); past it, the
# agent sees the first 2,000 bytes and the path of a file holding the whole output. Read from the
# installed binary on 2026-11-04 (ADR-0024, amendment of 2026-11-04).
BASH_OUTPUT_CHARS = 30_000
# A piece of a long value that fits under the cap with room for JSON escaping and other columns.
PAGE_CHARS = 20_000
OUTPUT_CAP = (
    f"A command's output longer than {BASH_OUTPUT_CHARS:,} characters is cut to its first 2,000, "
    "with the path of a file that holds all of it"
)
ENGINE_ACCESS = (
    f'Read the record with {QUERY_COMMAND} "SELECT ...". It is read-only SQL (DuckDB). Rows print '
    f"as JSON lines, at most {DEFAULT_ROW_CAP} per query, so a long text value prints as one JSON "
    f"string; shorten it with left(column, n), and narrow with WHERE or LIMIT. {OUTPUT_CAP}; "
    f"read a long value in parts: substr(column, 1, {PAGE_CHARS}), then "
    f"substr(column, {PAGE_CHARS + 1}, {PAGE_CHARS}), and so on."
)
FILES_ACCESS = (
    f"The record is under {RECORD_MOUNT}/. Each table below is a file there, <table>.jsonl, one "
    "JSON object per line. Each change's diff is also there as a file: "
    f"{HUNKS_DIRNAME}/<trial_id>/<step_idx>/<path>{HUNK_SUFFIX}. {OUTPUT_CAP}; read a long file "
    "in parts: with the Read tool's offset and limit, or sed -n."
)
RECORD_STEPS = f"""\
Work in this order:
1. Diagnose from the record, before anything else. Answer questions 1 to 4 below from the \
record, read the calls and files they point to, and work out the most likely reasons the \
previous attempts failed. Before you change any file, write them to {DIAGNOSIS_PATH}: each \
suspected cause, the evidence for it (the attempt and step_idx), and how you will check it.
2. List every requirement in the task, each with a check you can run that would fail if the \
requirement were not met. Whatever checks the failed attempts ran (question 2), they did not \
catch why the attempt failed, so those checks passing proves little: add checks for the cases \
they did not cover, including inputs other than the ones provided.
3. Fix, run the checks, and repeat until they all pass. Before you try an approach, look up \
whether an earlier attempt tried it and what came of it.
4. Before finishing, run every check again, and compare your deliverables with the failed \
attempts' versions (question 3): a result that matches a failed one fails again. If you would \
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
        ("path", "op", "hunk = unified diff holding the whole file, the changed lines marked"),
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
        "What each attempt wrote into its files, version by version, the files the task names "
        "first: each change as a diff that holds the whole file, the changed lines marked",
        # Both run as written (agents ran given queries verbatim and skipped the one that needed
        # a path filled in, 2026-11-03), and a hunk is never cut: it is the whole file.
        (
            "SELECT t.attempt_idx, p.path, p.versions, p.steps, p.names_prompt_path FROM "
            "path_versions p JOIN trials t USING (trial_id) WHERE NOT p.is_cache ORDER BY "
            "p.names_prompt_path DESC, t.attempt_idx, p.first_step LIMIT 100",
            "SELECT t.attempt_idx, d.step_idx, d.path, d.op, d.hunk FROM deltas d JOIN trials t "
            "USING (trial_id) JOIN delta_flags f USING (trial_id, step_idx, path) WHERE d.hunk "
            "IS NOT NULL AND NOT f.is_cache ORDER BY f.names_prompt_path DESC, t.attempt_idx, "
            "d.path, d.step_idx",
        ),
        # The diffs first, with the command that lists them: record-files agents read the calls
        # in steps.jsonl and seldom opened a `.diff` when pointed at path_versions.jsonl first
        # (2026-11-05).
        "each change's diff is its own file, the whole file with the changed lines marked: "
        f"{HUNKS_DIRNAME}/<trial_id>/<step_idx>/<path>{HUNK_SUFFIX} "
        f"(`find {RECORD_MOUNT}/{HUNKS_DIRNAME} -name '*{HUNK_SUFFIX}'` lists them all), with "
        "each attempt's `trial_id` in trials.jsonl; path_versions.jsonl lists the files each "
        "attempt changed (`names_prompt_path` marks the files the task names)",
    ),
    Question(
        "Every call each attempt made, in order, with the ones that changed files or hit an "
        "error marked",
        (
            "SELECT t.attempt_idx, s.step_idx, s.tool, left(coalesce(s.command_shell, "
            "s.args_json), 300) AS call, f.is_mutating, f.has_error_marker, left(s.output, 600) "
            "AS output FROM steps s JOIN trials t USING (trial_id) JOIN step_flags f USING "
            "(trial_id, step_idx) WHERE f.is_tool_call ORDER BY t.attempt_idx, s.step_idx",
        ),
        "steps.jsonl (`command_shell`, `args_json`, `output`), with step_flags.jsonl marking "
        "`is_mutating` and `has_error_marker`",
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
    """The five questions with the arm's answers."""
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
