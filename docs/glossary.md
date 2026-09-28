# Glossary

The vocabulary pool for this repo.
Docs, code comments, PRs, and meetings use these terms with exactly these meanings.
Each entry gives the canonical term, its definition, and, where drift exists, a "Not:" line naming the synonyms to avoid.
If a doc needs a word this file does not have, add it here in the same PR.

## Run hierarchy

The containment chain, largest to smallest, is: corpus > job > trial.
One trial is one task executed by one attempt.

- **task** - one Terminal-Bench task (e.g. `hello-world/hello-world`): the unit Harbor executes and verifies.
- **attempt** - one of the `n_attempts` repetitions of a task within a job.
  Say "attempt" when you mean the repetition index, "trial" when you mean the execution itself.
- **trial** - one execution of one task by one attempt.
  Harbor writes one trial dir per trial.
- **trial dir** - `corpus/jobs/<job>/<task>__<id>/`, laid out by Harbor's `TrialPaths`
  (`result.json`, `lock.json`, `agent/`, `verifier/`, `artifacts/`).
- **`trial_id`** - the UUID Harbor assigns a trial, in `result.json`.
- **`trial_name`** - the `<task>__<id>` directory name, from `config.json`.
  It also seeds Harbor's environment `session_id` (see below).
- **job** - one `harbor run` invocation over a task list; one directory under `corpus/jobs/` containing trial dirs.
- **corpus** - the set of trials recorded under one `corpus_id`: one job config plus one set of capture settings.
  A corpus may span more than one job dir (the manifest lists them).
- **`corpus_id` / corpus manifest** - `corpus/manifests/<corpus_id>.json`: Harbor version, repo sha, job config, task list, model, attempts, timeout multiplier, job dirs, storage location.
  Changing anything that affects what a trial records means a new `corpus_id` (see `CONTRIBUTING.md`).
- **run** - reserved for the CLI commands `trajlab run` and `harbor run`.
  Not: "run" meaning a trial, an attempt, or a corpus; say those words instead.
- **stock** - a run with no hooks and no watcher: unmodified Harbor behavior, checkpoints absent.

## Checkpoint machinery

- **checkpoint** - the environment state captured for one tool call, plus the `CheckpointRecord` describing it.
  With the `docker_commit` backend the state is a Docker image; the record's `checkpoint_id` is the image id.
  Keyed by `tool_use_id`.
- **snapshot** - the *act* the backend performs to take a checkpoint (`SnapshotBackend.snapshot()`).
  Use "snapshot" only for the action; the resulting artifact and record are a checkpoint.
  (Accepted ADRs sometimes say "snapshot" for the artifact; they are historical records and stay as written.)
