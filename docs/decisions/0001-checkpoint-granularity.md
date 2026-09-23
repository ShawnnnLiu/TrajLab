# ADR-0001: Checkpoint granularity

Status: proposed (2026-09-21). Decide before the first checkpointed corpus run.

## Context
"Which actions cause consequential environment changes" is only answerable if a checkpoint can be attributed to a single action. Checkpoint per turn makes attribution impossible in principle; checkpoint per tool call costs one snapshot per call (docker commit: seconds; StateFork/CRIU: ~1.7 s per 2 GB per DAPLab's arXiv paper, arXiv:2510.05556; earlier drafts miscited this as "the SOSP paper", which does not exist). Read-only tools cannot change the environment.

## Decision
Checkpoint after every state-mutating tool call: Bash, Write, Edit, MultiEdit, NotebookEdit. Read-only tools (Read, Grep, Glob, WebFetch, WebSearch, Task) are not checkpointed; postprocess joins their steps to the most recent earlier checkpoint. Subagent (Task) tool calls: the subagent's own Bash/Write calls fire the hook, so they are covered individually.

## Consequences
- Every mutating step has exactly one checkpoint; the join is total.
- Agent wall time grows by (snapshot time + ack latency) per mutating call; corpus manifests must record `timeout_multiplier`.
- Revisit if snapshot cost makes a 20-task corpus exceed budget; the fallback is checkpoint-every-N with N recorded.
