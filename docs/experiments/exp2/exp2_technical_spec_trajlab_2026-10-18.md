# Technical spec: store, query tool, and retry loop for Exp 2 (trajlab)

Written 2026-10-18. For the Claude Code agent working in the trajlab repo. Read in this order: `exp2_motivation_2026-10-18.md` (why), `exp2_design_spec_2026-10-18.md` (the experiment), then this (what to build, where it goes, what it must guarantee, and in what order). Anything not settled here is settled with Shawn in the repo and recorded as an ADR in `docs/decisions/`, numbered after the highest ADR on `main`. Do not resolve an open question silently.

## 1. Scope

Three deliverables, in order:

1. **Store.** Turn a captured trial (ATIF enriched trajectory + `checkpoints.jsonl` + docker-commit images) into four tables in DuckDB, with per-step filesystem deltas and materialized derived columns. Due as the Oct 30 design diagram (schema and predicate interface) and the Nov 6 prototype.
2. **Query tool.** A read-only `query(sql)` the repair agent can call from inside the Harbor container, with every call logged.
3. **Retry loop.** Attempt -> verify -> ingest -> retry, fresh environment each time, three arms, binary failure signal. Reuses the Exp 1 repair job machinery, protocol prompt, and stop hook.

Out of scope: Waypoint or any non-filesystem state; LLM-backed derived columns; an NL2SQL front end; enforcement (policy pushed into the store); a human-facing UI; schema support for environments other than Terminal-Bench. Each of these is a later ADR, not a hidden feature.

## 2. Where it lives

Following `docs/` layout rules: capture never imports analysis; analysis imports `contracts/` and `atif/` only.

```
src/trajlab/
├── contracts/
│   ├── store.py          # TrialRow, StepRow, DeltaRow, CheckRow, DerivedSpec  (pydantic, no I/O)
│   └── exp2.py           # ArmConfig, AttemptResult, QueryLogEntry
├── store/
│   ├── schema.py         # DDL for the four tables + derived columns; one place, versioned
│   ├── build.py          # trial dir(s) -> store; calls reconcile + derive
│   ├── reconcile.py      # checkpoints.jsonl + images -> DeltaRow per (trial, step)
│   ├── derive.py         # derived-column implementations, registry keyed by name
│   ├── export.py         # store -> arm-2 file bundle (trials.jsonl, steps.jsonl, deltas/, README)
│   ├── query.py          # read-only connection, row cap, timeout, logging
│   └── checks.py         # loads verifier-replay blame from sl/groundtruth-replay output; never exported to agents
├── repair/               # extend what Exp 1 v2 created; do not fork it
│   ├── loop.py           # the retry state machine
│   ├── arms.py           # control | record-files | record-engine
│   ├── mount.py          # what goes into /logs/agent/record for each arm
│   └── querytool/        # container side of the query tool (see §4)
└── score/
    ├── outcomes.py       # repair rate at budget N, cost to first pass, all-zero fraction
    ├── localization.py   # touched_blamed_step from query log / file-read log
    └── diagnosis.py      # v2 §3 scoring, unchanged, plus diag_cites_record
cli.py                    # trajlab store build|export|query ; trajlab exp2 run|score|prereg
docs/experiments/exp2/    # preregistration.md, manifest, sample.md, run notes
```

## 3. Store

### 3.1 Tables

Schema from the 10-15 meeting notes, one row per entity, no repeated prompt or trajectory text:

- `trials` (trial_id, task, attempt_idx, parent_trial_id, prompt, model, harness, harness_version, reward, tokens_in, tokens_out, cost_usd, wall_ms)
- `steps` (trial_id, step_idx, tool_call_id, source, reasoning, tool, args_json, output, t_start, t_end, checkpoint_id)
- `deltas` (trial_id, step_idx, path, op, is_text, hunk, blob_ref, bytes_before, bytes_after)
- `checks` (trial_id, check_id, passed, blamed_step_idx, blame_source)

Rules:
- `tool_call_id` is the join key everywhere (ATIF `tool_call_id` == Claude Code `tool_use_id` == checkpoint name). Never re-derive it.
- `attempt_idx` 0 is the corpus first attempt; retries are 1..N with `parent_trial_id` pointing at the attempt they followed. The store for an Exp 2 chain is the first attempt plus every prior retry.
- Read-only steps (ADR-0001) have no checkpoint and therefore an empty delta set. Represent that as zero rows in `deltas`, not a sentinel row.
- `checks` is populated from the verifier-replay output and is **never** in any agent-visible export or connection. `query.py` opens connections with `checks` absent (separate database file, or a view-only schema; ADR).
- DDL lives in `schema.py` with a `SCHEMA_VERSION`; the store file records it. A schema change bumps the version and gets an ADR.

