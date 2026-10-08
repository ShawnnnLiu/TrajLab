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
  In Exp 2, `attempt_idx` is instead a position in a retry chain (see "Exp 2 store" below): 0 is the corpus first attempt.
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

- **checkpoint** - the environment state captured after a tool call, plus the `CheckpointRecord` describing it; it covers that call and any calls deferred since the previous checkpoint (ADR-0007).
  With the `docker_commit` backend the state is a Docker image; the record's `checkpoint_id` is the image id.
  Keyed by `tool_use_id`.
- **snapshot** - the *act* the backend performs to take a checkpoint (`SnapshotBackend.snapshot()`).
  Use "snapshot" only for the action; the resulting artifact and record are a checkpoint.
  (Accepted ADRs sometimes say "snapshot" for the artifact; they are historical records and stay as written.)
- **physical / virtual** - a physical snapshot copies the state in full; a virtual snapshot is a copy-on-write reference (StateFork's terminology).
  Every trajlab checkpoint is physical (ADR-0002, ADR-0004).
- **hook** - unqualified, the Claude Code hook registered for `PostToolUse` and `PostToolUseFailure` by `configs/claude-code/settings.hooks.json`, running inside the container.
  It writes `<tool_use_id>.req` and waits for `.ack`.
  Qualify the other senses: the **hook script** (`post_tool_use.sh`, delivered inline as the settings file's `command`), and **Harbor lifecycle hooks** (trial events; none fire during the agent phase, which is why we use Claude Code's).
- **watcher** - the host process started by `trajlab watch <jobs-dir> --backend <name>`.
  It sees `.req` files through the bind mount, resolves the trial's container, calls the backend, appends to `checkpoints.jsonl`, and writes `.ack`.
  "Host-side watcher" means this process, not the snapshots it takes.
- **backend** - unqualified, a trajlab snapshot backend implementing `SnapshotBackend`.
  The only backend is `docker_commit` (ADR-0004).
  Write `docker_commit` (code) for the backend name and `docker commit` (two words) for the Docker command it wraps.
- **`.req` / `.ack` / `.timeout`** - the request, acknowledgement, and give-up marker files under `<trial>/agent/checkpoints/`; see `docs/checkpoint-protocol.md`.
- **every-N / `--every N`** - the watcher takes a checkpoint on every Nth state-mutating call of a trial (ADR-0007); N is recorded in each trial's `policy.json` and in the corpus manifest as `checkpoint_every`.
- **gate / change-gated** - which hooked calls get a checkpoint (`--gate`, ADR-0010): `change` checkpoints only calls that changed the filesystem; `audit` checkpoints all of them but records the change verdict; `none` is every-N (ADR-0007).
- **unchanged call** - a hooked call after which the container's filesystem equals the last checkpoint's, outside the harness exclusions; it gets no checkpoint, and its `CallRecord` names the previous one.
  Not: "minor" or "small" call; the paper never classifies calls by size.
- **stop checkpoint / stop record** - the checkpoint or `CallRecord` for the end of the agent's turn (`Stop` or `StopFailure`), with `trigger: "stop"` and a `stop_<epoch>_<pid>` id instead of a `tool_use_id` (ADR-0011).
- **baseline** - the first measured call of a trial (or after a watcher restart), always checkpointed because there is nothing to compare it with.
- **call record** - the `CallRecord` the watcher writes per answered hooked call, in `calls.jsonl` and `.ack`.
- **covered call** - a state-mutating call whose effects first appear in a given checkpoint; listed in that record's `covered_tool_call_ids`.
- **deferred ack** - the `.ack` the watcher writes, without a snapshot, for a call that does not reach N; its effects appear in the next checkpoint.
- **`checkpoints.jsonl`** - append-only, one `CheckpointRecord` per line, in capture order; written by the watcher.
  Records are keyed by `trial_name`, not `trial_id`, because a running trial has no trial id on disk (ADR-0006).
- **watcher lock** - `<jobs-dir>/.trajlab-watcher.lock`, held with `flock` by the one watcher of a jobs dir; `trajlab run` checks it before starting a job whose config enables hooks.

- **task image / derived image** - the task image is what Harbor would run the trial on, pulled (`docker_image`) or built from the task's `environment/`; the derived image is the task image plus Harbor's agent install, made and cached by `PreinstalledDockerEnvironment` as `trajlab-preinstalled:<tag>` (ADR-0008).
  "Pre-install" names the whole mechanism.
  Not: "base image", which the Dockerfile `FROM` line already means.

- **Waypoint session** - one Waypoint workspace: a first attempt's environment and its save, under `/srv/trajlab/waypoint/sessions/<id>/` (ADR-0016).
  Always "Waypoint session", never bare "session", which names Claude Code's or Harbor's.
- **save** - Waypoint's copy of an environment's files and running programs at one moment; trajlab makes one per first attempt, `final`, after the agent stops and before the tests (ADR-0016).
  A save is a checkpoint with programs; say "save" only for Waypoint's.
- **fork** - a live copy of a save; each `state-files`, `state-live`, or `state-live-traj` repair runs in its own fork.

- **pin** - `trajlab.capture.pins.CLAUDE_CODE_VERSION`, the one Claude Code version every capture runs (ADR-0009); enforced by `trajlab run`, by the derived-image build, and in every trial container.

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
  - **Harbor environment session id** - `"<trial_name>__env"`, e.g. `hello-world__bZZeEkw__env`; the sanitized (lowercased) source of the compose project name.
    Not `"<trial_name>__agent"`, which is the session id Harbor gives the agent object and names no container.
- **compaction** - Claude Code summarizing its context when it nears the limit.
  Harbor's converter drops the boundary, so postprocess recovers it from the native JSONL into `context_management` system steps.
- **compose project** - the Docker Compose project Harbor names by sanitizing the environment session id; the main container is `<project>-main-1`.

## Postprocessing

- **postprocess** - the canonical verb and the CLI command `trajlab postprocess`: insert checkpoint and compaction system steps, write the enriched trajectory.
  Not: "enrich" as a verb.
- **enriched trajectory** - `agent/trajectory.enriched.json`, the postprocess output; valid ATIF.
  The only sanctioned use of "enrich*".
- **join** - the association between a checkpoint and the trajectory step that caused it, keyed by `tool_use_id`; realized as ATIF system steps (ADR-0003).
  Steps of read-only tools join to the most recent earlier checkpoint (ADR-0001), which in the enriched file is the last checkpoint step before them; each hooked call's `CallRecord` sits on its step as `extra.trajlab.calls` (ADR-0003, 2026-10-01 amendment).
- **compaction record** - the `CompactionRecord` postprocess builds from one native `compact_boundary` event; it is the `observation.results[0].extra` of a compaction step.

## Exp 2 store

- **store** - the Exp 2 store (ADR-0014): a directory holding `record.duckdb`, `heldout.duckdb`, and `blobs/`.
  Not: "database" for the `record-files` export, which is files.
- **record of prior attempts** - what a record arm gives the repair agent: the store's agent-visible tables (`trials`, `steps`, `deltas`, and `store_meta`), through the query tool (`record-engine`) or as exported files (`record-files`).
  Always qualify it ("record of prior attempts", "record arm"); unqualified, "record" names a capture record (checkpoint record, call record, stop record, compaction record).
- **held-out tables** - `checks`, `faults`, `blame`, and `chains` in `heldout.duckdb`; never opened by an agent connection or exported to an agent.
- **step row** - one row of `steps`: a tool call, an ATIF step without tool calls, a compaction step, or a stop record (ADR-0014, item 4).
  Not the same as an ATIF step: a step with k tool calls gives k step rows.
- **start image** - the image a trial's container started from: its derived image, or for a trial resumed from a checkpoint, that checkpoint image.
  Every checkpoint image of the trial is its start image plus at most one layer.
  A trial run without pre-install starts from its task image, but `trajlab.store.build.start_image` names only the first two (ADR-0014, known limits).
  Not: "base image" (see derived image).
- **top layer** - the layer a `docker commit` added over the start image: the container's whole writable layer when it was committed, so it holds every change since the container started; empty when the commit added no layer (a container with no changes).
- **view** - the filesystem at a checkpoint: the start image with that checkpoint's top layer applied by the OCI rules (whiteouts, opaque directories, non-directory members replacing subtrees).
- **delta** - the delta rows of one checkpoint: the paths whose kind, mode, owner, or content differ between the previous checkpoint's view (the start image, for the first) and this one's; keyed to the step row that owns the checkpoint.
  Not: the detector's `changed_paths` in a call record, which compare change times and are capped.
- **checkpoint chain** - a trial's checkpoints in `seq` order; complete when every hooked call that ran has a call record and every checkpoint image was read (ADR-0014, item 8).
- **retry** - one more attempt at a failed task in Exp 2, started after the harness verifier scored the previous attempt; tests the agent runs inside an attempt are not retries.
- **retry chain** - a failure's attempts under one Exp 2 arm, in order: attempt 0 (the corpus first attempt), then retries 1 to N, each naming the one before in `parent_trial_id`.
  Not to be confused with a checkpoint chain.

## Detector capture and labels (ADR-0015)

- **call input** - `<tool_use_id>.call.json` beside the `.req`: the hook's whole stdin (the call's input, its output, and its ids), written on every hooked call.
- **extras** - what `trajlab watch --extras` adds per answered call, host-side under `<trial>/trajlab/`: a process snapshot and the call's changes.
  The call's changes need a filesystem listing, so `--gate change` or `audit`.
