# ADR-0007: Checkpoint every N state-mutating calls

Status: proposed (2026-09-30); superseded as the default by ADR-0010 (2026-10-01), still available as `--gate none`.
Amends ADR-0001.

## Context

ADR-0001 checkpoints after every state-mutating tool call and names checkpoint-every-N as the fallback "if snapshot cost makes a 20-task corpus exceed budget".
The step-6 acceptance run (ADR-0006, "Acceptance run") measured that cost: each `docker commit` re-captures Harbor's agent install, 1.33 GB and 36 s per checkpoint on hello-world, whatever the tool call changed.
A trial with 50 state-mutating calls would add about 30 minutes of agent time and 65 GB of images.

Keeping the install out of the writable layer would cut the per-checkpoint cost instead, but it means running every task on an image derived from the Terminal-Bench one.
The team is not taking that on yet.

## Decision

1. **The watcher takes a checkpoint on every Nth state-mutating call of a trial**, `trajlab watch <jobs-dir> --every N`.
   N is required; `--every 1` is ADR-0001's per-call behavior.
2. **Every matched call still fires the hook and still gets an `.ack`.**
   The watcher must see each call to count it, and the hook is unchanged.
   A call that does not trigger a checkpoint gets a *deferred* ack, `{"tool_call_id": ..., "deferred": true}`, written without a snapshot, so the agent waits only for the watcher's reaction time.
3. **Each checkpoint lists the calls it covers.**
   `CheckpointRecord.covered_tool_call_ids` holds, in request order, every call since the previous checkpoint whose effects first appear in this one, ending with the record's own `tool_call_id`.
   Calls whose hook timed out and calls whose late snapshot was discarded are covered by the next checkpoint the same way.
4. **The count is derived from disk, not kept in memory.**
   A trial's uncovered calls are its answered `.req` files (with `.ack` or `.timeout`) that no record covers, ordered by `requested_at`.
   A watcher restart therefore resumes the count exactly.
5. **The policy is written into each trial.**
   On first contact with a trial the watcher writes `agent/checkpoints/policy.json`, a `CheckpointPolicy` (`backend`, `every`).
   A watcher whose policy differs from a trial's file refuses to answer that trial's requests, so one trial never mixes policies.
6. **The corpus manifest records N** as `checkpoint_every`, read from the trials' `policy.json` files; trials that disagree are not one corpus, and a corpus without checkpoints records null.

## Consequences

- Attribution is per N calls, not per call: a checkpoint shows the combined effect of the calls it covers.
  The question "which action changed the environment" can then be answered exactly only when N is 1, or by narrowing to a window of N calls.
- **The final state can be missing.** Up to N-1 trailing calls after the last checkpoint are covered by none, because no hook fires when the agent stops.
  A Claude Code `Stop` hook could take a final checkpoint; that is a separate decision.
- The per-checkpoint cost is unchanged (1.33 GB, 36 s on hello-world); only the count drops, by a factor of N.
- Postprocess (build-order step 7) joins each covered call to the checkpoint that covers it, and other steps to the most recent earlier checkpoint, as ADR-0001 already prescribes for read-only tools.
- N is a capture setting: changing it means a new `corpus_id`.
- Revisit if the derived-image approach becomes acceptable; per-call checkpoints would then be affordable again.
