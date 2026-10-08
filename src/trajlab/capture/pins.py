"""Versions every trajlab capture is locked to (ADR-0009).

Changing a pin changes what trials record: it needs an ADR and a new `corpus_id`, and every job
config under configs/harbor/ must move to the new value in the same PR (a test enforces it).
"""

# Claude Code version for every trial. 2.1.278 ran every corpus so far and was cleared of the
# Sep 29 stream hang (docs/research/2026-09-29_mac-amd64-claude-code-hang.md).
CLAUDE_CODE_VERSION = "2.1.278"

# Claude Code's per-request output-token cap, set through `.env` for every trial (ADR-0005).
CLAUDE_CODE_MAX_OUTPUT_TOKENS = "128000"
