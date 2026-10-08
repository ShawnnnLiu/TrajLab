# Upstream notes

Things Harbor would need to change for trajlab's benefit, and the workaround we use instead.
Harbor is pinned and never patched (CLAUDE.md constraint 1); if you hit a Harbor limitation, add a dated note here with the workaround, and raise it upstream only if the team agrees.

## 2026-09-30: Compose teardown deletes committed images

Harbor's Docker environment tears a trial down with `docker compose down --rmi local`, which removes every image carrying the trial's `com.docker.compose.project` label.
`docker commit` copies container labels onto the image, so any snapshot of a trial container is deleted when the trial ends.
Workaround: the `docker_commit` backend blanks all `com.docker.compose.*` labels at commit time (ADR-0006).
Upstream option: remove only images Compose built for the project, e.g. by also matching `com.docker.compose.image`, or document the behavior.

## 2026-09-30: The agent install lands in every snapshot

Installed agents are set up inside the running container, so the install (about 1.3 GB for Claude Code on Ubuntu 24.04) sits in the container's writable layer and inflates every filesystem snapshot.
Workaround: `trajlab.capture.preinstall:PreinstalledDockerEnvironment` runs Harbor's install once into a cached derived image and starts trials from it (ADR-0008).
Upstream option: an option to install the agent into a derived image before the trial container starts.

## 2026-09-30: Two TB 2.1 tasks cannot install Claude Code (Debian 11)

`qemu-alpine-ssh` and `qemu-startup` are built `FROM debian:bullseye-slim`; the other 87 tasks of the pinned TB 2.1 dataset use bookworm, Debian 13, or Ubuntu 24.04.
Harbor installs Claude Code's dependencies with `apt-get install -y curl bash nodejs npm procps`, which on these images pulls security updates (e.g. `perl-base 5.32.1-4+deb11u5`) that `bullseye-security`'s index still lists (dated 2026-09-12) but that return 404 from deb.debian.org, security.debian.org, and archive.debian.org.
This is consistent with Debian 11's LTS ending on 2026-08-31.
Harbor's own command fails the same way on the unmodified image, so every Claude Code trial of these two tasks fails at agent setup, stock or pre-installed.
Workaround: none yet; leave both tasks out of corpus task lists until the archive catches up or a fix is chosen.
Upstream option: Terminal-Bench moves these images off bullseye, or Harbor's installer tolerates an EOL release (e.g. by pointing apt at snapshot.debian.org).

## 2026-10-02: Cost of a resumed run is reported as 0 or as the whole session

A trial seeded with `load_trajectory` runs Claude Code with `--resume`.
Claude Code 2.1.278's final stream-json `result` event then reports `total_cost_usd: 0` and `num_turns: 0`, so `agent_result.cost_usd` is 0 or a litellm estimate.
Harbor's per-model `model_usage` counts every assistant message in the session file, including the loaded history, so it charges the resumed trial for the source trial's tokens (`resume-kv-sonnet-v1`: $0.036 of Haiku usage from the source trial).
Workaround: price only the assistant messages after the cut point of the loaded session, deduplicated by message id, with litellm's table.
Upstream option: Harbor counts usage only for messages written after the seeded session's last event.

## 2026-10-03: No job-level network policy

A task's network policy (`public`, `no-network`, `allowlist`) comes only from its `task.toml`. A job config has only `extra_allowed_hosts`, which merge into a non-public policy and are ignored with a warning on a `public` task (`harbor/trial/network_policy.py`, `merge_extra_allowlists`). All 23 TB 4.0 tasks of ADR-0012 are `public`, so a run cannot restrict the agent's network without changing the tasks.
Claude Code runs inside the trial container, so a restricted run needs an allowlist with the Anthropic API host, applied to the agent phase only: two of the 23 verifiers install packages at verify time.
Workaround: none yet; deferred to a later experiment (`docs/research/2026-10-03_tb40-setup-vs-anthropic.md`).
Upstream option: a job-level override of the agent-phase policy, e.g. `agent.network_mode` plus `allowed_hosts` in the trial config.

## 2026-10-07: The prompt travels as one command-line argument, capped at 128 KiB

`ClaudeCode.run` puts the whole prompt in one environment variable, `HARBOR_CLAUDE_CODE_INSTRUCTION_<hex>`, which the Docker environment passes as one `docker compose exec -e KEY=VALUE` argument; the command then copies it into a shell variable and pipes it to `claude`.
Linux caps every argument and environment string at 128 KiB (`MAX_ARG_STRLEN`, 131,072 bytes including the terminating NUL), on the host's `docker` command line and again when the process starts in the container.
A longer prompt cannot start. ADR-0012's `traj-text` prompts carry a transcript of a failed trial: rendered from the tool output text the agent saw, as built, the largest is 7,402 bytes under the cap; rendered from Harbor's ATIF observation `content`, 2 of the 21 are over it.
Workaround: `trajlab.capture.repair.RepairClaudeCode` overrides `exec_as_agent`. For every `traj-text` run it uploads the prompt to `/tmp/trajlab-prompt-<hex>.txt` (chowned to the agent user, mode 600), drops the variable, and rewrites the command's `<var>="$<VAR>"; unset <VAR>; ` to read and remove the file before `claude` starts. It raises if the command no longer has that exact form; a unit test runs Harbor's own `run()` against a fake environment so an upgrade that changes the form fails the test.
Upstream option: deliver the prompt as an uploaded file or on stdin of the exec, not in an argument.
