"""The PostToolUse hook script and the Claude Code settings that deliver it inline.

ADR-0006: `post_tool_use.sh` is the source; `configs/claude-code/settings.hooks.json` carries
the script as the hook's `command` string. Regenerate the settings after editing the script:

    uv run python -m trajlab.checkpoint.hook > configs/claude-code/settings.hooks.json
"""

import json
from importlib.resources import files

# ADR-0001 is the authoritative tool list. MultiEdit no longer exists and matches nothing.
HOOK_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"
# Must exceed the hook's ack wait (TRAJLAB_ACK_WAIT, default 240 s) plus one snapshot.
HOOK_TIMEOUT_S = 300
SETTINGS_REPO_PATH = "configs/claude-code/settings.hooks.json"


def hook_script() -> str:
    return files(__package__).joinpath("post_tool_use.sh").read_text()


# PostToolUse does not fire for a call that ends in error (e.g. Bash exiting non-zero), which
# can still have changed files; PostToolUseFailure does (ADR-0010). Stop and StopFailure end the
# agent's turn, normally or on an API error; their checkpoint is the state the agent leaves
# behind (ADR-0011). All four checked in Claude Code 2.1.278.
TOOL_EVENTS = ("PostToolUse", "PostToolUseFailure")
STOP_EVENTS = ("Stop", "StopFailure")
HOOK_EVENTS = TOOL_EVENTS + STOP_EVENTS


def render_settings() -> str:
    """The Claude Code settings JSON that runs the hook script inline, as committed."""
    command = {"type": "command", "command": hook_script(), "timeout": HOOK_TIMEOUT_S}
    # Stop events take no matcher.
    settings = {
        "hooks": {
            **{event: [{"matcher": HOOK_MATCHER, "hooks": [command]}] for event in TOOL_EVENTS},
            **{event: [{"hooks": [command]}] for event in STOP_EVENTS},
        }
    }
    return json.dumps(settings, indent=2) + "\n"
