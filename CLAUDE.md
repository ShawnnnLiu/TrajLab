# trajlab — context for Claude Code

Read this file, then `@docs/harbor-facts.md`, before writing or moving anything. When a question about Harbor comes up, read the installed source in `.venv/lib/python3.12/site-packages/harbor/` before guessing; the facts file tells you which modules matter.

## What this repo is

Capture harness for COMS 6113 project #16 (Columbia, Fall 2026). We run Claude Code on Terminal-Bench tasks through Harbor, and record three things per trial: the ATIF trajectory Harbor already writes, an environment checkpoint at every tool call (which Harbor does not do), and a join between the two keyed by tool call id. A later phase adds analysis (store, question benchmark, evaluation). This phase is capture only.

Owner: Shawn Liu. Team of four. Course milestones: Sep 25 first baseline result (stock Harbor, no checkpoints), Oct 9 baseline demo, Dec 4 final paper.

## Non-negotiable constraints

1. **Harbor is a pinned dependency, never vendored, never patched in site-packages.** Every custom piece attaches from outside: custom environment via `--env module:Class`, custom agent by import path (subclass `harbor.agents.installed.claude_code.ClaudeCode`), Claude Code hooks via `--ak config=<settings.json>`, and post-processing of the trial directory. If you think Harbor needs a change, write it as a note in `docs/upstream-notes.md` and work around it.
2. **`src/trajlab/contracts/` holds pydantic models and nothing else.** No I/O, no imports from other trajlab modules. It is the only module the future analysis code will import from capture.
3. **`capture/`, `checkpoint/`, `atif/` import each other only through `contracts/`.** Each exposes one entry point in `cli.py`.
4. **No trial data in git.** `corpus/jobs/`, `.env`, session JSONLs, notebook outputs are ignored. `corpus/manifests/` is committed.
5. **Anything that changes what a trial records needs an ADR** in `docs/decisions/` and a new `corpus_id` on the next run. Do not silently change the hook, watcher, backend, or postprocess output.
6. **Do not design analysis modules.** No `store/`, `analysis/`, `bench/` directories yet. If capture needs a shape that analysis will use, put the model in `contracts/` and stop.
7. **Reuse Harbor's models.** ATIF is `harbor.models.trajectories`; trial results are `harbor.models.trial.result.TrialResult`; paths are `harbor.models.trial.paths.TrialPaths`. Do not redefine them.

## Layout

```
configs/harbor/            one JSON job config per corpus version (the exact command that produced the data)
configs/claude-code/       settings.hooks.json: PostToolUse hook wiring
configs/tasks/             task-name lists per subset
src/trajlab/cli.py         typer app: run | watch | postprocess | manifest | validate
src/trajlab/contracts/     CheckpointRecord, TrialRecord, ATIF step extras
src/trajlab/capture/       harbor_runner, corpus manifest, trial/container discovery
src/trajlab/checkpoint/    hook/ (script copied into the container), watcher, backends/, join
src/trajlab/atif/          load, compaction recovery, postprocess, validate
scripts/                   dated one-offs (2026-09-25_baseline.py); never imported by src/
tests/fixtures/hello-world-trial/   one small real trial dir; every test runs against it
corpus/manifests/          committed; corpus/jobs/ ignored
docs/decisions/            ADRs
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
8. **`statefork` backend**: same interface as `docker_commit`, attach mode. Only after 6 works end to end.

Stop after each step and run `make test`. Do not start step N+1 with step N red.

## What not to do

- Do not run `harbor run` inside tests or on import. It costs money and needs Docker.
- Do not put API keys anywhere but `.env`; `harbor_runner.py` reads them from the environment and never logs them.
- Do not write a `WaypointEnvironment` yet. It is a later ADR.
- Do not add a database, a query layer, or an LLM judge. That is analysis.
- Do not "clean up" Harbor's trial directory. Add files next to Harbor's; never rename or rewrite `trajectory.json`, `result.json`, or `lock.json`.

## When unsure

Read `@docs/harbor-facts.md`, then the Harbor source for the module it names, then ask. Prefer a question in the PR over a guess in the code.
