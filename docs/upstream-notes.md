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

## 2026-10-07: Regrade refuses recorded states with absent artifacts

`RegradeTrial._artifact_coverage_problems` (`harbor/trial/regrade.py`) refuses any manifest entry with status `failed`, although a live trial grades such a trial: the uploader simply skips the absent path. Two `tb40-sonnet-v2` trials end with absent artifacts (`pretrain-shard-corruption` `model_final.pt`, `train_metrics.json`), and early timeline points lack outputs not yet written.
Workaround: `trajlab.groundtruth.replay.ReplayTrial` drops the problem for sources it knows were absent (ADR-0013).
Upstream option: accept `failed` entries in regrade, as the live uploader does.

## 2026-10-07: Single-step regrade dereferences symlinks

`RegradeTrial._seed_from_source` copies the source's `artifacts/` with `preserve_symlinks=False`; `MultiStepRegradeTrial` and a live trial's upload keep links. A relative link becomes a copy and an absolute link resolves on the host.
Workaround: `ReplayTrial` seeds with `preserve_symlinks=True`. No state in `tb40-sonnet-v2` has a symlink.
Upstream option: keep symlinks in single-step regrade too.

## 2026-10-07: An environment-start timeout is reported as a verifier timeout

`RegradeTrial._run` turns any `asyncio.TimeoutError` from `_run_separate_verifier` into `VerifierTimeoutError`, including a timeout while the verifier environment starts (`trial.py`, `env.start` under `wait_for`).
Workaround: replays classify their outcome from the verifier output instead (`verdict`, `no_verdict`, `infra`).
Upstream option: a distinct exception for environment start.

## 2026-10-07: Cancelling a regrade can hang while a verifier command runs

Cancelling a running regrade (SIGINT) left the process waiting and its verifier containers running; the exec'd test process does not stop with the client.
Workaround: `trajlab gt replay` takes down in-flight compose projects itself after a grace period, and the admission ledger takes down projects of dead processes.
