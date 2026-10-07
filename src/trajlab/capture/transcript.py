"""A failed trial's history as plain text, for the `traj-text` repair arm (ADR-0012).

The arm puts the failed attempt in the repair agent's prompt, between the note and the task
instruction. The text is rendered from Harbor's ATIF trajectory (`agent/trajectory.json`): one
block per step, in order, with the step's message, then its tool calls with their arguments as
JSON, then the calls' outputs. A step's `reasoning_content` is never rendered, so the transcript
carries no thinking; a step with an empty message gets no message line.

A tool output is the tool result as the failed agent saw it: Claude Code's tool_result content,
which Harbor keeps in the observation's `extra.tool_result_metadata.raw_tool_result` and itself
prefers when it rebuilds a conversation (`ClaudeCode._session_tool_result_content`). Harbor's
`content` adds to that text a `[stdout]` copy, `[metadata]` JSON, and image data as base64 text,
none of which the agent saw. Non-text content, such as an image, is shown as `[<type>]`.

One rule shortens text, and only tool outputs: an output longer than `head + tail` characters
keeps its first `head` and last `tail` characters, with a line between them giving the number
of characters cut. Messages and tool-call arguments are never shortened.
"""

import json
from dataclasses import dataclass
from typing import Any

from harbor.models.trajectories import ContentPart, ObservationResult, Step, Trajectory

HEAD = 2000
TAIL = 2000
BEGIN = "=== TRANSCRIPT OF THE PREVIOUS ATTEMPT ==="
END = "=== END OF TRANSCRIPT ==="


@dataclass(frozen=True)
class TranscriptStats:
    chars: int
    bytes: int  # UTF-8
    outputs_total: int
    outputs_cut: int
    rule: str


def rule(head: int = HEAD, tail: int = TAIL) -> str:
    """The truncation rule, as recorded in `RepairSource.transcript_rule`."""
    return f"tool outputs > {head + tail} chars: first {head} + last {tail}"


def header(head: int = HEAD, tail: int = TAIL) -> str:
    """The lines after BEGIN that tell the reader what the transcript holds."""
    kept = f"first and last {head:,}" if head == tail else f"first {head:,} and last {tail:,}"
    return (
        "It gives the attempt's instruction, then each step: the agent's message,\n"
        f"its tool calls, and their outputs. Tool outputs longer than {head + tail:,}\n"
        f"characters are shortened to their {kept} characters."
    )


def shorten(text: str, head: int = HEAD, tail: int = TAIL) -> tuple[str, bool]:
    """`text` under the rule, and whether the rule cut it."""
    if len(text) <= head + tail:
        return text, False
    cut = len(text) - head - tail
    return f"{text[:head]}\n[... {cut:,} characters cut ...]\n{text[len(text) - tail :]}", True


def as_text(content: str | list[ContentPart] | None) -> str:
    """An ATIF message or tool output as text; an image or audio part becomes `[<type>]`."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(part.text if part.text is not None else f"[{part.type}]" for part in content)


def blocks_as_text(content: Any) -> str:
    """Tool_result content as Claude Code sent it: a string, or blocks whose non-text ones (an
    image) become `[<type>]`."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") if block.get("type") == "text" else f"[{block.get('type')}]"
            for block in content
            if isinstance(block, dict)
        )
    return str(content)


def seen_output(result: ObservationResult) -> str:
    """A tool output as the agent saw it, else Harbor's ATIF content."""
    raw = ((result.extra or {}).get("tool_result_metadata") or {}).get("raw_tool_result")
    if isinstance(raw, dict) and "content" in raw:
        return blocks_as_text(raw["content"])
    return as_text(result.content)


def step_lines(step: Step, head: int = HEAD, tail: int = TAIL) -> tuple[list[str], int, int]:
    """One step's lines, its number of tool outputs, and how many of them the rule cut."""
    lines = []
    message = as_text(step.message)
    if message.strip():
        lines.append(f"[{step.source}] {message}")
    for call in step.tool_calls or []:
        arguments = json.dumps(call.arguments, ensure_ascii=False)
        lines.append(f"[tool call {call.function_name}] {arguments}")
    results = step.observation.results if step.observation else []
    cut = 0
    for result in results:
        output, shortened = shorten(seen_output(result), head, tail)
        lines.append(f"[tool output] {output}")
        cut += shortened
    return lines, len(results), cut


def render(
    trajectory: Trajectory, head: int = HEAD, tail: int = TAIL
) -> tuple[str, TranscriptStats]:
    """The transcript block, from BEGIN to END, and its size and cut counts."""
    blocks = []
    outputs = cut = 0
    for step in trajectory.steps:
        lines, step_outputs, step_cut = step_lines(step, head, tail)
        if lines:
            blocks.append("\n".join(lines))
        outputs += step_outputs
        cut += step_cut
    text = "\n".join([BEGIN, header(head, tail), "", "\n\n".join(blocks), END])
    stats = TranscriptStats(
        chars=len(text),
        bytes=len(text.encode()),
        outputs_total=outputs,
        outputs_cut=cut,
        rule=rule(head, tail),
    )
    return text, stats
