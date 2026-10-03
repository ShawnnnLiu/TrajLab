# TB 4.0 setup versus Anthropic's reported setup (2026-10-03)

How our TB 4.0 runs differ from the setup Anthropic reports for Claude Sonnet 5.5 on Terminal-Bench 4.0.
Every TB 4.0 corpus so far carries these differences, including `tb40-repair-v3` (running at the time of writing).
The changes listed under "Decided for a later experiment" are not implemented; no run behavior changes with this note.

## Sources

- **Anthropic, "Introducing Claude Sonnet 5.5"** (https://www.anthropic.com/claude-sonnet-5-5), retrieved 2026-10-03: 70.6% on TB 4.0.
  Footnote 1 says only that Opus 5.5 is reported at Xhigh effort. The post gives no harness, prompt, or timeout details.
- **Claude Sonnet 5.5 System Card, §8.5** (https://www.anthropic.com/claude-sonnet-5-5-system-card, PDF pp. 112-113), retrieved 2026-10-03. Quoted:
  - "The numbers reported use Claude Code in --bare mode at max thinking effort, except Opus 5.5, which is reported at xhigh effort."
  - "We ran the evaluation with no internet egress allowed, preemptively caching resources needed for the model to operate based on historical run logs, and rebuilding the images accordingly."
  - "Scores are averaged over five trials per task (330 trials) for Sonnet 5.5 and Opus 5.5." Standard error ±2.5 points for Sonnet 5.5.
  - "Claude Sonnet 5.5 scored 70.6% on Terminal-Bench 4.0 with safeguards enabled; requests flagged by the safeguards were answered by a fallback model following the default server-side fallback policy (1.2% of requests, affecting 1.5% of trials, with one attempt stopping instead of replying)."
  - TB 4.0 "largely addresses these issues by increasing timeouts and adaptively raising both RAM and CPU in a few tasks."
- Neither document mentions a custom system prompt, appended prompt, or other prompting for TB 4.0.
- **Claude Code `--bare`:** `claude --help` (2.1.288) and https://code.claude.com/docs/en/headless ("Start faster with bare mode"), retrieved 2026-10-03.
- **Our side:** `configs/harbor/tb40-sonnet-v*.json`, ADR-0012, the recorded `claude` command and `system/init` event in `corpus/jobs/tb40-sonnet-v2/vpp-loss-divergence__xfbvgSS/agent/claude-code.txt`, the cached task configs under `~/.cache/harbor/tasks/packages/terminal-bench/`, and the pinned Harbor source.

## Differences

| | Anthropic (Sonnet 5.5 system card) | Ours (TB 4.0 corpora) |
|---|---|---|
| Claude Code mode | `--bare` | Normal. Command: `claude --verbose --output-format=stream-json --settings /tmp/claude-code-settings/settings.json --effort medium --permission-mode=bypassPermissions --print` |
| Effort | max | `medium` (all `tb40-sonnet-v*` configs; repairs use the first attempt's effort, ADR-0012 decision 6) |
| Network during the agent phase | No egress; resources pre-cached from historical run logs, images rebuilt | Public. All 23 tasks in our set declare no network policy, so Harbor's default `public` applies |
| Tasks | 66 (all of TB 4.0) | 23 (ADR-0012 decision 2) |
| Trials per task | 5 | 3 first attempts (`tb40-sonnet-v2`) |
| Agent time limit | Not stated; TB 4.0 gives up to 8 h | `agent_timeout_multiplier` 0.5 (4 h) in `tb40-sonnet-v2`; 0.25 in `tb40-sonnet-v1`, `v1b`, `v1c` |
| Hooks in the agent | Not stated; `--bare` skips settings hooks | Our checkpoint hooks on PostToolUse, PostToolUseFailure, Stop, StopFailure (`configs/claude-code/settings.hooks.json`). The hook holds the agent until the watcher acks, inside the agent's time limit |
| Safety-flagged requests | Answered by a fallback model | Not handled; recorded as `AgentSafetyRefusalError` (round 1 of the repair experiment: 3 in `fresh`, 3 in `state`) |
| Claude Code version | Not stated | 2.1.278 (ADR-0009) |
| Sandbox | Not stated | Local Docker on the Linux server, pre-installed derived image (ADR-0008) |

### What `--bare` turns off, against our recorded session

From the `system/init` event of `vpp-loss-divergence__xfbvgSS` (Claude Code 2.1.278):

| | `--bare` (docs) | Ours |
|---|---|---|
| Tools | Bash, file read, file edit | 22: Task, Bash, CronCreate, CronDelete, CronList, Edit, EnterWorktree, ExitWorktree, ListAgents, NotebookEdit, Read, RemoteTrigger, ReportFindings, ScheduleWakeup, SendMessage, Skill, TaskStop, ToolSearch, WebFetch, WebSearch, Workflow, Write |
| Skills listed | none auto-discovered | 16 |
| Subagent types | none auto-discovered | 5 (claude, Explore, general-purpose, Plan, statusline-setup) |
| Settings and plugin hooks | skipped | our four hooks |
| Auto-memory | off | on (`/logs/agent/sessions/projects/-app/memory/`) |
| CLAUDE.md auto-discovery | off | on |
| MCP servers, plugins | only those passed on the command line | 0, 0 |
| System reminders | not sent (Claude Code 2.1.286 and later) | sent |
| Background tasks | none; a timed-out command stops | available |
| Auth | `ANTHROPIC_API_KEY` or `apiKeyHelper` only | as passed by Harbor |

Tool use in `tb40-sonnet-v2`, top-level calls over 69 trials: Bash 1,630 (69 trials), Read 158 (16), `bash` 11 (11), Write 2 (2).
No trial called a tool that `--bare` removes; the removed tools and the skill list were present in the model's context.

### Network use in our first attempts

Bash commands in `tb40-sonnet-v2` (69 trials) matching a package or download command, by regex over the command text:

| Pattern | Trials | Tasks |
|---|---|---|
| any below | 28 | 11 |
| `pip`/`uv` install or add | 28 | 11 |
| `curl`/`wget` | 3 | 1 |
| `npm`/`cargo`/`go` fetch | 3 | 1 |
| `apt`, `git clone`, Hugging Face download | 0 | 0 |

A match shows the command was run, not that it reached the internet (a `curl` may target a local server, an install may be satisfied locally).

## Harbor facts relevant to removing the network

- Harbor's network policy is `public`, `no-network`, or `allowlist` (`harbor/models/task/config.py`, `NetworkMode`), set per task in `task.toml` for the environment, the agent phase, and the verifier phase.
- The job config cannot impose a policy. It has only `extra_allowed_hosts` (agent and environment), which merge into a non-public policy; on a `public` task Harbor ignores them with a warning (`harbor/trial/network_policy.py`, `merge_extra_allowlists`).
- The agent-phase policy applies only during `agent.run()`; the verifier phase switches back to its own policy (`harbor/trial/trial.py`, `_phase_network_policy`).
- The Docker environment enforces non-public policies with an egress-control sidecar, enabled when any phase policy is non-public (`harbor/environments/docker/docker.py`, `_requires_egress_control`, `_apply_network_policy`).
- Claude Code runs inside the trial container, so it needs `api.anthropic.com` reachable: "no internet" for a Claude Code trial is an allowlist, not `no-network`.
- Two of the 23 verifiers install packages at verify time (`pip install` in `cargo-flight-dispatch`, `uv pip install` in `vba-userform-port`), so the verifier phase needs network.

## Open questions

1. Whether hooks passed with `--settings` run under `--bare`. The help text says `--bare` skips "hooks (those defined in settings and by installed plugins)" and also lists `--settings` as a way to "explicitly provide context". Untested. Checkpoint capture depends on the answer.
2. Which Claude Code version Anthropic used. Before 2.1.286, `--bare` still sent system reminders and allowed background tasks.
3. Whether `--bare` changes the default system prompt. The docs do not say.
4. The agent time limit Anthropic used.

## Decided for a later experiment (2026-10-03)

Decided by the owner, to be applied together in a later, larger experiment, possibly with more trials per task:

1. **Custom allowlist** for the agent phase, with the Anthropic API host; verifier phase unchanged. Mechanism not yet chosen; Harbor offers no job-level override (above), and Harbor is not patched (CLAUDE.md constraint 1).
2. **`--bare`**, subject to open question 1.
3. **Pre-caching** of resources into the task images, from our historical run logs.

Effort, time limit, task count, and safety-fallback handling are recorded above but not decided.
Each change alters what a trial records, so it needs an ADR and new corpus ids before it runs (CLAUDE.md constraint 5).
