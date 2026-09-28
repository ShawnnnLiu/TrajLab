# trajlab

Capture harness for COMS 6113 project #16: Terminal-Bench trials with Claude Code, recorded as ATIF
(Agent Trajectory Interchange Format) trajectories with per-tool-call environment checkpoints, for
later analysis of where agent trials go wrong. Terms used here are defined in `docs/glossary.md`.

Built on [Harbor](https://github.com/harbor-framework/harbor). Harbor runs the agent in a sandbox,
verifies the result, and writes the trajectory. This repo adds three things Harbor does not do:
checkpoint the sandbox at every state-mutating tool call, join each checkpoint to the trajectory step
that caused it, and recover context-compaction boundaries that Harbor's Claude Code converter drops.

## What you get per trial

```
corpus/jobs/<job>/<task>__<id>/
├── result.json                     reward, tokens, cost, timings, exception      (Harbor)
├── lock.json                       task sha256, agent version, env config        (Harbor)
├── agent/trajectory.json           ATIF-v1.7                                     (Harbor)
├── agent/sessions/                 Claude Code native session JSONL              (Harbor)
├── agent/checkpoints/              <tool_call_id>.req/.ack, checkpoints.jsonl    (trajlab)
├── agent/trajectory.enriched.json  ATIF + checkpoint and compaction system steps (trajlab)
└── verifier/, artifacts/                                                          (Harbor)
```

## Quickstart

```bash
brew install --cask docker && open -a Docker   # macOS; run in your own terminal, it asks for a password
uv sync                          # Python 3.12, harbor pinned, everything in uv.lock
cp .env.example .env             # add CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or an API key
docker info                      # Docker must be running

# 1. Stock corpus, no checkpoints
uv run trajlab run configs/harbor/tb_subset_v0.json

# 2. Same run with checkpoints: watcher first, then the run with hook settings
uv run trajlab watch corpus/jobs --backend docker_commit
uv run trajlab run configs/harbor/tb_subset_v0.json --hooks configs/claude-code/settings.hooks.json

# 3. Postprocess and validate
uv run trajlab postprocess corpus/jobs/<job>
uv run trajlab validate corpus/jobs/<job>
```

## Authentication

Claude Code inside the container can run on a **Claude subscription** or an **API key**. The
subscription is the default here: run `claude setup-token`, put the token in `.env` as
`CLAUDE_CODE_OAUTH_TOKEN`, and keep `CLAUDE_FORCE_OAUTH=1` so Harbor drops any API key your shell
happens to export. Subscription usage counts against your plan's rate limits rather than a bill, and
one account's limits are shared by every concurrent trial. To use an API key instead, set
`ANTHROPIC_API_KEY` and unset `CLAUDE_FORCE_OAUTH`. After the first trial, confirm with
`grep apiKeySource <trial>/agent/claude-code.txt`; `"ANTHROPIC_API_KEY"` means the key was billed.
Full setup, including installing Docker, is in `docs/bootstrap.md`.

## How checkpointing works

Three roles cooperate (definitions in `docs/glossary.md`). The **hook**, a Claude Code PostToolUse
hook registered by `settings.hooks.json` and running inside the container, writes
`/logs/agent/checkpoints/<tool_use_id>.req` after each state-mutating tool call and waits for `.ack`.
The **watcher**, a host process started by `trajlab watch`, sees the request through the bind mount
(`/logs/agent` is the trial dir's `agent/`) and asks the **backend** (`docker_commit`) to snapshot the
container. The watcher then writes a `CheckpointRecord` keyed by `tool_use_id` and acks. Harbor itself
fires no hook during the agent phase, which is why the hook is Claude Code's. `tool_use_id` is the
ATIF `tool_call_id`, so the join is exact. Full protocol: `docs/checkpoint-protocol.md`. Decisions:
`docs/decisions/`.

## Corpus versions

Every corpus is recorded in `corpus/manifests/<corpus_id>.json`: Harbor version, this repo's git sha,
job config, task list, model, attempts, timeout multiplier. Changing any of those means a new
`corpus_id`. Data lives outside git; see `corpus/README.md`.

## Layout

| Path | Purpose |
| --- | --- |
| `src/trajlab/contracts/` | shared pydantic models, no I/O; the only capture module analysis will import |
| `src/trajlab/capture/` | Harbor runs, corpus manifests, trial/container discovery |
| `src/trajlab/checkpoint/` | hook script, watcher, snapshot backends, join records |
| `src/trajlab/atif/` | load, postprocess, validate trajectories |
| `configs/` | Harbor job configs, Claude Code hook settings, task subsets |
| `scripts/` | dated one-off experiments; never imported |
| `tests/fixtures/hello-world-trial/` | one real trial dir; all tests run against it |
| `docs/` | `glossary.md`, `harbor-facts.md`, `checkpoint-protocol.md`, `bootstrap.md`, `project-brief.md` |
| `docs/decisions/` | ADRs |
| `docs/research/`, `docs/meetings/` | dated research reports and meeting briefs; historical record |

## Status

- [x] environment, scaffold, hello-world fixture (Sep 22)
- [ ] stock Harbor corpus on a Terminal-Bench subset (Sep 25)
- [ ] hook + watcher + docker_commit backend
- [ ] checkpoint join as ATIF system steps
- [ ] compaction recovery from native JSONL
- ~~StateFork backend~~ dropped: filesystem-only checkpoints (ADR-0004)
- [ ] analysis modules (store, benchmark, evaluation): separate decision

## Contributing

See `CONTRIBUTING.md`. Working with Claude Code: it reads `CLAUDE.md`; start it with
"Read CLAUDE.md and docs/harbor-facts.md, then do build-order step N and stop."
