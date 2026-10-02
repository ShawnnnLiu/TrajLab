import json
from pathlib import Path

import pytest

from tests.conftest import FIXTURE_TOOL_CALL_ID
from trajlab.capture.resume import SessionCutError, cut_session, descends_from

SESSION = Path(__file__).parent / (
    "fixtures/hello-world-trial/agent/sessions/projects/-app/"
    "78b481c6-4620-425b-b2c0-ec60bce7422f.jsonl"
)


def test_cut_ends_at_the_tool_result() -> None:
    lines = SESSION.read_text().splitlines()

    cut = cut_session(lines, FIXTURE_TOOL_CALL_ID)

    assert cut == lines[: len(cut)]
    last = json.loads(cut[-1])
    assert last["type"] == "user"
    assert last["message"]["content"][0]["tool_use_id"] == FIXTURE_TOOL_CALL_ID
    # The final assistant reply came after the result, so it is cut.
    assert len(cut) < len(lines)
    assert not any(
        json.loads(line).get("uuid") == "de823e78-d7cd-4efb-bffe-2ff95c328d41" for line in cut
    )


def test_cut_refuses_an_unknown_call() -> None:
    with pytest.raises(SessionCutError):
        cut_session(SESSION.read_text().splitlines(), "toolu_nowhere")


@pytest.mark.parametrize(
    ("image", "base", "expected"),
    [
        (["a", "b", "c"], ["a", "b"], True),
        (["a", "b"], ["a", "b"], False),  # the base itself is not a checkpoint of it
        (["a", "x", "c"], ["a", "b"], False),
        (["a"], ["a", "b"], False),
    ],
)
def test_descends_from(image: list[str], base: list[str], expected: bool) -> None:
    assert descends_from(image, base) is expected
