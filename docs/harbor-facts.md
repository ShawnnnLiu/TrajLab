# Harbor facts this repo depends on

Verified 2026-09-21 against harbor-framework/harbor main (commit 404bae7) and docs.harborframework.com; items marked **[0.23.0]** were re-checked against the pinned install on 2026-09-22. Re-verify against the installed version in `.venv/lib/python3.12/site-packages/harbor/` whenever the pin changes. Each fact names the module to read.

## Trial lifecycle (`harbor/trial/trial.py`, `trial/single_step.py`, `trial/hooks.py`)

- Events: `START → ENVIRONMENT_START → AGENT_START → AGENT_END → VERIFICATION_START → END` (plus `CANCEL`). **No hook fires during the agent phase.** That is why we checkpoint from inside Claude Code's own hooks, not from Harbor.
- Order: build/start sandbox → `agent.setup()` (installs the CLI) → `agent.run()` under a timeout → download `/logs/agent` (skipped on Docker, where it is bind-mounted) → `populate_context_post_run` (native log → ATIF) → collect artifacts → verifier → write `result.json` → stop and delete the sandbox.
- Optional structured log stream: `Trial.on_log(callback)` gets `LogEntry{trial_id, phase, stream, text, timestamp}` per exec chunk. Not needed for capture.

## Trial directory (`harbor/models/trial/paths.py`)

```
<job>/<task>__<id>/
├── config.json      TrialConfig
├── lock.json        TrialLock: task digest sha256, git commit, agent config, env config, verifier mode
├── result.json      TrialResult (id, agent_info, agent_result tokens/cost, verifier_result.rewards, exception_info, phase timings)
├── trial.log
├── exception.txt    only on failure
├── agent/           bind-mounted at /logs/agent in the container (Docker); downloaded otherwise
├── verifier/        test-stdout.txt, test-stderr.txt, reward.txt | reward.json, ctrf.json if emitted
└── artifacts/       manifest.json + mirrored absolute paths
```

Multi-step tasks nest `agent/ verifier/ artifacts/` under `steps/<name>/`. We use single-step tasks only.

In-container contract: `/logs/agent`, `/logs/verifier`, `/logs/artifacts` are mounted; `/tests` and `/solution` are copied. **Anything we write under `/logs/agent` from inside the container lands in `agent/` on the host for free.** This is the channel for the hook.

## Claude Code integration (`harbor/agents/installed/claude_code.py`)

- Installed inside the task container (`npm install -g @anthropic-ai/claude-code` or the bootstrap script). Version captured by `claude --version` into `result.json → agent_info.version`.
- Run command, verbatim shape:
  `printf "%s" "$instruction" | claude --verbose --output-format=stream-json [--settings <path>] [--continue|--resume <id>] --print 2>&1 | tee /logs/agent/claude-code.txt`
