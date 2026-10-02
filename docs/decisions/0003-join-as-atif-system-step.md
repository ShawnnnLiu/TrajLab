# ADR-0003: The trajectory↔checkpoint join is an ATIF system step

Status: proposed (2026-09-21).

## Context
Waypoint keys state by named checkpoint in a DAG; ATIF keys steps by index. The join could live in a side table (`checkpoints.jsonl`) or inside the trajectory. ATIF v1.8 reserves system steps for "environment resets, checkpoint creation" and lets them carry an `observation`.

## Decision
`checkpoints.jsonl` is the capture-time record. Postprocess additionally inserts, after each agent step that owns a checkpointed `tool_call_id`, a system step:
```
source: "system", message: "checkpoint",
observation.results[0].extra = CheckpointRecord as dict,
extra = {"trajlab": {"kind": "checkpoint", "tool_call_id": ..., "original_step_id": null}}
```
into `agent/trajectory.enriched.json`, renumbering `step_id` and storing each original id in `extra.trajlab.original_step_id`. `agent/trajectory.json` is never modified. The enriched file must pass Harbor's validator.

Compaction boundaries recovered from native JSONL use the same mechanism with `extra.context_management = {type: "compaction", boundary: "replace"}` per RFC §VII.

## Consequences
One file, standard format, validatable, viewable by any ATIF consumer; analysis code never needs the side table. Hook delivery mechanism (inline command vs. watcher-written script) is decided in the implementation PR and recorded here as an amendment.

## Amendment (2026-09-29): typed `extra.trajlab`, build-order step 4

The shapes above are now pydantic models in `src/trajlab/contracts/steps.py`.
Writing them down made the ADR precise in two places the original text left open.

- Every step of the enriched trajectory carries `extra.trajlab`, discriminated by `kind`:
  - `{"kind": "original", "original_step_id": <int>}` on each step copied from `trajectory.json`;
  - `{"kind": "checkpoint", "tool_call_id": ..., "original_step_id": null}` on an inserted checkpoint step, as above;
  - `{"kind": "compaction", "original_step_id": null}` on an inserted compaction step, next to `extra.context_management`.
- A consumer can therefore tell inserted steps from copied ones without comparing against `trajectory.json`, and can always read `extra.trajlab.original_step_id`.
- `extra.trajlab` on a copied step is added next to Harbor's own keys (`id`, `agent_id`, `cwd`, ...), which stay unchanged.

`tests/test_contracts.py` builds this shape from the models and checks it passes Harbor's validator.
Postprocess has not produced any output yet, so no corpus is affected.

## Amendment (2026-09-30): hook delivery, build-order step 6

The hook script is delivered inline (option a in `docs/checkpoint-protocol.md`): `configs/claude-code/settings.hooks.json` carries the whole script as the hook's `command` string.
The script's source is `src/trajlab/checkpoint/hook/post_tool_use.sh`, and the settings file is generated from it; a test fails if the two disagree.
Nothing is uploaded into the container and nothing races the watcher.
ADR-0006 records this together with the other step-6 protocol changes.

## Amendment (2026-10-01): postprocess as built, build-order step 7

`trajlab postprocess <job or trial dir>...` (`src/trajlab/atif/postprocess.py`, `src/trajlab/atif/compaction.py`) writes `agent/trajectory.enriched.json` from Harbor's `trajectory.json`, the files under `agent/checkpoints/`, and the native session JSONL.
Building it settled the questions the text above and `docs/checkpoint-protocol.md` left open.

### Placement

- A tool-call checkpoint goes right after the agent step whose `tool_calls` hold its `tool_call_id`; several checkpoints after one step (parallel calls) go in `seq` order.
- A stop checkpoint (ADR-0011) goes after the last step, since it records the state the agent left behind.
- A compaction step goes before the first step whose timestamp is at or after the boundary's.
  Claude Code 2.1.278 logs the boundary after the summarizing call returns and before the summary message, and Harbor orders steps by timestamp, so the compaction step lands after the checkpoints of the step before it and right before the user step Harbor makes from the summary.
  `tests/test_atif_compaction.py` checks this against Harbor's own converter.

### The read-only join, and calls without a checkpoint

ADR-0001 joins a read-only step to the most recent earlier checkpoint.
In the enriched file that join is positional: it is the last checkpoint step before the step.
Calls the hook saw but that got no checkpoint step carry their own record instead of relying on position:

- each copied step's `extra.trajlab.calls` holds the watcher's `CallRecord` for each of its answered hooked calls, in `tool_calls` order; `checkpoint_seq` names the checkpoint holding the state after the call (`unchanged` calls name the previous one, `deferred` calls name none and are listed by the covering checkpoint's `covered_tool_call_ids`);
- `extra.trajlab.timed_out_tool_call_ids` lists the step's calls whose hook gave up (`.timeout`): no record, no checkpoint;
- a state-mutating call with neither was never hooked, e.g. a failed call in a corpus captured before commit `06e2061`.

Positional joins assume checkpoints are in step order, which holds for one agent but not necessarily for subagents running concurrently; `calls` is exact either way.

### Trajectory root

The root carries `extra.trajlab` as `EnrichedTrajectoryExtra`: the `sha256` of the `trajectory.json` it was built from (to detect a stale enriched file), the trial's `CheckpointPolicy` (null for a stock trial), the stop `CallRecord`s, and timed-out stop ids.
Stop records live here because they belong to no step; every trial with a stop hook therefore names its final state even when the stop got no checkpoint.
`final_metrics.total_steps` is set to the enriched step count; every other root field is Harbor's.

### Compaction records

The compaction step's `observation.results[0].extra` is a `CompactionRecord` (native uuid and timestamp, source file, trigger, token counts, the linked summary event, sidechain and agent id), mirroring how a checkpoint step carries its `CheckpointRecord`.
The summary text is not copied; it is the next user step.
Compaction steps are read from the same files Harbor converts (`<session dir>/*.jsonl` and `subagents/*.jsonl`), deduplicated by uuid as Harbor does.

### Failure semantics

The join must be total, so postprocess refuses a trial rather than drop a record: a checkpoint, call record, or timeout naming no tool call in `trajectory.json`, a record for another trial, or a malformed record fails that trial, and the enriched file is validated with Harbor's validator before it replaces an earlier one.
Records written before ADR-0007 (no `covered_tool_call_ids`, only in `hello-world-checkpoint-v1`) are refused; that corpus is superseded and stays unprocessed.
Postprocess is deterministic: rerunning it on unchanged inputs writes identical bytes.

On the 2026-10-01 copies of every job under `corpus/jobs/`, postprocess wrote and validated an enriched file for each finished trial except the two pre-ADR-0007 ones; none of these trials compacted.
