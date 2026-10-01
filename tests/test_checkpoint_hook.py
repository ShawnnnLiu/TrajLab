import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from trajlab.checkpoint.hook import (
    HOOK_MATCHER,
    SETTINGS_REPO_PATH,
    hook_script,
    render_settings,
)
from trajlab.checkpoint.join import CheckpointRequest

REPO = Path(__file__).parent.parent
TOOL_USE_ID = "toolu_01TKn2WsYzAzVaTXotNFqp2Z"


def _stdin(**overrides: object) -> str:
    event = {
        "session_id": "78b481c6-4620-425b-b2c0-ec60bce7422f",
        "transcript_path": "/logs/agent/sessions/projects/-app/x.jsonl",
        "cwd": "/app",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        # An escaped decoy: JSON-escaped quotes inside a value must never be read as a key.
        "tool_input": {"command": 'echo "tool_use_id": "toolu_DECOY"'},
        "tool_response": {"stdout": "Hello, world!", "stderr": "", "interrupted": False},
        "tool_use_id": TOOL_USE_ID,
    }
    return json.dumps(event | overrides)


def _run_hook(agent_dir: Path, stdin: str, wait: str = "1") -> subprocess.CompletedProcess[str]:
    # Exactly how Claude Code runs a shell-form command hook: `sh -c <command>`.
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": str(agent_dir / "sessions")}
    env["TRAJLAB_ACK_WAIT"] = wait
    return subprocess.run(
        ["/bin/sh", "-c", hook_script()],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_committed_settings_match_script() -> None:
    committed = (REPO / SETTINGS_REPO_PATH).read_text()
    assert committed == render_settings(), (
        "settings.hooks.json is stale; run "
        "`uv run python -m trajlab.checkpoint.hook > configs/claude-code/settings.hooks.json`"
    )
    hook = json.loads(committed)["hooks"]["PostToolUse"][0]
    assert hook["matcher"] == HOOK_MATCHER
    assert hook["hooks"][0]["timeout"] > 240


def test_hook_waits_for_ack(tmp_path: Path) -> None:
    checkpoints = tmp_path / "checkpoints"
    req = checkpoints / f"{TOOL_USE_ID}.req"

    def acknowledge() -> None:
        deadline = time.monotonic() + 10
        while not req.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)  # the hook must still be waiting
        (checkpoints / f"{TOOL_USE_ID}.ack").write_text("{}")

    thread = threading.Thread(target=acknowledge)
    thread.start()
    start = time.monotonic()
    result = _run_hook(tmp_path, _stdin(), wait="20")
    thread.join()

    assert result.returncode == 0
    assert result.stdout == ""  # nothing reaches the transcript
    assert 0.3 <= time.monotonic() - start < 5
    assert not (checkpoints / f"{TOOL_USE_ID}.timeout").exists()
    assert not (checkpoints / f"{TOOL_USE_ID}.req.tmp").exists()
    request = CheckpointRequest.model_validate_json(req.read_text())
    assert request == CheckpointRequest(
        tool_use_id=TOOL_USE_ID,
        tool_name="Bash",
        session_id="78b481c6-4620-425b-b2c0-ec60bce7422f",
        agent_id=None,
    )
    assert checkpoints.stat().st_mode & 0o777 == 0o777


def test_hook_times_out_without_watcher(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, _stdin(agent_id="a1b2", tool_name="Write"), wait="1")

    assert result.returncode == 0
    checkpoints = tmp_path / "checkpoints"
    assert (checkpoints / f"{TOOL_USE_ID}.timeout").exists()
    request = CheckpointRequest.model_validate_json(
        (checkpoints / f"{TOOL_USE_ID}.req").read_text()
    )
    assert (request.tool_name, request.agent_id) == ("Write", "a1b2")


@pytest.mark.parametrize("wait", ["", "abc", "-1"])
def test_hook_survives_bad_wait_budget(tmp_path: Path, wait: str) -> None:
    (tmp_path / "checkpoints").mkdir()
    (tmp_path / "checkpoints" / f"{TOOL_USE_ID}.ack").write_text("{}")
    assert _run_hook(tmp_path, _stdin(), wait=wait).returncode == 0


@pytest.mark.parametrize(
    "stdin",
    [
        _stdin(tool_use_id="../../escape"),
        _stdin(tool_use_id=""),
        '{"hook_event_name": "PostToolUse"}',
        "not json",
        "",
    ],
    ids=["traversal", "empty-id", "no-id", "garbage", "no-input"],
)
def test_hook_logs_unusable_input(tmp_path: Path, stdin: str) -> None:
    result = _run_hook(tmp_path, stdin)

    assert result.returncode == 0
    checkpoints = tmp_path / "checkpoints"
    assert [p.name for p in checkpoints.iterdir()] == ["hook-errors.log"]
    assert "no usable tool_use_id" in (checkpoints / "hook-errors.log").read_text()


def test_hook_accepts_pretty_printed_input(tmp_path: Path) -> None:
    stdin = json.dumps(json.loads(_stdin()), indent=2)
    assert _run_hook(tmp_path, stdin).returncode == 0
    assert (tmp_path / "checkpoints" / f"{TOOL_USE_ID}.req").exists()
