# ADR-0002: Physical snapshots only

Status: proposed (2026-09-21).

## Context
StateFork's Smart Decider can take a virtual snapshot (record commands since the last physical one; restore by replay). The state at a virtual node is derived, not recorded, and is only as sound as replay determinism. Terminal-Bench runs are not deterministic (paper Appendix E).

## Decision
Every checkpoint is physical. `CheckpointRecord.physical` is always True in this phase; the field exists so a future relaxation is explicit and queryable. The StateFork backend is configured with virtual snapshots disabled.

## Consequences
Higher capture cost; ground truth for every benchmark question is materialized state, never reconstructed state.
