# Checkpoint protocol: hook ↔ watcher ↔ backend

Status: spec, not yet implemented. Governs `src/trajlab/checkpoint/` and `configs/claude-code/settings.hooks.json`. Changes here require an ADR.

## Goal

One environment checkpoint per state-mutating tool call, taken synchronously between the tool's completion and the next model call, keyed by the same `tool_use_id` that ATIF records as `tool_call_id`.

## Participants

- **Hook** (inside the container): a Claude Code `PostToolUse` hook registered in `settings.hooks.json`, which Harbor passes through as `--settings`. Runs as the agent user with `CLAUDE_CONFIG_DIR=/logs/agent/sessions` set, so `/logs/agent` is `$CLAUDE_CONFIG_DIR/..`.
- **Watcher** (on the host): `trajlab watch <jobs-dir> --backend <name>`. Watches `*/agent/checkpoints/*.req` under every trial dir with `watchdog`, one process per job.
- **Backend** (on the host): implements `SnapshotBackend.snapshot(target, name) -> CheckpointRecord`. The only backend is `docker_commit` (ADR-0004; the planned `statefork` backend was dropped). `restore()` and `fork()` are declared on the protocol but raise `NotImplementedError` in this phase.

## Files, all under `<trial>/agent/checkpoints/`

| File | Written by | Content |
| --- | --- | --- |
| `<tool_use_id>.req` | hook | JSON: `{tool_use_id, tool_name, session_id, cwd, ts}` from the hook's stdin |
| `<tool_use_id>.ack` | watcher | JSON: the `CheckpointRecord` |
| `<tool_use_id>.timeout` | hook | written if no `.ack` arrived within the hook's wait budget; checkpoint is missing for this call |
| `checkpoints.jsonl` | watcher | append-only, one `CheckpointRecord` per line, in capture order |
| `watcher.log` | watcher | per-trial log |

## Sequence

1. Claude Code finishes a tool call matched by the hook (`Bash|Write|Edit|MultiEdit|NotebookEdit`). It runs the hook command with the event JSON on stdin.
2. Hook writes `<tool_use_id>.req` atomically (write to `.req.tmp`, `mv`).
3. Hook polls for `<tool_use_id>.ack` every 0.2 s, up to `TRAJLAB_ACK_WAIT` seconds (default 240). On ack: exit 0. On timeout: write `.timeout`, exit 0. **The hook never blocks the agent indefinitely and never exits non-zero**; a missing checkpoint is recorded, not fatal.
4. Watcher sees `.req`, resolves the container for that trial (see below), calls `backend.snapshot(container, name=f"{trial_id}-{seq:04d}-{tool_use_id[:8]}")`, appends the record to `checkpoints.jsonl`, writes `.ack`.
5. Claude Code continues to its next model call.

## Container resolution

From the `.req` path, the trial dir is two levels up. Read `config.json` for `trial_name`; Harbor's Docker environment names the compose project from `session_id = f"{trial_name}__agent"`, sanitized. Do not rebuild the sanitizer; call `harbor.environments.docker.docker._sanitize_docker_compose_project_name` and then `docker ps --filter label=com.docker.compose.project=<name> --filter label=com.docker.compose.service=main --format '{{.ID}}'`. Cache per trial.

## CheckpointRecord (in `contracts/checkpoint.py`)

```
checkpoint_id   str    backend-specific handle (image id for docker_commit)
trial_id        UUID   from result.json / config
tool_call_id    str    == tool_use_id
seq             int    capture order within the trial, from 1
tool_name       str
backend         str    always "docker_commit"; kept as a field for per-snapshot provenance (ADR-0004)
physical        bool   always True; docker commit has no virtual mode (ADR-0002, ADR-0004)
capture_ms      int
bytes           int | None
path            str | None   on-disk location, if any
requested_at / captured_at   ISO 8601
```

## Constraints to design around

- **Hook timeout budget**: Claude Code's per-hook `timeout` must exceed the worst-case snapshot time plus the ack wait; set it to 300 s in `settings.hooks.json`. Hook wall time counts toward Harbor's agent timeout (task-defined, scaled by `--timeout-multiplier`); corpus runs with checkpoints will need a larger multiplier. Record the multiplier in the corpus manifest.
- **Tool coverage**: read-only tools (`Read`, `Grep`, `Glob`, `WebFetch`) are not matched. Postprocess joins those steps to the most recent earlier checkpoint. This is ADR-0001.
- **No jq/python guarantee** in task images. The hook is POSIX `sh` and extracts `tool_use_id` with `sed`. Test against an Alpine image.
- **Watcher absent** (e.g. stock corpus run): no `.ack` ever arrives; every call writes `.timeout` after 240 s. So **do not pass `settings.hooks.json` unless the watcher is running.** `trajlab run --hooks` refuses to start if it cannot see a watcher pid file.
- **docker commit** pauses the container briefly and captures the filesystem only; live memory and shell state are lost. This is a permanent, accepted limitation of the project (ADR-0004): no CRIU-capable backend attaches to the Docker containers Harbor runs, and CRIU's per-call cost exceeds our storage and compute budget. See `docs/research/2026-09-23_checkpoint-platform-research.md` Q1/Q3 for the evidence.
- **Idempotence**: a `.req` with an existing `.ack` is ignored. Watcher restart replays unacked `.req` files.

## settings.hooks.json (shape)

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'mkdir -p \"$CLAUDE_CONFIG_DIR/../checkpoints\" && exec sh \"$CLAUDE_CONFIG_DIR/../hook/post_tool_use.sh\"'",
            "timeout": 300
          }
        ]
      }
    ]
  }
}
```

The hook script itself must reach the container before the agent runs. Two options, pick one in ADR-0003: (a) inline the whole script in `command` so nothing needs uploading; (b) the watcher writes `agent/hook/post_tool_use.sh` into the trial dir the moment it appears (bind mount), which is before Claude Code finishes installing. Option (a) is safer; option (b) is more readable. Check the Claude Code hooks reference for the exact stdin fields (`tool_use_id`, `tool_name`, `session_id`, `cwd`) against the installed CLI version before relying on them.
