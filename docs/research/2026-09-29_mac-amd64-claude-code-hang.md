# Dev trials on the Mac: Claude Code hangs in amd64 task containers

Date: 2026-09-29, follow-up 2026-09-30.
Status: failure record and open investigation, not a decision.
Sep 30 follow-up: the hang did not reproduce on either Claude Code version, so the version-regression hypothesis below is withdrawn; see "Follow-up".
These were development runs (`dev-tb21-strict-check*`) meant to give build-order step 3 real, longer trajectories.
No trial produced a usable trajectory, and all job directories were deleted afterwards.

## Setup

- Apple Silicon Mac, Docker Desktop 29.8.0, Harbor 0.23.0.
- Terminal-Bench 2.1 via `terminal-bench/terminal-bench-2-1`, one attempt per task, two concurrent.
- Tasks: `write-compressor`, `regex-chess`, `kv-store-grpc`, and `qemu-alpine-ssh` (later replaced by `schemelike-metacircular-eval`).
- Agent: Harbor's `claude-code`, which installed Claude Code 2.1.285 in every container.
- Models: `anthropic/claude-sonnet-5` at `reasoning_effort=medium`, then `anthropic/claude-sonnet-5-5` at `high` with `--agent-timeout-multiplier 2`.

Task names need the dataset prefix: `-i terminal-bench/kv-store-grpc`, not `-i kv-store-grpc`.

## Failures, in order

1. **Empty auth token.**
   `.env` was a byte-for-byte copy of `.env.example`, so `CLAUDE_CODE_OAUTH_TOKEN` was empty while `CLAUDE_FORCE_OAUTH=1`.
   Three trials failed in agent setup with `RuntimeError`.
   Fix: `claude setup-token` and paste the token into `.env`.
2. **`qemu-alpine-ssh` cannot install the agent.**
   Its image is Debian 11 (bullseye).
   `apt-get install nodejs npm` gets 404s from `deb.debian.org/debian-security`, reproduced in a fresh container from `alexgshaw/qemu-alpine-ssh:20251031`.
   Likely cause: bullseye LTS ended and its packages are moving to `archive.debian.org` (not verified).
   This affects every TB 2.1 task whose image is bullseye, including in a real corpus; a fix would be an agent subclass that repoints apt, which needs an ADR.
   `schemelike-metacircular-eval` (bookworm) installs cleanly and replaced it.
3. **Lid-close sleep.**
   The Mac slept mid-job; Harbor's timeouts count wall-clock time, so three trials ended in `AgentTimeoutError` after about 20 agent events each.
   `caffeinate -ims` does not prevent lid-close sleep.
   This turned out to mask failure 4.
4. **Agent hangs mid-stream (the real blocker).**
   With the Mac awake and Docker freshly restarted, every trial still stopped after 13 to 50 session events and then sat silent until `AgentTimeoutError`.
   In each stuck container, the `claude` process was alive and sleeping (`Sl`), the last line of `claude-code.txt` was a `thinking_tokens` or tool-result event, and its sockets to the Anthropic API (160.79.104.10:443) were in `CLOSE_WAIT`.
   Rate limits were not involved: `rate_limit_info` showed 6% of the five-hour window.

## Reproduction outside Harbor

Same prompt (a long mental-arithmetic question), `claude --print --output-format=stream-json --effort high --model claude-sonnet-5-5`, Claude Code 2.1.285:

| Where | CPU emulation | Result |
|---|---|---|
| Mac host | none | finished in 2.4 min |
| `debian:bookworm` container, `linux/arm64` | none | finished in 6.3 min |
| `debian:bookworm` container, `linux/amd64` | Rosetta | stalled after about 950 thinking tokens, sockets in `CLOSE_WAIT`, no progress for 9+ min |
| `debian:bookworm` container, `linux/amd64` | QEMU (Rosetta disabled in Docker Desktop) | Claude Code aborted (SIGABRT) during install and never started |

All four runs share one Docker VM and network path, so the difference is the container's CPU architecture, not networking.
Terminal-Bench task images (`alexgshaw/<task>:20251031`) are amd64-only, so every TB trial on this Mac runs under Rosetta.
Rosetta was re-enabled after the QEMU test; check with `grep rosetta /proc/1/maps` inside an amd64 container (`/run/rosetta` is not a reliable probe).

## Follow-up, 2026-09-30 (03:45 to 04:17 UTC)

Same Mac, Docker Desktop, Rosetta (`grep -c rosetta /proc/1/maps` = 3), model, and effort as the night before.

### Repro outside Harbor, both versions

Container: `debian:bookworm`, `linux/amd64`, Claude Code from `claude.ai/install.sh`, `--effort high --model claude-sonnet-5-5 --print --output-format=stream-json`.
Prompt: a five-part long-multiplication question asked as a "written worked solution".
A first wording that asked to "show every intermediate step of your reasoning" was refused by the API as `reasoning_extraction`; do not reuse that phrasing.