- **physical / virtual** - a physical snapshot copies the state in full; a virtual snapshot is a copy-on-write reference (StateFork's terminology).
  Every trajlab checkpoint is physical (ADR-0002, ADR-0004).
- **hook** - unqualified, the Claude Code `PostToolUse` hook registered by `configs/claude-code/settings.hooks.json`, running inside the container.
  It writes `<tool_use_id>.req` and waits for `.ack`.
  Qualify the other senses: the **hook script** (`post_tool_use.sh`), the **`--hooks` flag** (`trajlab run --hooks`), and **Harbor lifecycle hooks** (trial events; none fire during the agent phase, which is why we use Claude Code's).
- **watcher** - the host process started by `trajlab watch <jobs-dir> --backend <name>`.
  It sees `.req` files through the bind mount, resolves the trial's container, calls the backend, appends to `checkpoints.jsonl`, and writes `.ack`.
  "Host-side watcher" means this process, not the snapshots it takes.
- **backend** - unqualified, a trajlab snapshot backend implementing `SnapshotBackend`.
  The only backend is `docker_commit` (ADR-0004).
  Write `docker_commit` (code) for the backend name and `docker commit` (two words) for the Docker command it wraps.
- **`.req` / `.ack` / `.timeout`** - the request, acknowledgement, and give-up marker files under `<trial>/agent/checkpoints/`; see `docs/checkpoint-protocol.md`.
- **`checkpoints.jsonl`** - append-only, one `CheckpointRecord` per line, in capture order; written by the watcher.

## Trajectory

- **ATIF** - Agent Trajectory Interchange Format, Harbor RFC 0001; models in `harbor.models.trajectories`.
  Harbor's Claude Code converter stamps `ATIF-v1.7`; the schema accepts v1.7 and v1.8, so never assert v1.8 on Harbor's file.
- **`trajectory.json`** - Harbor's ATIF output in `agent/`; never modified by trajlab.
- **native session JSONL** - Claude Code's own log at `agent/sessions/projects/<cwd-slug>/<session-id>.jsonl`.
  "Native" always means "written by Claude Code itself", as opposed to Harbor's converted ATIF.
- **step / agent step / system step** - an ATIF step has `source ∈ {system, user, agent}`.
  Postprocess inserts *system* steps for checkpoints and compaction (ADR-0003).
  ("Step" also names build-order steps in `CLAUDE.md`; context disambiguates, but prefer "build-order step" in prose.)
- **`tool_use_id` / `tool_call_id`** - the same id: Claude Code's `tool_use_id` (session JSONL, hook stdin) is the ATIF `tool_call_id`.
  It is the join key naming the trajectory step, the native event, and the checkpoint.
  `source_call_id` is the field on an ATIF observation result that points back at the `tool_call_id` it answers.
- **`session_id`** - two unrelated ids; always qualify which:
  - **Claude Code session id** - names the native session JSONL, arrives in the hook's stdin, and is the ATIF root `session_id`.
  - **Harbor environment session id** - `"<trial_name>__agent"`, e.g. `hello-world__bZZeEkw__agent`; the sanitized source of the compose project name.
- **compaction** - Claude Code summarizing its context when it nears the limit.
  Harbor's converter drops the boundary, so postprocess recovers it from the native JSONL into `context_management` system steps.
- **compose project** - the Docker Compose project Harbor names by sanitizing the environment session id; the main container is `<project>-main-1`.

## Postprocessing

- **postprocess** - the canonical verb and the CLI command `trajlab postprocess`: insert checkpoint and compaction system steps, write the enriched trajectory.
  Not: "enrich" as a verb.
- **enriched trajectory** - `agent/trajectory.enriched.json`, the postprocess output; valid ATIF.
  The only sanctioned use of "enrich*".
- **join** - the association between a checkpoint and the trajectory step that caused it, keyed by `tool_use_id`; realized as ATIF system steps (ADR-0003).
  Steps of read-only tools join to the most recent earlier checkpoint (ADR-0001); their representation in the enriched file is an open question (see `docs/checkpoint-protocol.md`).

## Everything else

- **ADR** - Architecture Decision Record, in `docs/decisions/`.
  Accepted ADRs are historical: amend, do not rewrite.
- **capture** - this project's current phase (versus the later analysis phase); also the act of taking a checkpoint (`capture_ms`, "capture order").
- **contracts** - `src/trajlab/contracts/`: the shared pydantic models, and the only capture module analysis code will import.
- **fixture** - `tests/fixtures/hello-world-trial/`: one real, trimmed trial dir; every offline test runs against it.
- **bind mount** - Harbor mounts the trial's `agent/` dir at `/logs/agent` in the container, so container writes appear on the host immediately; this is the hook-to-watcher channel.
- **manifest** - unqualified, the corpus manifest.
  Qualify Harbor's `artifacts/manifest.json` and any dataset manifest.
- **state-mutating / read-only tool** - state-mutating: `Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` (the hook matcher; checkpointed).
  Read-only: `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Task` (not checkpointed).
  ADR-0001 is the authoritative list; do not restate a different one.
- **timeout multiplier** - Harbor's `--timeout-multiplier`, scaling each task's agent timeout; checkpointed runs need a larger one, recorded in the corpus manifest.
