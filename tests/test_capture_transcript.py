from pathlib import Path
from typing import Any

import pytest
from harbor.models.trajectories import Trajectory

from tests.conftest import edit_trajectory
from trajlab.capture.transcript import HEAD, TAIL, header, render, rule, shorten

# What the agent saw: Claude Code's tool_result, not Harbor's ATIF content, which appends a
# `[metadata]` line of the tool's structured result.
FIXTURE_OUTPUT = (
    "File created successfully at: /app/hello.txt "
    "(file state is current in your context — no need to Read it back)"
)


def load(trial_dir: Path) -> Trajectory:
    return Trajectory.model_validate_json((trial_dir / "agent/trajectory.json").read_text())


def test_render_fixture(fixture_trial: Path) -> None:
    trajectory = load(fixture_trial)
    text, stats = render(trajectory)

    # The instruction ends with a newline, kept verbatim, hence the extra blank line after it.
    assert text == (
        "=== TRANSCRIPT OF THE PREVIOUS ATTEMPT ===\n"
        "It gives the attempt's instruction, then each step: the agent's message,\n"
        "its tool calls, and their outputs. Tool outputs longer than 4,000\n"
        "characters are shortened to their first and last 2,000 characters.\n"
        "\n"
        '[user] Create a file called hello.txt with "Hello, world!" as the content.\n'
        "\n"
        "\n"
        "[agent] I'll create the file now.\n"
        '[tool call Write] {"file_path": "/app/hello.txt", "content": "Hello, world!"}\n'
        f"[tool output] {FIXTURE_OUTPUT}\n"
        "\n"
        '[agent] Created `/app/hello.txt` with the content "Hello, world!".\n'
        "=== END OF TRANSCRIPT ==="
    )
    assert "[metadata]" not in text
    assert stats.outputs_total == 1 and stats.outputs_cut == 0
    assert stats.chars == len(text)
    # The tool output has an em dash, so UTF-8 bytes exceed characters.
    assert stats.bytes == len(text.encode()) > stats.chars
    assert stats.rule == "tool outputs > 4000 chars: first 2000 + last 2000"
    assert (HEAD, TAIL) == (2000, 2000)


def set_output(trial_dir: Path, content: Any) -> Trajectory:
    """Give the fixture's one tool result `content` as the text the agent saw."""

    def mutate(data: dict[str, Any]) -> None:
        result = data["steps"][1]["observation"]["results"][0]
        result["extra"]["tool_result_metadata"]["raw_tool_result"]["content"] = content

    edit_trajectory(trial_dir, mutate)
    return load(trial_dir)


def test_long_output_keeps_its_first_and_last_2000_characters(trial_copy: Path) -> None:
    trajectory = set_output(trial_copy, "a" * 2000 + "b" * 1234 + "c" * 2000)
    text, stats = render(trajectory)

    assert f"[tool output] {'a' * 2000}\n[... 1,234 characters cut ...]\n{'c' * 2000}\n" in text
    assert "b" * 10 not in text
    assert stats.outputs_total == 1 and stats.outputs_cut == 1


@pytest.mark.parametrize(("length", "cut"), [(4000, False), (4001, True)])
def test_rule_cuts_only_outputs_over_4000_characters(length: int, cut: bool) -> None:
    text = "x" * length
    shortened, was_cut = shorten(text)
    assert was_cut is cut
    assert shortened == (f"{'x' * 2000}\n[... 1 characters cut ...]\n{'x' * 2000}" if cut else text)


def test_tool_call_arguments_and_messages_are_never_shortened(trial_copy: Path) -> None:
    long_content = "y" * 9000
    long_message = "m" * 9000

    def mutate(data: dict[str, Any]) -> None:
        data["steps"][1]["tool_calls"][0]["arguments"]["content"] = long_content
        data["steps"][2]["message"] = long_message

    edit_trajectory(trial_copy, mutate)
    text, stats = render(load(trial_copy))

    assert f'"content": "{long_content}"}}' in text
    assert f"[agent] {long_message}\n" in text
    assert stats.outputs_cut == 0