- **process snapshot** - one `ProcessSnapshot` in `processes.jsonl`: the container's processes and listening TCP ports when the watcher answered a call.
  Not a snapshot in the backend's sense (see snapshot); it is never a checkpoint.
- **call changes** - one `CallChanges` in `changes.jsonl`: for each file a call changed, the op, size, sha256 before and after, and a unified diff for text, within the limits of `checkpoint/diffs.py`.
  Not: a delta, which the Exp 2 store computes between checkpoint views.
- **pattern** - one of ADR-0015's seven failure patterns (`loop`, `no_effect_write`, `false_edit_claim`, `false_test_claim`, `unverified_completion`, `server_state`, `forbidden_action`); `stall` is labeled beside them but reported separately.
- **flag** - the detector's claim that a pattern occurred at a `tool_call_id` (ADR-0015 §3, phase 2).
- **label / labeler** - a person's judgment, in `trajlab trace`, of whether a pattern occurred at a call or in a trial, saved per labeler as `TrialLabels` under `corpus/labels/` (git-ignored).
  Not: a Docker label, or the gt labeler (ADR-0013), which is an agent.

## Ground truth (ADR-0013)

- **timeline / point** - a trial's ordered points: `initial` (the derived image it started from), one per checkpoint, and `final` (Harbor's recorded `artifacts/`); `P<i>` names point `i`.
- **artifact state / `state_id`** - the declared (graded) artifacts' bytes at a point, collected as Harbor collects them; `state_id` hashes the canonical listing. Points with the same bytes share a state.
- **replay** - one run of a task's verifier on one artifact state through `ReplayTrial` (Harbor's regrade); its `outcome` is `verdict`, `no_verdict`, or `infra`.
- **gate 1 / gate 2** - fidelity checks: the last checkpoint's state equals the final state; replaying the final state reproduces the recorded reward and each check (`flaky` and `irreproducible` checks are excluded).
- **regression** - a check that passes at a point and fails from the next point to the end; a mechanical ground-truth item, tested by revert.
- **fix / try** - a unified diff tried on a trial's final state by `trajlab gt try-fix`, in `artifacts` or `environment` mode; recorded as a `FixRecord`, regraded as a `counterfactual` replay.
- **confirmed fix** - a fix every replay of which turns its claimed checks from failing to passing and breaks none.
- **gt labeler** - the Claude Code agent `trajlab gt label` runs to propose causes and fixes for one trial; never the judge.
  Not: a labeler in `trajlab trace` (ADR-0015), who is a person.
