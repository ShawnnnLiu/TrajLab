---
name: hooks
description: "Change, add, or debug the Claude Code hooks trajlab injects into the task container (the PostToolUse/PostToolUseFailure/Stop/StopFailure checkpoint hook, the Exp 2 repair stop hook, and the query-tool channel), using the built-in plugin-authoring skill for hook event semantics and the repo's own delivery rules. Use this whenever the task mentions settings.hooks.json, post_tool_use.sh, a hook event, hook stdin fields, --ak config, the .req/.ack protocol, TRAJLAB_ACK_WAIT, or an agent-side command that must talk to the host; and whenever a hook \"did not fire\", \"timed out\", or \"wrote nothing\". Not for hooks in your own Claude Code session; those go through update-config."
---

# Hooks in trajlab

The hooks here run inside the Terminal-Bench task container, under the Claude Code binary Harbor installs (pinned 2.1.278, ADR-0009), with no watcher of their own but a bind-mounted `/logs/agent` as the only channel to the host.
That makes them different from hooks in a developer's settings: the shell may be BusyBox, there is no `jq` or `python`, a non-zero exit would change the agent's behavior, and every second the hook waits counts against Harbor's agent timeout.
Get event semantics from the built-in `plugin-authoring` skill (which hook events exist, what arrives on stdin, exit-code meaning, matcher syntax); get everything about delivery and constraints from here and from `docs/checkpoint-protocol.md`.

## Where the pieces are

| Piece | Path | Notes |
|---|---|---|
| Hook script (the source) | `src/trajlab/checkpoint/hook/post_tool_use.sh` | POSIX sh; one script serves all four events |
| Rendered settings | `configs/claude-code/settings.hooks.json` | generated, never hand-edited |
| Renderer | `src/trajlab/checkpoint/hook/__init__.py` | `HOOK_MATCHER`, `HOOK_TIMEOUT_S`, event lists |
| Tests | `tests/test_checkpoint_hook.py` | run the script with a fake stdin; `test_committed_settings_match_script` fails if the JSON is stale |
| Alpine check (manual, Docker) | `scripts/2026-09-30_hook_alpine_check.sh` | BusyBox sh, ack path, timeout path, bad id |
| Protocol | `docs/checkpoint-protocol.md` | files, sequence, constraints |
| Decisions | ADR-0001 (which tools), 0003 and 0006 (delivery), 0010 (failed calls), 0011 (stop) |

## Editing the checkpoint hook

1. Edit `post_tool_use.sh`, not the JSON.
2. Regenerate: `uv run python -m trajlab.checkpoint.hook > configs/claude-code/settings.hooks.json`.
3. `make test`; then `sh scripts/2026-09-30_hook_alpine_check.sh` if the shell logic changed.
4. Commit script and JSON together.
   Any change in what the hook records is a recording change: ADR plus a new `corpus_id` (CLAUDE.md constraint 5).

Rules the script keeps, and why:
- POSIX sh only, fields pulled with `grep` and `sed`: task images may lack bash, jq, python.
  Claude Code runs command hooks as `sh -c`, so bashisms fail silently on Alpine.
- Never exit non-zero: exit 2 blocks the tool call and other non-zero codes surface to the agent; a capture hook must be invisible to the agent's behavior.
- Atomic request: write `<id>.req.tmp`, then `mv`, so the watcher never reads a partial file.
- Wait for `.ack` up to `TRAJLAB_ACK_WAIT` (default 240 s), then write `.timeout` and check `.ack` once more; the hook `timeout` in the JSON (300 s) must exceed that wait plus one snapshot.
- Stop events have no `tool_use_id`; the request is named `stop_<epoch>_<pid>`.
- `TRAJLAB_ACK_WAIT` reaches the container only through Harbor's `--ae KEY=VALUE`, not from `.env`.

## Enabling hooks for a run

A job config sets `agents[0].kwargs.config` to the settings file; Harbor uploads it verbatim and passes `--settings`.
`trajlab run` refuses such a config unless `trajlab watch` holds the lock on the jobs dir, because without a watcher every hooked call waits the full 240 s and the trial records nothing.
Start the watcher first: `uv run trajlab watch corpus/jobs --every 1 --gate change`.

## Adding a hook for Exp 2

The repair protocol needs a `Stop` hook that runs only in repair attempts (gated on `TRAJLAB_REPAIR=1`), and the query tool needs an agent-side command.
Neither exists on any branch yet; when building them:
- Put a second script next to `post_tool_use.sh` and extend `render_settings` to emit it, so the same test keeps JSON and source in sync; do not add a second settings file by hand.
- Gate on an environment variable the launcher passes with `--ae`; a hook that reads a file to decide whether to run adds a failure mode.
- The query tool is not a hook: it is a command under `/logs/agent/bin/` the agent calls, which writes `<id>.sql` to `/logs/agent/queries/` and waits for `<id>.result` or `<id>.err` from a host process (ADR-0014 open question 2).
  Reuse the hook's wait loop and atomic-write pattern; keep it separate from the checkpoint watcher so a slow query never delays an ack.
- Log on the host, by construction, not from inside the container.

## Debugging a hook that "did nothing"

In order, because each is a known cause:
1. Was the settings file actually passed? Check the trial's `config.json` for the agent kwargs, and `agent/sessions/` for a `settings` reference in Claude Code's debug output.
2. Did the event fire? `PostToolUse` does not fire for a call that ended in error; `PostToolUseFailure` does (ADR-0010). `MultiEdit` matches nothing on 2.1.278.
3. Did the id parse? `agent/checkpoints/hook-errors.log` holds inputs with no usable `tool_use_id`.
4. Did the watcher answer? `.req` without `.ack` means the watcher was down, not holding the lock, or stuck in a commit; read that trial's `agent/checkpoints/watcher.log`.
5. Did the shell choke? Run the Alpine check; a bashism is the usual culprit.
