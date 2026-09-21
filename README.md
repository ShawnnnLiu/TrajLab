# trajlab

Capture harness for COMS 6113 project #16: Terminal-Bench runs with Claude Code, recorded as ATIF
trajectories with per-tool-call environment checkpoints, for later analysis of where agent runs go wrong.

Built on [Harbor](https://github.com/harbor-framework/harbor). Harbor runs the agent in a sandbox,
verifies the result, and writes the trajectory. This repo adds three things Harbor does not do:
checkpoint the sandbox at every state-mutating tool call, join each checkpoint to the trajectory step
that caused it, and recover context-compaction boundaries that Harbor's Claude Code converter drops.

## What you get per trial

```
corpus/jobs/<job>/<task>__<id>/
├── result.json                     reward, tokens, cost, timings, exception      (Harbor)
├── lock.json                       task sha256, agent version, env config        (Harbor)
├── agent/trajectory.json           ATIF v1.8                                     (Harbor)
├── agent/sessions/                 Claude Code native session JSONL              (Harbor)
├── agent/checkpoints/              <tool_call_id>.req/.ack, checkpoints.jsonl    (trajlab)
├── agent/trajectory.enriched.json  ATIF + checkpoint and compaction system steps (trajlab)
└── verifier/, artifacts/                                                          (Harbor)
```

## Quickstart

```bash
uv sync                          # Python 3.12, harbor pinned, everything in uv.lock
cp .env.example .env             # add ANTHROPIC_API_KEY
docker info                      # Docker must be running

# 1. Stock corpus, no checkpoints
uv run trajlab run configs/harbor/tb_subset_v0.json

# 2. Same run with checkpoints: watcher first, then the run with hook settings
uv run trajlab watch corpus/jobs --backend docker_commit
uv run trajlab run configs/harbor/tb_subset_v0.json --hooks configs/claude-code/settings.hooks.json

# 3. Enrich and validate
uv run trajlab postprocess corpus/jobs/<job>
uv run trajlab validate corpus/jobs/<job>
```

## How checkpointing works

Harbor fires no hook during the agent phase, but Claude Code does. `settings.hooks.json` registers a
PostToolUse hook that writes `/logs/agent/checkpoints/<tool_use_id>.req` and waits for `.ack`. On local
Docker `/logs/agent` is bind-mounted from the trial dir, so the host-side watcher sees the request,
snapshots the container, writes a `CheckpointRecord` keyed by `tool_use_id`, and acks. `tool_use_id` is
the ATIF `tool_call_id`, so the join is exact. Full protocol: `docs/checkpoint-protocol.md`. Decisions:
`docs/decisions/`.

## Corpus versions

Every run is recorded in `corpus/manifests/<corpus_id>.json`: Harbor version, this repo's git sha, job
config, task list, model, attempts, timeout multiplier. Changing any of those means a new `corpus_id`.
Data lives outside git; see `corpus/README.md`.

## Layout

| Path | Purpose |
| --- | --- |
| `src/trajlab/contracts/` | shared pydantic models, no I/O; the only capture module analysis will import |
| `src/trajlab/capture/` | Harbor runs, corpus manifests, trial/container discovery |
| `src/trajlab/checkpoint/` | hook script, watcher, snapshot backends, join records |
| `src/trajlab/atif/` | load, enrich, validate trajectories |
| `configs/` | Harbor job configs, Claude Code hook settings, task subsets |
| `scripts/` | dated one-off experiments; never imported |
| `tests/fixtures/hello-world-trial/` | one real trial dir; all tests run against it |
| `docs/` | `harbor-facts.md`, `checkpoint-protocol.md`, `bootstrap.md`, `decisions/` |

## Status

- [ ] stock Harbor corpus on a Terminal-Bench subset (Sep 25)
- [ ] hook + watcher + docker_commit backend
- [ ] checkpoint join as ATIF system steps
- [ ] compaction recovery from native JSONL
- [ ] StateFork backend
- [ ] analysis modules (store, benchmark, evaluation): separate decision

## Contributing

See `CONTRIBUTING.md`. Working with Claude Code: it reads `CLAUDE.md`; start it with
"Read CLAUDE.md and docs/harbor-facts.md, then do build-order step N and stop."
