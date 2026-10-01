# Checkpoint protocol: hook ↔ watcher ↔ backend

Status: spec for build-order step 6, amended by ADR-0006 to match the implementation, by ADR-0007 for checkpoints every N calls, and by ADR-0010 for checkpoints only on calls that changed the filesystem.
Governs `src/trajlab/checkpoint/` and `configs/claude-code/settings.hooks.json`.
Changes here require an ADR.

## Goal

One environment checkpoint per tool call that changed the container's filesystem, taken synchronously between the call's completion and the next model call, keyed by the same `tool_use_id` that ATIF records as `tool_call_id` (ADR-0010, `--gate change`).
Every hooked call gets a `CallRecord` naming the checkpoint that holds the state after it; a call that changed nothing names the previous checkpoint, whose state it provably equals.
The older policy, a checkpoint every N calls without measuring (ADR-0007, `--gate none`), remains available.

## Participants

Vocabulary for these roles, and for everything else in this doc, is in `docs/glossary.md`.

- **Hook** (inside the container): a Claude Code hook registered for `PostToolUse` and `PostToolUseFailure` in `settings.hooks.json`, which Harbor passes through as `--settings`.
  A call that ends in error (e.g. Bash exiting non-zero) fires only `PostToolUseFailure` and may still have changed files (ADR-0010).
  Claude Code runs it with `sh -c` as the agent user, with `CLAUDE_CONFIG_DIR=/logs/agent/sessions` set, so `/logs/agent` is `$CLAUDE_CONFIG_DIR/..`.
- **Watcher** (on the host): `trajlab watch <jobs-dir> --every 1 --gate change`.
  Watches `*/*/agent/checkpoints/*.req` under the jobs dir with `watchdog`, plus a periodic sweep for events the observer missed.
  One process per jobs dir, enforced by an exclusive `flock` on `<jobs-dir>/.trajlab-watcher.lock`.
- **Backend** (on the host): implements `SnapshotBackend.snapshot(container, tag=..., labels=...) -> Snapshot`.
  The watcher builds the `CheckpointRecord` from the `Snapshot` and the `.req`.
  The only backend is `docker_commit` (ADR-0004).
  `restore()` and `fork()` are declared on the protocol but raise `NotImplementedError` in this phase.

## Files, all under `<trial>/agent/checkpoints/`

| File | Written by | Content |
| --- | --- | --- |
| `<tool_use_id>.req` | hook | JSON `{tool_use_id, tool_name, session_id, agent_id, event}` extracted from the hook's stdin; its mtime is `requested_at` |
| `<tool_use_id>.ack` | watcher | JSON: the call's `CallRecord` (outcome `checkpoint`, `unchanged`, or `deferred`) |
| `calls.jsonl` | watcher | append-only, one `CallRecord` per answered call, in answer order |
| `<tool_use_id>.timeout` | hook | written if no `.ack` arrived within the hook's wait budget; checkpoint is missing for this call |
| `checkpoints.jsonl` | watcher | append-only, one `CheckpointRecord` per line, in capture order |
| `policy.json` | watcher | the `CheckpointPolicy` (`backend`, `every`, `gate`) this trial is captured under; written on first contact |
| `watcher.log` | watcher | per-trial log |
| `hook-errors.log` | hook | one line per hook invocation that could not extract a `tool_use_id` |

## Sequence

1. Claude Code finishes a tool call matched by the hook (`Bash|Write|Edit|MultiEdit|NotebookEdit`; `MultiEdit` is not a tool in current Claude Code and matches nothing).
   It runs the hook command with the event JSON on stdin.
2. Hook writes `<tool_use_id>.req` atomically (write to `<tool_use_id>.req.tmp`, `mv`).
3. Hook polls for `<tool_use_id>.ack` every 0.2 s, up to `TRAJLAB_ACK_WAIT` seconds (default 240; Harbor forwards it only via `--ae`, not from `.env`).
   On ack: exit 0.
   On timeout: write `.timeout`, check for `.ack` once more (deleting `.timeout` if it is there), exit 0.
   **The hook never blocks the agent indefinitely and never exits non-zero**; a missing checkpoint is recorded, not fatal.
