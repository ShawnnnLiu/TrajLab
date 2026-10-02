# ADR-0011: Checkpoint the state the agent leaves behind

Status: proposed (2026-10-01).

## Context

Checkpoints are taken after hooked tool calls (ADR-0006, ADR-0010).
Nothing captures what changes after the last one: a background job the agent started (Harbor sets `FORCE_AUTO_BACKGROUND_TASKS=1`, and agents also use `nohup ... &`) can keep writing until the agent's turn ends, and under every-N (ADR-0007) up to N-1 trailing calls are in no checkpoint.
The final state is the one the verifier grades, so it is the checkpoint analysis needs most.
Claude Code 2.1.278 fires `Stop` when the agent's turn ends and `StopFailure` when it ends on an API error; both carry `session_id` and `last_assistant_message` but no `tool_use_id` (checked in the binary).

## Decision

1. The hook is registered for `Stop` and `StopFailure` as well, with no matcher.
2. For those events the hook names the request `stop_<epoch>_<pid>`, since there is no tool call id, and records the event as the tool name.
3. The watcher answers a stop request like a hooked call, with `trigger: "stop"` on its `CallRecord` and, if one is taken, its `CheckpointRecord`:
   under `--gate change` it is checkpointed only if the filesystem differs from the last checkpoint, otherwise its record names that checkpoint; under `--gate none` it is always checkpointed, covering any deferred calls; a trial with no earlier hooked call gets a baseline checkpoint here.
4. The hook still never blocks: `Stop` with exit 0 lets the turn end once the watcher answers, or after the hook's wait budget.

## Consequences

- Every trial ends with a record of its final state: either a stop checkpoint or a stop record that names the checkpoint holding it.
- Writes that land after the last hooked call are attributed to the stop, not to the next call (there is none) and not lost.
- The final checkpoint is the state at the end of the agent's turn, before Harbor runs the verifier; the verifier's own effects are outside capture.
- The stop request id is not an ATIF `tool_call_id`; build-order step 7 places stop checkpoints after the last agent step, keyed by `trigger`.
- A capture change: corpora that use it need a new `corpus_id`.

## Verification (2026-10-01)

A real trial (Claude Code 2.1.278, `--gate change`, pre-installed hello-world image) whose only Bash call wrote `/app/hello.txt` and started `nohup` a job that rewrote `/app/tick.txt` every second, after which the agent ended its turn.
`Stop` fired under Harbor's `--print` run; the watcher measured `~/app/tick.txt` as changed since the call's checkpoint and took stop checkpoint seq 2 (`trigger: "stop"`, id `stop_1790907280_166`); reward 1.0.
Without this ADR that write would have been in no checkpoint.
The hook script was also run under BusyBox `sh` with a `Stop` input whose `last_assistant_message` contained an escaped decoy id; it named the request `stop_...` and ignored the decoy.
