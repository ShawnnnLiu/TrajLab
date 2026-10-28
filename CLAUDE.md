# trajlab — context for Claude Code

Read this file, then `@docs/harbor-facts.md`, before writing or moving anything. Use the terms in `docs/glossary.md` with exactly the meanings given there. When a question about Harbor comes up, read the installed source in `.venv/lib/python3.12/site-packages/harbor/` before guessing; the facts file tells you which modules matter.

## What this repo is

Capture harness for COMS 6113 project #16 (Columbia, Fall 2026). We run Claude Code on Terminal-Bench tasks through Harbor, and record three things per trial: the ATIF trajectory Harbor already writes, an environment checkpoint at every tool call (which Harbor does not do), and a join between the two keyed by tool call id.
Capture is done (build-order steps 1-7 below).
Experiment 2 (`docs/experiments/exp2/`, ADR-0014) adds the analysis side: a store of each trial's steps joined to its filesystem deltas, a query tool over it, and a repair retry loop that compares the two.

Owner: Shawn Liu. Team of four. Course milestones: Sep 25 first baseline result (stock Harbor, no checkpoints), Oct 9 baseline demo, Dec 4 final paper.

## Non-negotiable constraints

1. **Harbor is a pinned dependency, never vendored, never patched in site-packages.** Every custom piece attaches from outside: custom environment via `--env module:Class`, custom agent by import path (subclass `harbor.agents.installed.claude_code.ClaudeCode`), Claude Code hooks via `--ak config=<settings.json>`, and post-processing of the trial directory. If you think Harbor needs a change, write it as a note in `docs/upstream-notes.md` and work around it.
2. **`src/trajlab/contracts/` holds pydantic models and nothing else.** No I/O, no imports from other trajlab modules.
   With `atif/`, it is the only capture code that analysis code (`store/`) imports.
3. **`capture/`, `checkpoint/`, `atif/` import each other only through `contracts/`.** Each exposes one entry point in `cli.py`.
4. **No trial data in git.** `corpus/jobs/`, `.env`, session JSONLs, notebook outputs are ignored. `corpus/manifests/` is committed.
5. **Anything that changes what a trial records needs an ADR** in `docs/decisions/` and a new `corpus_id` on the next run. Do not silently change the hook, watcher, backend, or postprocess output.
6. **Do not design analysis modules beyond `src/trajlab/store/` and `src/trajlab/groundtruth/`** (ADR-0014: the Exp 2 store; ADR-0013: ground truth by verifier replay).
   No `analysis/`, `bench/` directories yet.
   `store/` imports only `contracts/`, `atif/`, Harbor, and DuckDB, never `capture/` or `checkpoint/`; `groundtruth/` imports only `contracts/` and Harbor.
   If capture needs a shape that analysis will use, put the model in `contracts/` and stop.
7. **Reuse Harbor's models.** ATIF is `harbor.models.trajectories`; trial results are `harbor.models.trial.result.TrialResult`; paths are `harbor.models.trial.paths.TrialPaths`. Do not redefine them.

## Layout

```
configs/harbor/            one JSON job config per corpus version (the exact command that produced the data)
configs/claude-code/       settings.hooks.json: PostToolUse hook wiring
configs/tasks/             task-name lists per subset
src/trajlab/cli.py         typer app: run | watch | repair | postprocess | manifest | validate | store build|derive|export|query|serve | trace | gt
src/trajlab/contracts/     CheckpointRecord, TrialRecord, ATIF step extras, ProcessSnapshot/CallChanges, TrialLabels
src/trajlab/capture/       harbor_runner, corpus manifest, trial/container discovery, preinstall (custom environment, ADR-0008), trace site and labels (ADR-0015)
src/trajlab/checkpoint/    hook/ (PostToolUse hook script; delivery per ADR-0003), watcher, changes (ADR-0010), diffs and processes (--extras, ADR-0015), backends/, join
src/trajlab/atif/          load, compaction recovery, jobs (background-job notices, ADR-0020), postprocess, validate
src/trajlab/store/         Exp 2 store (ADR-0014): schema, steps, reconcile (deltas from checkpoint top layers, with write times), docker_images, build; derive (derived views, ADR-0018; step_jobs and delta_attribution, ADR-0020), describe (schema text), export (record-files bundle) (ADR-0018); query, query_child, serve, query.sh (the record-engine query tool, ADR-0019)
src/trajlab/groundtruth/   ground truth by verifier replay (ADR-0013): extract, replay, admission, counterfactual fixes, items
scripts/                   dated one-offs (2026-09-25_baseline.py); never imported by src/
tests/fixtures/hello-world-trial/   one small real trial dir; every test runs against it
corpus/manifests/          committed; corpus/jobs/ ignored
docs/decisions/            ADRs
docs/experiments/exp2/     Exp 2 motivation, design spec, technical spec (the build order for the store, query tool, retry loop)
```

## Conventions