4. Watcher sees `.req`. Under `--gate change` it lists the container's filesystem and compares it with the listing at the trial's last checkpoint (ADR-0010); if nothing differs outside the harness exclusions, it answers the call as unchanged and takes no snapshot.
   Under `--gate none` it counts the trial's uncovered calls (ADR-0007); below N, it answers the call as deferred and takes no snapshot.
   At N, it resolves the container for that trial (see below) and calls `backend.snapshot(container, tag=f"{trial_name}.{seq:04d}", labels=...)`.
   If `.timeout` exists once the snapshot returns, the watcher removes the image and records nothing (ADR-0006); the calls stay uncovered.
   Otherwise it appends the record, covering all those calls, to `checkpoints.jsonl`, then the `CallRecord` to `calls.jsonl`, then writes `.ack`.
5. Claude Code continues to its next model call.

Snapshots within one trial are serialized; parallel tool calls get one checkpoint each, in arrival order.

## Container resolution

From the `.req` path, the trial dir is three levels up.
Read `config.json` for `trial_name`.
Harbor's Docker environment names the compose project from the environment session id `f"{trial_name}__env"`, sanitized (see `docs/harbor-facts.md`); `trajlab.capture.discover.compose_project_name` implements this, and the CLI hands it to the watcher, since `checkpoint/` does not import `capture/` (CLAUDE.md constraint 3).
Then `docker ps --filter label=com.docker.compose.project=<name> --filter label=com.docker.compose.service=main --format '{{.ID}}'`.
Cache per trial.

## docker_commit backend

- `docker commit --change 'LABEL ...' <container> trajlab-checkpoint:<tag>`; Docker pauses the container for the duration, hook included.
- Tag: `<trial_name>.<seq:04d>`; image repository names must be lowercase but tags need not be.
  Labels: `trajlab.trial_name`, `trajlab.tool_call_id`, `trajlab.seq`, so `docker images --filter label=trajlab.trial_name=<name>` lists a trial's checkpoints.
- Every `com.docker.compose.*` label the container carries is blanked on the image; `docker commit` would otherwise copy the compose project label, and Harbor's teardown (`docker compose down --rmi local`) deletes every image with that label (ADR-0006).
- `checkpoint_id` is the image id (`sha256:...`).
- `bytes` is `docker container inspect --size` `.SizeRw` right after the commit: the writable layer the commit captured.
  `docker image inspect .Size` is not used; under the containerd snapshotter it counts unpacked and compressed content together.

## CheckpointRecord (in `contracts/checkpoint.py`)

```
checkpoint_id   str    backend-specific handle (image id for docker_commit)
trial_name      str    the trial dir name, from config.json; a running trial has no trial_id yet (ADR-0006)
tool_call_id    str    == tool_use_id
seq             int    capture order within the trial, from 1
covered_tool_call_ids  list[str]  calls since the previous checkpoint, in request order, ending with tool_call_id (ADR-0007)
tool_name       str
backend         str    always "docker_commit"; kept as a field for per-snapshot provenance (ADR-0004)
physical        bool   always True; docker commit has no virtual mode (ADR-0002, ADR-0004)
capture_ms      int    wall time of the docker commit call
bytes           int | None   writable-layer size captured
path            str | None   on-disk location, if any (null for docker_commit)
requested_at    ISO 8601     .req mtime
captured_at     ISO 8601     when docker commit returned
```

## Enabling hooks for a run

A job config enables checkpointing by setting `agents[0].kwargs.config` to `configs/claude-code/settings.hooks.json`.
`trajlab run` refuses such a config unless a watcher holds the lock on the config's `jobs_dir`.
Start the watcher first: `uv run trajlab watch corpus/jobs --every 1 --gate change`.
The watcher writes its policy into each trial's `policy.json`, and the corpus manifest records it as `checkpoint_every` and `checkpoint_gate`.
`--gate audit` checkpoints every call while still recording the detector's verdict; `scripts/2026-09-30_audit_detector.py` then checks the verdicts against the checkpoints' contents.

