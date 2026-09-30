# Dev trials on the Mac: Claude Code hangs in amd64 task containers

Date: 2026-09-29.
Status: failure record and open investigation, not a decision.
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

## Interpretation

Most likely, the Claude Code process stops reading its socket under x86 emulation; the server then closes the idle stream, and the client never notices.
The mechanism (for example a missed event-loop wakeup) is inference, not observed.
The Sep 21 `hello-world` smoke test does not contradict this: Harbor built that image locally from `FROM ubuntu:24.04`, so it ran natively on arm64, and the agent ran for only 4 seconds on Claude Code 2.1.278.
The Sep 25 pilot (`2026-09-25_docent-baseline-pilot.md`) passed 4/4 on an arm64 Mac with these same amd64 images, which points to a regression in a Claude Code release after that pilot.

## Next steps

1. Repeat the Rosetta repro with Claude Code 2.1.278 (Harbor: `--ak version=2.1.278`).
   If it passes, pin that version for Mac dev runs and report the regression upstream.
2. If it still hangs: check the stuck socket's receive queue (unread data confirms the client stopped reading), and try the npm build of Claude Code under Rosetta.
3. Record the Claude Code version in every pilot or dev note; the Sep 25 pilot did not, which is why its version is unknown.
4. Fallbacks: build task images natively for arm64 from each task's `environment/Dockerfile` (dev data only), or run on the x86 Linux server.
