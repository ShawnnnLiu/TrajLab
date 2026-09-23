# ADR-0004: Filesystem-only checkpoints; no CRIU-based backend

Status: proposed (2026-09-23).

## Context

The build order planned a `statefork` backend (attach mode) as step 8, and the checkpoint protocol described StateFork/CRIU as the upgrade path past `docker commit`'s filesystem-only limitation.
Source dissection of StateFork v0.7.0 and Waypoint v0.7.0 (`docs/research/2026-09-23_checkpoint-platform-research.md`, Q1/Q3) showed that no CRIU path attaches to the containers Harbor actually runs:

- StateFork's `docker_attach` snapshots via `docker commit` (`vendor/statefork/controller/container_env_manager.py:120`), so it is filesystem-only, functionally identical to our own `docker_commit` backend.
- Memory capture through StateFork requires either `hybrid_attach` (Podman + runc + root; Harbor runs would have to migrate off Docker) or Waypoint (which replaces the container environment entirely with its own sessions and cannot attach to a Docker container).
- Docker's own `docker checkpoint` is experimental and cannot checkpoint containers with an external terminal, which agent tasks use.

Independently, CRIU capture is expensive at our granularity: dumps are memory-sized (~1.7 s per 2 GB per arXiv:2510.05556), taken once per state-mutating tool call, per trial, per corpus.
The project does not have the storage or compute budget for that, and the analysis-phase benchmark questions are framed around filesystem state.

## Decision

`docker_commit` is the only snapshot backend in this project.
The planned `statefork` backend (build-order step 8) is dropped, and no `WaypointEnvironment` will be written.
Checkpoints capture the container filesystem only; live memory, running processes, and shell state are accepted losses, recorded as a limitation in `docs/checkpoint-protocol.md`.

## Consequences

- Build-order step 8 is removed; the capture phase ends at step 7.
- `CheckpointRecord.backend` stays a string and always reads `"docker_commit"`; the field remains so provenance is explicit per snapshot and the decision is queryable in any corpus.
- ADR-0002 is amended: physical-only now holds trivially, since `docker commit` has no virtual mode; the `physical` field remains for the same queryability reason.
- The decision is cheap to reverse: `SnapshotBackend` stays pluggable, and a future CRIU-capable corpus needs only the Podman or Waypoint infrastructure work plus a new `corpus_id`.
  Nothing structural closes.
- The `vendor/statefork` and `vendor/waypoint` submodules are retained as pinned references; the research report's `file:line` citations resolve against them.
- Storage note for the surviving backend: successive `docker commit` images do not dedupe against each other (each re-tars the full writable layer), so per-trial storage grows with cumulative changes times checkpoint count.
  Acceptable for Terminal-Bench-sized write sets; revisit if a task family writes large files.
