"""The retry prompt (ADR-0016 item 1, with ADR-0022's evidence sentences).

An attempt's agent receives, in order and apart by blank lines: the note, the arm's evidence
sentence (none in `control`), the four protocol steps, and the task instruction.
`RepairClaudeCode` pipes `<repair_note>\\n\\n<instruction>` into Claude Code, so the loop passes
everything before the instruction as the agent's `repair_note` (`prefix`); `render` is the whole
prompt, which the loop checks against the attempt's trajectory once it has run.

The prefix depends only on the arm and the number of prior attempts: the evidence sentences
carry the record's layout and schema text, which are the same for every store of one schema and
derived id. So the preregistration fixes every prefix's hash, and the instruction is the root
failure's own (`trials.prompt` of attempt 0).
"""

import hashlib
from collections.abc import Sequence

from trajlab.capture.repair import REPAIR_PROTOCOL, repair_instruction
from trajlab.contracts import QUERY_TOOL_RELPATH, RECORD_MOUNT, Exp2Arm
from trajlab.store.describe import relations
from trajlab.store.export import HUNKS_DIRNAME, README_FILENAME

NOTE = (
    "{k} previous attempt(s) at this task FAILED the task's tests. The tests are not visible to "
    "you and may check cases beyond the examples in the environment."
)
# The clause ADR-0016 item 1 removes from round 2's step 1: a fresh environment holds no earlier
# work, and the evidence sentence says what the agent can see instead.
EARLIER_WORK_CLAUSE = ", and your earlier work on this task if you can see it"
QUERY_COMMAND = f"/logs/agent/{QUERY_TOOL_RELPATH}"


class PromptError(ValueError):
    """The retry prompt cannot be rendered as ADR-0016 defines it."""


def protocol_steps(protocol: str = REPAIR_PROTOCOL) -> str:
    """Round 2's protocol without its first paragraph (round 1's note, which `NOTE` replaces)
    and without the clause about earlier work: "Work in this order:" and the four steps."""
    _, sep, steps = protocol.partition("\n\n")
    if not sep or not steps.startswith("Work in this order:"):
        raise PromptError("REPAIR_PROTOCOL no longer has its note, then its steps")
    if steps.count(EARLIER_WORK_CLAUSE) != 1:
        raise PromptError(f"REPAIR_PROTOCOL's step 1 no longer holds {EARLIER_WORK_CLAUSE!r}")
    return steps.replace(EARLIER_WORK_CLAUSE, "")


PROTOCOL_STEPS = protocol_steps()


def note(k: int) -> str:
    if k < 1:
        raise PromptError(f"a retry follows at least one attempt, not {k}")
    return NOTE.format(k=k)


def bundle_files() -> list[str]:
    """The bundle's relation files, in the order the README lists them (ADR-0018 item 5)."""
    return [f"{name}.jsonl" for name in relations()]


def files_evidence(files: Sequence[str]) -> str:
    """The record-files sentence, its file list rendered from the bundle's layout (ADR-0022)."""
    return (
        f"The record of those attempts is under {RECORD_MOUNT}/: {', '.join(files)}, and "
        f"{HUNKS_DIRNAME}/ (one diff per changed file per step). {README_FILENAME} lists the "
        "fields."
    )


def engine_evidence(schema_text: str) -> str:
    """The record-engine sentence: the command, what it prints (ADR-0019 item 9), and the same
    schema text the bundle's README holds (ADR-0022), with no example rows."""
    if not schema_text.strip():
        raise PromptError("the record-engine sentence needs the store's schema text")
    return (
        "The record of those attempts is a read-only SQL database. Run a query with: "
        f'{QUERY_COMMAND} "SELECT ...". It prints the rows as JSON lines. Tables:\n\n'
        f"{schema_text.strip()}"
    )


def evidence(arm: Exp2Arm, schema_text: str | None = None) -> str | None:
    """The arm's evidence sentence; None in `control`. `schema_text` is
    `trajlab.store.describe.describe` of the chain's store, needed by record-engine only."""
    if arm == "control":
        return None
    if arm == "record-files":
        return files_evidence(bundle_files())
    if schema_text is None:
        raise PromptError("the record-engine sentence needs the store's schema text")
    return engine_evidence(schema_text)


def prefix(arm: Exp2Arm, k: int, schema_text: str | None = None) -> str:
    """Everything before the task instruction: the agent's `repair_note`."""
    sentence = evidence(arm, schema_text)
    parts = [note(k), *([sentence] if sentence is not None else []), PROTOCOL_STEPS]
    return "\n\n".join(parts)


def render(prefix_text: str, instruction: str) -> str:
    """The whole prompt the agent receives, exactly as `RepairClaudeCode.run` builds it."""
    return repair_instruction(prefix_text, instruction)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