### 3.2 Reconciliation (`reconcile.py`)

Input: a trial dir with `agent/trajectory.enriched.json`, `agent/checkpoints/checkpoints.jsonl`, and the docker-commit images those records name. Output: `DeltaRow`s, one per changed path per mutating step.

Requirements:
- The delta for step k is the filesystem difference between checkpoint k-1 and checkpoint k (checkpoint 0 is the task's base image).
- `op` in {add, modify, delete, rename?}; `hunk` is a unified diff for text files; binaries get a `blob_ref` into a content-addressed blob dir and no hunk.
- Exclude paths that are never the agent's work: `/proc`, `/sys`, `/tmp` scratch, the `/logs` mount, Claude Code's own config dir. The exclusion list is one file and is recorded in the store's manifest.
- Idempotent and resumable: a trial already reconciled at the same `SCHEMA_VERSION` is skipped.
- Reports, per trial, whether the checkpoint chain is complete (every mutating step has an acked checkpoint). Incomplete chains are built but flagged; `exp2 run` refuses them.

**ADR needed:** how to compute the diff between two docker-commit images (export both and diff, or diff top layers), and the text/binary heuristic. Pick the simplest that is correct on the fixture, measure its time per step, and write it down.

### 3.3 Derived columns (`derive.py`)

A registry: `name -> (inputs, implementation, materialize_to)`. Each is a plain function over store rows, deterministic, versioned. Exp 2 ships:

- `steps.is_loop`: same `tool` and normalized `args_json` as an earlier step in the same trial, with zero delta rows.
- `steps.is_revert`: this step's delta on a path restores the content that an earlier step's delta on the same path replaced.
- `steps.files_touched`: count of delta rows.

Design constraint that outlasts Exp 2: the registry must admit a second implementation of the same name later (an LLM call) without the schema changing. So the column is the interface; the implementation is a registered function with a version string stored in the manifest.

**ADR needed:** the exact `args_json` normalization for `is_loop`, and whether derived columns are recomputed or appended on ingest of a retry.

### 3.4 Export (`export.py`)

Arm 2's bundle is produced **from the store**, never from the raw trial dir, so arms 2 and 3 carry the same bytes of information:

```
record/
├── README.md          # field names, one line each; no analysis hints
├── trials.jsonl
├── steps.jsonl        # includes derived columns
└── deltas/<trial_id>/<step_idx>/<path-with-slashes-escaped>.diff | .blob
```

`trajlab store export --verify` diffs the bundle back against the tables and fails on any mismatch. That check runs in CI on the fixture and before every Exp 2 run.

## 4. Query tool

### 4.1 Guarantees

- Read-only. Any statement that is not a single `SELECT`/`WITH` is rejected before execution.
- `checks` is not reachable.
- Row cap (default 200) and statement timeout (default 10 s); both recorded in the manifest.
- Every call is logged as a `QueryLogEntry` (attempt id, timestamp, SQL, rows returned, latency, error) to `/logs/agent/query_log.jsonl` in the trial dir.
- The agent gets the schema once, in the evidence sentence of the protocol prompt, as the output of `DESCRIBE` plus one example per table. Nothing else about how to use it.

### 4.2 Mechanism

Task images do not guarantee Python, jq, or network access, and the store should not be copied into the container. The repo already has a proven host/container channel: the bind-mounted `/logs/agent` directory and the `.req`/`.ack` protocol of the checkpoint hook. Recommended: the query tool is a shell script in the container that writes `<id>.sql` to `/logs/agent/queries/`, and the host-side watcher executes it against the store and writes `<id>.result` (rows as TSV or JSON) and `<id>.err`. Same watcher process, second file pattern. Expose it to Claude Code as a documented command (`/logs/agent/bin/query "SELECT ..."`), not as an MCP server.

**ADR needed:** confirm this against an MCP server on the host; decide result format and the cap behavior (truncate with a marker vs error).

### 4.3 Arm-2 file-read log

For localization scoring, arm 2 needs the equivalent of the query log: which record files the agent opened. Derive it after the fact from the ATIF trajectory (Read/Grep/Bash steps whose args reference `/logs/agent/record/`). No extra instrumentation in the container.

## 5. Retry loop

### 5.1 State machine (`loop.py`)

Per (failure, arm):

```
attempt_idx = 0  (corpus first attempt; reward known = 0)
while attempt_idx < N:
    attempt_idx += 1
    trial = run_attempt(task, arm, record=store_for(chain))   # fresh env, fresh session
    reward = trial.reward                                       # Harbor verifier, hidden suite
    log AttemptResult
    if reward == 1: break
    if arm != control: ingest(trial) -> store; re-export arm-2 bundle
```

- `run_attempt` is a Harbor trial on the same task, in a new container, with the Exp 1 v2 protocol prompt (the "same environment" paragraph rewritten for a fresh environment), the v2 stop hook, and the arm's mount under `/logs/agent/record/` (arm 2: the bundle; arm 3: the query tool and nothing else; control: nothing).
- The failure signal is the string FAILED in the prompt. No check names, no verifier text, in any arm. Assert this in a test that greps the rendered prompt.
- Agent-run tests inside an attempt are ordinary tool calls. Only the harness verifier ends an attempt.
- One chain per (failure, arm). No repeats.
- Resumable: a chain interrupted mid-way restarts at its last completed attempt from the on-disk `AttemptResult` log.

### 5.2 Ingest

After a failed retry: `postprocess` the new trial (existing command), reconcile its deltas, derive columns, append to the chain's store, re-export. Target: under one minute per attempt. If reconciliation of a retry fails, the chain stops and is marked incomplete; it does not continue with a partial store.

### 5.3 Manifest and pre-registration

`trajlab exp2 prereg` writes `docs/experiments/exp2/preregistration.md` with: sample (usable failures and exclusion reasons), N, arms, primary comparison (arm 3 vs arm 2 at N=3, paired), test, derived-column list with implementation versions, protocol prompt hash, stop-hook version, query-tool caps, model and effort, `SCHEMA_VERSION`, repo sha. It must be committed before the first arm-3 attempt runs; `exp2 run` refuses to start arm 3 if the file's sha is not in `git log`.

## 6. Scoring (`score/`)

- `outcomes.py`: repair rate at N=1,2,3 per arm; cost to first pass (tokens, tool calls, wall) among solved; total cost per solve; all-zero fraction. Paired table by failure. The statistical test runs in a notebook, not in `src/`.
- `localization.py`: `touched_blamed_step` per attempt. Arm 3: any logged query whose result rows include the blamed `(trial_id, step_idx)`. Arm 2: any file read under `record/` whose path names the blamed step's delta or whose `steps.jsonl` read covers it. Blame comes from `checks.py`, loaded from the ground-truth branch output.
- `diagnosis.py`: v2 §3 unchanged, plus `diag_cites_record`.

## 7. Build order and acceptance

| Step | Deliverable | Done when | Target |
|---|---|---|---|
| 1 | `contracts/store.py`, `schema.py`, `reconcile.py` on the fixture | fixture trial builds; delta count matches a hand count; chain-completeness flag correct | Oct 24 |
| 2 | `build.py` over the 69-trial corpus | usable-failure count written to `docs/experiments/exp2/sample.md`; per-trial reconcile time reported | Oct 28 |
| 3 | `derive.py` with the three columns; `export.py --verify` | CI green on fixture; export round-trips | Oct 30 (design diagram) |
| 4 | Query tool end to end in a Harbor container | a dry-run attempt issues a query and the host answers it; log entry present; non-SELECT rejected | Nov 4 |
| 5 | `loop.py`, `arms.py`, `mount.py`; `exp2 prereg` | 3 failures x 3 arms x N=1 dry run; prompts grep clean of check names | Nov 6 (prototype) |
| 6 | `score/` | dry-run outputs scored; `touched_blamed_step` computed for both record arms | Nov 10 |
| 7 | Full run | all usable failures, 3 arms, N=3 | mid-Nov |

Tests run offline against `tests/fixtures/hello-world-trial/` only. Steps 2, 4, and 7 touch Docker and the corpus and are run by hand with their commands and shas recorded in `docs/experiments/exp2/`.

## 8. Open questions for ADRs

Numbered here for reference; the ADR gets the next free number on `main`.

1. Diff method between consecutive docker-commit images, and the text/binary rule.
2. `args_json` normalization for `is_loop`; recompute vs append on ingest.
3. Query tool mechanism: file channel through the existing watcher vs MCP server; result format; cap behavior.
4. How `checks` is kept out of agent-visible connections: separate file vs restricted schema.
5. Path exclusion list for reconciliation.
6. Fresh environment per retry (recommended) vs resume in the failed environment, for all arms. Experiment design decision; needs Shawn, and the Exp 2 design spec updated to match. **Decided 2026-10-19 by Shawn: fresh environment in every arm** (design spec §2 updated).
7. Whether derived columns appear in the arm-2 bundle (recommended yes, so the comparison isolates the engine).

## 9. Invariants (test these)

- No agent-visible artifact contains `checks` or the name of any failing check.
- Arm 2 bundle and arm 3 tables are information-equal (`export --verify`).
- Rendered prompts contain FAILED and no verifier text.
- Every arm-3 attempt has a query log, even if empty.
- `tool_call_id` round-trips: trajectory step -> checkpoint record -> delta rows -> back.
- A chain's store contains exactly attempts 0..t-1 when attempt t runs.