- **item** - one ground-truth fault of a failed trial (`GroundTruthItem`): its checks, kind, blamed points and calls, hunks, flags.
- **blame / origin / earliest** - the point that last wrote a line (last-writer blame), and the earliest point at which the file held the same text.
- **admission / claim / quiet** - the host-wide ledger that starts verifier environments and fix containers by declared CPUs and memory; a claim is one entry; a quiet claim runs beside at most 2 other CPUs.
  Not: "slot", which the first draft used.

## Everything else

- **ADR** - Architecture Decision Record, in `docs/decisions/`.
  Accepted ADRs are historical: amend, do not rewrite.
- **capture** - this project's current phase (versus the later analysis phase); also the act of taking a checkpoint (`capture_ms`, "capture order").
- **contracts** - `src/trajlab/contracts/`: the shared pydantic models; with `atif/`, the only capture modules analysis code (`store/`) imports (ADR-0014).
- **fixture** - `tests/fixtures/hello-world-trial/`: one real, trimmed trial dir; every offline test runs against it.
- **bind mount** - Harbor mounts the trial's `agent/` dir at `/logs/agent` in the container, so container writes appear on the host immediately; this is the hook-to-watcher channel.
- **manifest** - unqualified, the corpus manifest.
  Qualify Harbor's `artifacts/manifest.json` and any dataset manifest.
- **state-mutating / read-only tool** - state-mutating: `Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` (the hook matcher; checkpointed; `MultiEdit` no longer exists in current Claude Code and matches nothing).
  Read-only: `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Task` (not checkpointed).
  ADR-0001 is the authoritative list; do not restate a different one.
- **timeout multiplier** - Harbor's `--timeout-multiplier`, scaling each task's agent timeout; checkpointed runs need a larger one, recorded in the corpus manifest.
