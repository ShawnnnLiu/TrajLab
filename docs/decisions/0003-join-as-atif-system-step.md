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