- Python 3.12, `uv` for everything (`uv sync`, `uv run`, `uv add`). Never `pip install`.
- `ruff format` + `ruff check` via pre-commit. Line length 100. Type hints everywhere; `from __future__ import annotations` not needed on 3.12.
- CLI with `typer`; models with `pydantic` v2; file watching with `watchdog`; logging via `logging`, never `print`, in `src/`.
- Tests with `pytest`, offline, against `tests/fixtures/`. Nothing in `tests/` calls Docker, the network, or an API. Integration checks that need Docker live in `scripts/` or a `make` target and are documented as manual.
- Paths are `pathlib.Path` on the host and `PurePosixPath` for in-container paths, mirroring Harbor.
- Commit messages: imperative, one line, optional body. Branch names `<initials>/<topic>`. PR into `main` with one reviewer.
- A file is either a module in `src/` with tests, or a dated script in `scripts/`. If a script is run twice, promote it to a `cli.py` command.

## Build order (do these in sequence; each has an acceptance check)

1. **Scaffold**: layout above, `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example`, empty packages. Check: `uv sync && uv run trajlab --help` lists the five commands as stubs.
2. **Fixture**: run Harbor's hello-world task once on Docker (`harbor run -t hello-world/hello-world -a claude-code -m <model> -e docker`), copy the trial dir into `tests/fixtures/hello-world-trial/`, strip anything large or secret from `agent/sessions/`. Check: fixture < 1 MB, contains `result.json`, `lock.json`, `agent/trajectory.json`, `agent/sessions/`, `verifier/`.
3. **`atif/load.py` + `atif/validate.py`**: load the fixture into `harbor.models.trajectories.Trajectory`; validate wraps `harbor.utils.trajectory_validator`. Check: tests pass; `uv run trajlab validate tests/fixtures/hello-world-trial` exits 0.
4. **`contracts/`**: `CheckpointRecord`, `TrialRecord`, `CheckpointStepExtra`, `ContextManagementExtra`. Check: round-trip JSON tests.
5. **`capture/`**: `corpus.py` writes a manifest (corpus_id, harbor version from `importlib.metadata`, this repo's git sha, config path, task list, model, n_attempts, job dirs); `harbor_runner.py` shells out to `harbor run` with a config file and records the manifest; `discover.py` yields trial dirs and derives the compose project name from `session_id` (see facts file). Check: `uv run trajlab manifest corpus/jobs/<job>` produces a manifest for the fixture's parent.
6. **`checkpoint/`**: hook script, `settings.hooks.json`, watcher, `docker_commit` backend, join records. Protocol in `@docs/checkpoint-protocol.md`. Check: manual Docker run on hello-world produces `agent/checkpoints/checkpoints.jsonl` with one record per tool call and `.ack` for every `.req`.
7. **`atif/postprocess.py` + `atif/compaction.py`**: insert one system step per checkpoint after the agent step that owns the `tool_call_id`; insert `context_management` system steps from native JSONL compaction entries; write `agent/trajectory.enriched.json`; validator passes on the output. Check: tests on the fixture with a synthetic `checkpoints.jsonl`.

Capture ends at step 7. The formerly planned step 8 (`statefork` backend) was dropped by ADR-0004: `docker_commit` is the only backend.

Exp 2 comes after capture: the store, query tool, and retry loop are built in the order of `docs/experiments/exp2/exp2_technical_spec_trajlab_2026-10-18.md` §7, one step per PR, each with its acceptance check (ADR-0014 for the store).
Store tests run offline on synthetic layers; anything that needs Docker or the corpus is a dated script run by hand, with its command and output recorded in `docs/experiments/exp2/` or the ADR.
Ground truth (ADR-0013) also comes after capture: `trajlab gt extract | replay | try-fix | label | confirm | minimize | revert | items | report` write `groundtruth/` inside trial dirs, next to Harbor's files, and never modify Harbor's own files.
Replays and fix containers are admitted host-wide by declared CPUs and memory (`trajlab.groundtruth.admission`); never start verifier environments outside it.

Stop after each step and run `make test`. Do not start step N+1 with step N red.

## What not to do

- Do not run `harbor run` inside tests or on import. It costs money and needs Docker.
- Do not put API keys anywhere but `.env`; `harbor_runner.py` reads them from the environment and never logs them.
- Do not write a CRIU-based snapshot backend: per-call checkpoints are filesystem-only via `docker_commit` (ADR-0004).
  The one exception is ADR-0017: `capture/waypoint.py` runs a trial on Waypoint to make a single save of files and running programs for the Waypoint repair arms; do not extend it to per-call capture without a new ADR.
- Do not add a database or a query layer outside `store/` (ADR-0014), and do not add an LLM judge.
- Do not "clean up" Harbor's trial directory. Add files next to Harbor's; never rename or rewrite `trajectory.json`, `result.json`, or `lock.json`.

## When unsure

Read `@docs/harbor-facts.md`, then the Harbor source for the module it names, then ask. Prefer a question in the PR over a guess in the code.
