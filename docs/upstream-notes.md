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