| Version | Started (UTC) | Wall time | Thinking tokens | Output tokens | Result |
|---|---|---|---|---|---|
| 2.1.278 | 03:47 | 58 s | 6,777 | 8,562 | finished, exit 0 |
| 2.1.285 | 04:01 | 74 s | 8,660 | 10,630 | finished, exit 0 |

The 2.1.285 run was the control, started while the Harbor job was still running, and it went well past the ~950 thinking tokens where the same version stalled the night before.
Sockets stayed `ESTABLISHED` throughout; no `CLOSE_WAIT`.

### Dev job `dev-tb21-strict-check`, Claude Code pinned to 2.1.278

Command: the Sep 29 command plus `--ak version=2.1.278`, run detached under `caffeinate -dims`.
The stale job directory from Sep 29 was moved to `dev-tb21-strict-check.stale-2026-09-29` because Harbor refuses to resume a job directory whose config differs.
Job runtime 28 min 16 s, two trials concurrent.

| Trial | Reward | Steps | Agent time | Input / output tokens | Cost | `trajlab validate` |
|---|---|---|---|---|---|---|
| write-compressor | 1.0 | 5 | 1 min 20 s | 121,573 / 5,375 | $0.13 | valid |
| kv-store-grpc | 1.0 | 5 | 22 s | 111,322 / 1,335 | $0.05 | valid |
| schemelike-metacircular-eval | 1.0 | 25 | 20 min 45 s | 2,098,348 / 61,924 | $1.38 | valid |
| regex-chess | 0.0 | 10 | 18 min 35 s | 146,260 / 128,095 | $1.36 | valid |

No trial stalled.
Every container was polled every two minutes for `CLOSE_WAIT` sockets (state `08` in `/proc/net/tcp`) and never showed one.
`result.json → agent_info.version` reads `2.1.278` in all four, so `--ak version=` is honoured.

regex-chess did not hang.
Claude Code ended the session with `API Error: Claude's response exceeded the 32000 output token maximum`, which Harbor classifies as `OutputTokenExceededError`; the agent had written nothing to disk, so all four verifier tests failed on missing files.
Harbor still wrote a valid `trajectory.json` for the errored trial.
The four capped turns were thinking only (32,000 output tokens each, no tool call); Claude Code injected "Output token limit hit. Resume directly ..." after each and gave up after three retries.
Resolved by ADR-0005: `CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000` in `.env`, which Harbor forwards into the container; verified against a stub API that Claude Code sends the value as given and clamps above 128000.

Liveness check that worked: the session JSONL is a poor signal because a line is written only when an assistant message completes, and single turns here ran past four minutes of thinking.
Use the mtime of `agent/claude-code.txt` (stream-json events arrive every few seconds during thinking) together with the socket-state count.

## Interpretation (revised)

The version-regression hypothesis is withdrawn.
Claude Code 2.1.285 completed the identical prompt under Rosetta about an hour after it had stalled on it repeatedly, and 2.1.278 was never shown to differ from it.
The Sep 29 pattern, every stream dying in `CLOSE_WAIT` between roughly 02:45 and 03:30 UTC on both a Harbor trial and a bare container, is now best explained as a transient condition in that window: the API edge, the network path, or Docker Desktop's userspace network proxy, which is the component that can leave the container side half-closed.
Nothing observed distinguishes these, and Docker Desktop had already been restarted once during the Sep 29 attempts, which weakens the Docker-only reading.
Rosetta remains a possible contributing factor but is not established: the Sep 29 arm64 control finished while the amd64 one stalled, yet the amd64 case succeeded on Sep 30 without any change to emulation.
The Sep 25 pilot passing 4/4 on these same amd64 images, and the Sep 21 hello-world smoke test, are both consistent with a transient cause and need no regression to explain.

## Next steps

1. Drop the version pin from the dev-run command; it is unsupported by evidence and only hides the CLI version drift that a bug report would need.
   Keep recording the version in every note.
2. Keep the ten-minute liveness check in the dev-run procedure: `claude-code.txt` mtime plus `CLOSE_WAIT` count per container.
   If a stall recurs, capture `ss -tanpi` (or `/proc/net/tcp` receive-queue column) inside the container and the Docker Desktop log before killing anything, since that is the evidence that separates a client that stopped reading from a proxy that dropped the stream.
3. Rerun regex-chess with ADR-0005's cap in place to see whether the trial completes; the default cap ended it with no files written.
4. The three passing trials plus the errored one are the first real TB 2.1 trajectories for build-order step 3; `trajlab validate` passes on all four.
5. Fallbacks unchanged: build task images natively for arm64 (dev data only), or run on the x86 Linux server.