def test_reasoning_is_never_rendered(trial_copy: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["steps"][1]["reasoning_content"] = "a thought the transcript must not carry"

    edit_trajectory(trial_copy, mutate)
    text, _ = render(load(trial_copy))
    assert "a thought" not in text


def test_a_step_without_a_message_has_no_message_line(trial_copy: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["steps"][1]["message"] = ""

    edit_trajectory(trial_copy, mutate)
    text, _ = render(load(trial_copy))
    assert "\n\n[tool call Write] " in text
    assert "[agent] \n" not in text


def test_an_image_the_agent_saw_is_a_placeholder_not_its_data(trial_copy: Path) -> None:
    image = {"type": "image", "source": {"type": "base64", "data": "iVBOR" * 50_000}}
    text, stats = render(set_output(trial_copy, [image]))
    assert "[tool output] [image]\n" in text
    assert "iVBOR" not in text and stats.outputs_cut == 0

    text, _ = render(set_output(trial_copy, [{"type": "text", "text": "seen"}, image]))
    assert "[tool output] seen\n[image]\n" in text


def test_without_the_raw_tool_result_the_atif_content_is_used(trial_copy: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        result = data["steps"][1]["observation"]["results"][0]
        del result["extra"]
        result["content"] = "only the ATIF content"

    edit_trajectory(trial_copy, mutate)
    text, _ = render(load(trial_copy))
    assert "[tool output] only the ATIF content\n" in text


def test_content_parts_and_empty_outputs(trial_copy: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["steps"][0]["message"] = [
            {"type": "text", "text": "Look at this."},
            {"type": "image", "source": {"media_type": "image/png", "path": "images/a.png"}},
        ]
        data["steps"][1]["observation"]["results"][0]["extra"] = None
        data["steps"][1]["observation"]["results"][0]["content"] = None

    edit_trajectory(trial_copy, mutate)
    text, stats = render(load(trial_copy))
    assert "[user] Look at this.\n[image]\n" in text
    assert "[tool output] \n" in text
    assert stats.outputs_total == 1


def test_a_step_with_several_calls_lists_calls_then_outputs(trial_copy: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        step = data["steps"][1]
        first = step["tool_calls"][0]
        second = first | {"tool_call_id": "toolu_2", "function_name": "Bash"}
        second["arguments"] = {"command": "ls"}
        step["tool_calls"].append(second)
        result = step["observation"]["results"][0]
        step["observation"]["results"].append(
            {"source_call_id": "toolu_2", "content": "hello.txt\n[metadata] {}"}
        )
        result["extra"]["tool_result_metadata"]["raw_tool_result"]["content"] = "written"

    edit_trajectory(trial_copy, mutate)
    text, stats = render(load(trial_copy))
    assert (
        "[agent] I'll create the file now.\n"
        '[tool call Write] {"file_path": "/app/hello.txt", "content": "Hello, world!"}\n'
        '[tool call Bash] {"command": "ls"}\n'
        "[tool output] written\n"
        "[tool output] hello.txt\n[metadata] {}\n"
    ) in text
    assert stats.outputs_total == 2


def test_carriage_returns_are_kept(trial_copy: Path) -> None:
    text, _ = render(set_output(trial_copy, "10%\r50%\r100%\nline a\r\nline b"))
    assert "[tool output] 10%\r50%\r100%\nline a\r\nline b\n" in text


def test_other_lengths_change_the_rule_and_its_description(trial_copy: Path) -> None:
    trajectory = set_output(trial_copy, "0123456789" * 3)
    text, stats = render(trajectory, head=10, tail=5)

    assert header(10, 5).endswith("shortened to their first 10 and last 5 characters.")
    assert "longer than 15\n" in text
    assert "[tool output] 0123456789\n[... 15 characters cut ...]\n56789\n" in text
    assert stats.rule == rule(10, 5) == "tool outputs > 15 chars: first 10 + last 5"


def test_an_empty_output_the_agent_saw_stays_empty(trial_copy: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        result = data["steps"][1]["observation"]["results"][0]
        result["extra"]["tool_result_metadata"]["raw_tool_result"]["content"] = ""
        result["content"] = "[metadata] {}"

    edit_trajectory(trial_copy, mutate)
    text, _ = render(load(trial_copy))
    assert "[tool output] \n" in text and "[metadata]" not in text