## Constraints to design around

- **Hook timeout budget**: Claude Code's per-hook `timeout` must exceed the worst-case snapshot time plus the ack wait; it is 300 s in `settings.hooks.json`.
  Hook wall time counts toward Harbor's agent timeout (task-defined, scaled by `--timeout-multiplier`); corpus runs with checkpoints need a larger multiplier, which the corpus manifest records.
- **Tool coverage**: read-only tools (`Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Task`; ADR-0001 is the authoritative list) are not matched.
  Postprocess joins those steps to the most recent earlier checkpoint.
  This is ADR-0001.
- **No jq/python guarantee** in task images.
  The hook is POSIX `sh` and extracts fields with `grep` and `sed`.
  It is tested against an Alpine image (`scripts/2026-09-30_hook_alpine_check.sh`).
- **Hook delivery is inline** (ADR-0003 amendment, ADR-0006): `settings.hooks.json` carries the whole script as the `command` string, generated from `src/trajlab/checkpoint/hook/post_tool_use.sh` by `uv run python -m trajlab.checkpoint.hook`.
  Edit the script, regenerate, commit both; a test enforces that they agree.
- **Watcher absent** (e.g. stock corpus run): no `.ack` ever arrives; every call writes `.timeout` after 240 s.
  So **do not pass `settings.hooks.json` unless the watcher is running**; `trajlab run` enforces this.
- **Permissions**: the hook makes `checkpoints/` world-writable so a non-root watcher on a Linux host can write into a directory the container created.
- **docker commit** pauses the container briefly and captures the filesystem only; live memory and shell state are lost.
  Bind mounts, including `/logs/agent`, are not in the image.
  This is a permanent, accepted limitation of the project (ADR-0004): no CRIU-capable backend attaches to the Docker containers Harbor runs, and CRIU's per-call cost exceeds our storage and compute budget.
  See `docs/research/2026-09-23_checkpoint-platform-research.md` Q1/Q3 for the evidence.
- **Final state**: under `--gate change` the state after the last call is always in a checkpoint, either its own or, if it changed nothing, the previous one, unless that call timed out. With `--gate none` and N > 1, up to N-1 calls after the last checkpoint are covered by none (ADR-0007).
- **Change detection** (ADR-0010): GNU `find / -xdev` per call, about 0.2 to 0.3 s; files compare by type, mode, owner, size, change time, and link target, directories by type, mode, and owner; harness paths are excluded from the decision but not from checkpoints. An image without GNU `find` is checkpointed on every call. The first measured call of a trial, and the first after a watcher restart, is checkpointed as the baseline.
- **Idempotence**: a `.req` with an existing `.ack` or `.timeout` is ignored.
  Watcher restart replays unacked `.req` files of trials without `result.json`; a `.req` whose `tool_use_id` a record already covers only gets its `.ack` rewritten.
  The every-N count is derived from the files, so a restart resumes it exactly; a watcher whose N differs from a trial's `policy.json` refuses that trial.

## Open questions

- **Checkpoint cost.**
  On Harbor's stock image each checkpoint re-captures the agent install, about 1.3 GB and 36 s (ADR-0006).
  On the pre-installed image (ADR-0008) a checkpoint holds only the trial's changes, 0.1 to 57 MB in the acceptance runs, but `docker commit` still takes 4 to 16 s, growing with the base image's size under Docker Desktop's containerd image store.
  Choose N (ADR-0007) with that time cost in mind.
- **Final checkpoint.** A Claude Code `Stop` hook could capture the state after the last call, which every-N leaves uncovered.

- **Representation of the read-only join.**
  ADR-0001 says postprocess joins read-only steps to the most recent earlier checkpoint, but ADR-0003 only inserts a system step after agent steps that own a checkpointed `tool_call_id`, so the enriched file does not yet say how a read-only step's join appears.
  Decide in the step-7 implementation PR and amend ADR-0003.
