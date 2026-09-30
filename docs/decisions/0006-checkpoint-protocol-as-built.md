# ADR-0006: Checkpoint protocol as built (build-order step 6)

Status: proposed (2026-09-30).

## Context

`docs/checkpoint-protocol.md` was written as a spec before any of it ran.
Planning the implementation against Harbor 0.23.0, Claude Code's hooks reference, and Docker 29 on the development Mac turned up places where the spec cannot work as written or leaves a choice open.

- **No trial id during the agent phase.**
  Harbor writes the trial's `config.json` at start without an id; `result.json`, the only file holding `id`, is written in `Trial._finalize` after the agent has exited (`harbor/trial/trial.py`, `_init_result`, `_finalize`).
  `CheckpointRecord.trial_id: UUID` therefore cannot be filled by the watcher.
- **The planned image name carries no information.**
  `tool_use_id[:8]` is `toolu_01` for every call.
  Docker image repositories must be lowercase, while `tool_use_id` and `trial_name` are mixed case.
  Tags may be mixed case (checked with Docker 29.8).
- **Image size is not reportable from the image.**
  With the containerd snapshotter (Docker Desktop's default), `docker image inspect .Size` counts both the unpacked and the compressed content: a 20 MB write on a 13.6 MB base reported 55.5 MB.
  `docker container inspect --size .SizeRw` reported the writable layer exactly (20,975,616 bytes), and that layer is what `docker commit` captures.
- **Hook shell.**
  The hooks reference says a shell-form command hook runs under `sh -c` on Linux, not bash, which matches the POSIX `sh` requirement.
  PostToolUse stdin carries `tool_use_id`, `tool_name`, `session_id`, and `agent_id` inside subagents.
  `MultiEdit` is no longer documented as a tool; it stays in the matcher, where it matches nothing and costs nothing.
- **Hook delivery was left open** by ADR-0003 and the protocol ("decided in the step-6 implementation PR").
- **A late acknowledgement is ambiguous.**
  If the hook gives up and the agent continues, a snapshot finished afterwards shows a later state than the tool call it is keyed by.
- **The `trajlab run --hooks` flag duplicates the job config.**
  Whether hooks are on is already stated by the job config's agent kwargs.
- **Measured cost.** `docker commit` of a small container took 0.47 s on the development Mac.

## Decision

1. **Records are keyed by `trial_name`.**
   `CheckpointRecord.trial_id` is replaced by `trial_name`, the trial dir's name from `config.json`.
   Postprocess runs inside a finished trial dir and reads the trial id from `result.json` when it needs one; `TrialRecord` already joins the two.
2. **Hook delivery is inline (ADR-0003 option a).**
   `src/trajlab/checkpoint/hook/post_tool_use.sh` is the source; `configs/claude-code/settings.hooks.json` is generated from it with the script as the hook's `command` string, newlines included, so nothing is uploaded and no race with the watcher exists.
   A test fails if the committed settings file differs from the rendered one.
   The job config enables hooks by naming the settings file in `agents[0].kwargs.config`; Harbor uploads it and passes it as `--settings`.
3. **The `.req` file** is `{"tool_use_id", "tool_name", "session_id", "agent_id"}`, extracted from the hook's stdin with `grep`/`sed`.
   `requested_at` is the `.req` file's modification time as the host sees it, so it has sub-second resolution without relying on the container's `date`.
   A hook that cannot extract a well-formed `tool_use_id` appends a line to `checkpoints/hook-errors.log` and exits 0.
4. **Late acknowledgements are discarded.**
   `docker commit` pauses the container, hook included, so the hook cannot give up mid-snapshot.
   After a snapshot, the watcher keeps it only if `<id>.timeout` does not exist; otherwise it removes the image and records nothing.
   A hook that writes `.timeout` checks for `.ack` once more and deletes its `.timeout` if the ack won the race.
   Every kept record was therefore captured before the agent resumed.
5. **Images** are tagged `trajlab-checkpoint:<trial_name>.<seq:04d>` and labelled `trajlab.trial_name`, `trajlab.tool_call_id`, `trajlab.seq`.
   `checkpoint_id` is the `sha256:` image id; `path` stays null.
6. **`bytes`** is the container's writable-layer size (`SizeRw`) right after the commit: the bytes the checkpoint's top layer holds.
7. **One watcher per jobs dir**, enforced by an exclusive `flock` on `<jobs-dir>/.trajlab-watcher.lock`.
   `trajlab run` refuses a job config that enables hooks unless that lock is held, which replaces the planned `--hooks` flag.
8. **Snapshots within a trial are serialized.**
   Parallel tool calls in one assistant message fire their hooks together; each gets its own checkpoint in arrival order, and a later one may include the effects of an earlier one's siblings.
   `seq` is assigned at append time.
9. **Replay on watcher start** snapshots a `.req` that has neither `.ack` nor `.timeout` and whose trial has no `result.json`.
   A `.req` whose `tool_use_id` already has a record in `checkpoints.jsonl` only gets its `.ack` rewritten.
10. **Permissions.** The hook makes `checkpoints/` world-writable, so a watcher that is not root can write `.ack` into a directory the container created (matters on a Linux host; a no-op on Docker Desktop).

## Consequences

- `docs/checkpoint-protocol.md` is updated to match; this ADR is the record of what changed from the original spec.
- `CheckpointRecord` changes shape before any corpus has used it, so no data is affected.
- The first checkpointed run needs a new `corpus_id`, as every capture change does.
- ADR-0003 is amended with the delivery decision.
- Agent wall time grows by roughly the commit time plus up to 0.2 s of polling per state-mutating call; corpus configs with hooks raise `timeout_multiplier`, which the manifest records.
- Open until the acceptance run: whether hook output appears in the stream-json `claude-code.txt` and whether Harbor's converter tolerates it, and whether `--print` fires PostToolUse for subagent calls.
