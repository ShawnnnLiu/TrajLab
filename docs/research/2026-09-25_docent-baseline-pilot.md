# Docent on stock Harbor output: setup check and TB 2.1 pilot

Date: 2026-09-25.
Status: pilot notes, not a decision.
This is a development run on an arm64 Mac with no ADR and no `corpus_id`, so none of it is corpus data.
The Terminal-Bench version is still pending its ADR (see `2026-09-24_tb-version-recommendation.md`).

## Setup

- Harbor 0.23.0 (from `uv.lock`), Docent 0.1.87 (via `uv run --with docent`, not in `pyproject.toml`).
- Docker Desktop on Apple Silicon, 7.7 GiB memory; two concurrent trials used about 300 MiB each.
- Claude Code with `anthropic/claude-sonnet-5` on a subscription token (`CLAUDE_FORCE_OAUTH=1`).
- Hosted Docent at docent.transluce.org.
- Ingest: `scripts/2026-09-25_docent_ingest.py` (PR #5).

## What was verified

1. `make test` passes after `uv sync`; `trajlab --help` lists the five commands (all still stubs).
2. Harbor smoke test (`hello-world/hello-world`) passes, reward 1.0 in 1m 14s.
   `claude-code.txt` shows `"apiKeySource":"none"`, so the subscription token was used, not a per-token API key.
3. Docent's Harbor importer, run offline on `tests/fixtures/hello-world-trial`:
   - a stock trial converts to one run with reward 1.0;
   - a trial with both `trajectory.json` and `trajectory.enriched.json` raises `ConversionError: expected exactly one ATIF JSON file under agent/, found 2`, so enriched trials need per-file import with `convert_atif_file_to_agent_run`;
   - a synthetic checkpoint system step keeps `trajlab.tool_call_id` in the message metadata (`atif_extra`).
4. The importer on a job with unfinished trials (a run stopped mid-way) does not fail: it loads only the trials that have a `result.json`.
5. The pass-rate DQL below returns the same value as Harbor on both uploaded jobs.

```sql
SELECT COUNT(*) AS runs,
  AVG(CAST(metadata_json->'harbor'->'result_json'->'verifier_result'->'rewards'->>'reward' AS FLOAT)) AS pass_rate
FROM agent_runs
```

## Harbor facts found along the way

- `harbor datasets list` in 0.23.0 only prints a link to https://hub.harborframework.com/datasets; it does not list datasets.
- TB 2.1 resolves through our pinned Harbor as `-d terminal-bench/terminal-bench-2-1`, locked to
  `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a` (open item 2 of the TB version recommendation).
  TB 2.0 is `terminal-bench/terminal-bench-2`.
- `result.json` fills `agent_result.cost_usd` even on a subscription token. It is Claude Code's estimate, not a bill, and it varies run to run
  (hello-world: $0.045 here vs $0.032 in the fixture). If cost is an experiment metric, report tokens alongside it.
- `--extra-instruction-path` and `-i/--include-task-name` exist in 0.23.0, so the history-feedback command on the architecture map is valid apart from its dataset name.

## TB 2.1 pilot

Command (job `tb21-pilot-v0`, stopped after 4 finished trials):

```
uv run harbor run -d terminal-bench/terminal-bench-2-1 -l 10 -k 2 -n 2 \
  -a claude-code -m anthropic/claude-sonnet-5 -e docker \
  -o corpus/jobs --job-name tb21-pilot-v0 --env-file .env -y
```

`-l 10` selected: dna-assembly, kv-store-grpc, openssl-selfsigned-cert, pypi-server, qemu-alpine-ssh, regex-chess,
schemelike-metacircular-eval, torch-pipeline-parallelism, torch-tensor-parallelism, write-compressor.

| Task | Reward | cost_usd (estimate) | Input tokens |
|---|---|---|---|
| kv-store-grpc | 1.0 | 0.12 | 300k |
| pypi-server | 1.0 | 0.14 | 388k |
| torch-tensor-parallelism | 1.0 | 0.19 | 245k |
| write-compressor | 1.0 | 0.72 | 704k |

Docent collection `d2400c11-9b84-405e-843c-5b8fc137b7cc`: DQL returns `runs=4, pass_rate=1.0`, matching Harbor.

A TB 2.0 run (`tb-baseline-v0`, 20 tasks x 3) was started first from the Docent comparison doc's runbook and stopped before any trial finished, once the Sep 25 brief's TB 2.1 recommendation was read.

## What this means

- The Harbor -> Docent path works end to end for stock trials. Experiment 1's analysis side needs no database of ours.
- 4/4 passes is a small sample, but it is consistent with the saturation concern in the TB version recommendation.
  The history-feedback conditions (raw logs, lessons) need failed attempts, so their task set should come from the 30-task hard tier or from observed failures, not from `-l N`.
- No trial failed, so nothing here tests arm64 breakage on TB 2.1. That scan is still open.
- At about 15-30 minutes per trial with two concurrent, a 20 x 3 run takes roughly 8-10 hours on a Mac. Full runs belong on the Linux server.

## Corrections to the Docent vs TrajectoryDB doc (Sep 24)

- Runbook steps 5 and 7: use `terminal-bench/terminal-bench-2-1`, not `terminal-bench@2.0`; `harbor datasets list` cannot confirm the name.
- Step 5 should be a small Mac pilot; the full baseline runs on the Linux server.
- "Where our contribution lives": `docker_commit` captures files only and cannot branch a live sandbox, which the project brief requires (Sep 25 brief). The part Docent cannot do depends on Waypoint on a root Linux host.
- "Experiment 1" is defined as the stock baseline in the doc and as the history-feedback loop on the architecture map. The team should pick one; the baseline can be condition A of the loop.

## Open items

1. Team: adopt TB 2.1 (ADR + `corpus_id`) and settle the definition of experiment 1.
2. arm64 scan of TB 2.1's 89 tasks, or keep all recorded runs on the Linux server.
3. Pick the task set for the history-feedback conditions from the hard tier.
4. Still untested: long runs and Task-tool (subagent) runs against Docent's importer, which rejects `continued_trajectory_ref` and subagent references; self-hosted Docent.
