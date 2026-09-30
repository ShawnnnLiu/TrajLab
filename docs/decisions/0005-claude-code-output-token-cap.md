# ADR-0005: Raise Claude Code's per-request output-token cap to 128000

Status: proposed (2026-09-30).

## Context

Claude Code sends `max_tokens: 32000` on every Messages API request unless `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is set.
Thinking tokens count against that cap.
In `dev-tb21-strict-check` (2026-09-30, `docs/research/2026-09-29_mac-amd64-claude-code-hang.md`), the regex-chess trial spent 32,000 output tokens on thinking alone in four consecutive turns without emitting a tool call.
After each cut-off Claude Code injected its own recovery prompt ("Output token limit hit. Resume directly ...") and retried; after three retries it ended the session with `API Error: Claude's response exceeded the 32000 output token maximum`.
Harbor classifies that as `OutputTokenExceededError`, the agent had written nothing to disk, and the trial scored 0.0.

The cap is a client-side setting, not a model limit.
Claude Sonnet 5.5 accepts up to 128K output tokens per request.
Measured against a stub API server with Claude Code 2.1.278: unset sends 32000; 64000 and 128000 are sent as given; 200000 is clamped to 128000.
The `maxOutputTokens` field in the stream-json `result` event always reads 32000 and does not reflect the setting.

Harbor's `ClaudeCode.run` copies `CLAUDE_CODE_MAX_OUTPUT_TOKENS` from its own process environment into the container (`harbor/agents/installed/claude_code.py`, `run`), and `harbor run --env-file .env` loads `.env` into that process with override.
No Harbor change is needed.

## Decision

Every trajlab run sets `CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000` through `.env`.
`.env.example` documents the line so a fresh checkout gets the same value.
The setting is part of the capture configuration: any run without it, or with a different value, is a different corpus and needs a new `corpus_id`.

## Consequences

- A single thinking-heavy turn can run to 128K output tokens before Claude Code's retry logic engages, so trials that failed only on the cap can complete.
  Trials whose turns need more than 128K still fail the same way.
- Worst-case cost per capped turn rises from about $0.32 to about $1.28 at Sonnet 5.5 output pricing, so a four-retry failure costs about $5 instead of about $1.30.
- Claude Code's recovery prompts appear in the ATIF as `user` steps.
  Analysis must treat them as synthetic harness input, not task input; the exact text is quoted above so they can be matched.
- The corpus manifest (build-order step 5, `capture/corpus.py`) must record this value alongside model, effort, and timeout multiplier, since it changes which trials fail and what they cost.
- Reversal is one line in `.env` plus a new `corpus_id`.
- Not decided here: reasoning effort.
  Lowering it would also shorten thinking turns, but it changes agent behaviour broadly and the corpus design fixed `high`; revisit only with evidence from a full corpus run.