- `CLAUDE_CONFIG_DIR=/logs/agent/sessions`. So the native session JSONL is at `agent/sessions/projects/<cwd-slug>/<session-id>.jsonl`, subagents at `.../subagents/agent-<id>.jsonl`. `todos/`, `debug/`, `shell-snapshots/`, `skills/`, `.claude.json` (MCP) are there too.
- Env set by Harbor: `IS_SANDBOX=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `FORCE_AUTO_BACKGROUND_TASKS=1`, `ENABLE_BACKGROUND_TASKS=1`, `ANTHROPIC_MODEL`, auth vars. Extra env via `--ae KEY=VALUE`.
- **Auth [0.23.0]** (`_resolve_auth_env`, `_should_force_oauth`): Harbor forwards `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` into the container. If both are set it keeps the API key. `CLAUDE_FORCE_OAUTH=1` drops the key so the CLI uses the subscription token, and raises if the token is missing. So a Claude subscription works; get the token with `claude setup-token`. Which one was used is visible as `apiKeySource` in the first line of `agent/claude-code.txt`. Harbor never logs the values.
- **`--ak config=<settings.json>` is uploaded verbatim and passed as `--settings`.** Hooks defined there work. Harbor does not merge or filter the file (`_load_base_settings`, `_upload_base_settings`).
- Capabilities: `atif, resume, load_native_trajectory, load_atif_trajectory, handoff, native_config, skills, mcp_servers`.
- **Schema version [0.23.0]:** the Claude Code converter stamps `schema_version="ATIF-v1.7"` even though `Trajectory` defaults to v1.8 and accepts both. Do not assert v1.8 on Harbor's file; the enriched file may use either.
- **`context_management` is not a typed field [0.23.0]:** nothing under `harbor/models/trajectories/` defines it. It is a convention inside step `extra`, exactly as the RFC reserves it, so our `ContextManagementExtra` model in `contracts/` is the only schema for it.
- **The converter emits no `context_management` system steps.** It dedups tool calls "replayed after compaction" (`_convert_events_to_trajectory`). Compaction boundaries must be recovered from the native JSONL. Only the `vibe` agent's converter emits `context_management`.
- Per-step ATIF `metrics`: `prompt_tokens = input + cache_read + cache_creation`, `cached_tokens = cache_read`, raw usage dict in `metrics.extra`. Trajectory `total_cost_usd` is parsed from the stream-json `result` event in `claude-code.txt`.
- Observations: `content` is `[stdout]…[stderr]…[exit_code] N…`; `extra.tool_use_result` keeps Claude Code's structured result; `extra.raw_tool_result` the untouched block; `is_error` preserved.
- Step `extra` carries `id`, `agent_id`, `cwd`, `user_type`, `is_sidechain`. `agent.extra` carries `cwds`, `git_branches`, `agent_ids`, `version`.

## The join key

Claude Code's `tool_use_id` (in the session JSONL, and in the JSON that PreToolUse/PostToolUse hooks receive on stdin) **is** the ATIF `tool_call_id`. One id names the trajectory step, the native event, and the checkpoint.

## Docker environment (`harbor/environments/docker/docker.py`)

- Compose project name = `_sanitize_docker_compose_project_name(session_id)`; `session_id` looks like `hello-world__bZZeEkw__agent`. The main container is `<project>-main-1`. Confirm with `docker ps --filter label=com.docker.compose.project=<project>` rather than string-building.
- `EnvironmentCapabilities.mounted = True` on Docker: `agent/` is a bind mount, so inotify on the host sees container writes immediately.
- `--no-delete` keeps the container after the trial. Useful for debugging the hook; never for corpus runs.
- `EnvironmentConfig.mounts: list[ServiceVolumeConfig]` allows extra bind mounts if we ever need a second channel.

## ATIF (`harbor/models/trajectories/`, `rfcs/0001-trajectory-format.md`, v1.8)

- Root: `schema_version`, `session_id`, `trajectory_id`, `agent{name, version, model_name, tool_definitions?, extra}`, `steps[]`, `final_metrics`, `subagent_trajectories[]`, `extra`.
- Step: `step_id` (1-based), `timestamp`, `source ∈ {system,user,agent}`, `message`, `reasoning_content`, `tool_calls[]{tool_call_id, function_name, arguments, extra}`, `observation.results[]{source_call_id, content, subagent_trajectory_ref, extra}`, `metrics`, `llm_call_count`, `is_copied_context`, `extra`.
- System steps may carry `observation` for "environment resets, checkpoint creation" and `extra.context_management{type: compaction|pruning|injection, boundary: replace|append|truncate}`. **We use exactly these reserved slots** for checkpoint and compaction records, so enriched files stay valid ATIF.
- Validator: `python -m harbor.utils.trajectory_validator <file>` or `harbor.utils.trajectory_validator.TrajectoryValidator`.
- Step ids must stay sequential after insertion. Renumber on write; keep the original id in `extra.original_step_id`.

## Other Harbor commands worth knowing

- `harbor view jobs` — local viewer on :8080; reads `agent/trajectory.json`. It will not show `trajectory.enriched.json`; that is fine.
- `harbor analyze <trial|job>` — an agent (default claude-code) reads `result.json`, `trajectory.json`, `test-stdout.txt`, `exception.txt` and grades a rubric. This is a **baseline** for the analysis phase, not a component.
- `harbor trial regrade` — reruns only the verifier from recorded artifacts. `--load-trajectory` — seeds a new run's conversation from an ATIF or native file; restores no files.
- `harbor datasets list` — confirm dataset names and versions before writing a job config; do not assume `terminal-bench@2.1` exists under that exact name.

## Things Harbor does not have (do not go looking)

- No snapshot/fork/restore in `BaseEnvironment` (`start/stop/exec/upload_*/download_*` only). No `EnvironmentCapabilities` flag for it.
- No per-command timing inside a tool call, no network traffic capture, no `recording.cast` for Claude Code (that is Terminus 2 only).
- No tool definitions in the Claude Code ATIF; no system prompt step.
