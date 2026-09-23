# Checkpoint stack and platform research

Date: 2026-09-23.
Scope: questions Q1-Q6 from the platform planning note (StateFork/Waypoint requirements, CPU architecture, Harbor's platform handling, Terminal-Bench on arm64, CRIU on macOS).
Method: Agent A dissected source code; Agent B researched primary online sources.
This document reconciles the two and supersedes both individual reports.

Agent A's source base, vendored as submodules on this branch:

- `vendor/statefork` at `9fdb324` (tag v0.7.0), `vendor/waypoint` at `dcb6a7b` (tag v0.7.0).
- Harbor pinned install in `.venv/lib/python3.12/site-packages/harbor/` (0.23.x).

Legend: [src] = read in source at the pinned commits, [doc] = documented in a primary online source, [user] = reported by users, [inf] = inferred.
`file:line` citations are relative to `vendor/` for StateFork/Waypoint and to the installed package root for Harbor.

## Headline reconciliations

The two reports agree everywhere they overlap, and together they overturn three assumptions in the planning note and repo docs:

1. **Harbor does not pass `--platform` for Docker task containers.**
   The planning note's premise "forcing amd64 on the Mac probably doesn't work, because Harbor passes --platform explicitly" is wrong for the Docker environment; that flag exists only in the SkyPilot environment. [src]
   Agent B independently found a user repro (`DOCKER_DEFAULT_PLATFORM=linux/amd64 harbor run ...` on Apple Silicon, terminal-bench issue #1601) that achieved reward 1 on an amd64-only task. [user]
   The source explains why the env var works: see Q4.
2. **Waypoint is the checkpoint engine, not a layer above StateFork.**
   Both agents found the relationship inverted from the planning note's framing: Waypoint is a standalone Go binary on CRIU + OverlayFS with the checkpoint DAG inside it, and StateFork wraps it as one pluggable backend. [src][doc]
3. **"The SOSP paper" is an arXiv position paper.**
   The closest publication is arXiv:2510.05556, "Toward Systems Foundations for Agentic Exploration" (Xu, Zhou, Wu, Kaffes, submitted 2025-10-07); Agent B found no SOSP proceedings entry, only a DAPLab SOSP 2025 attendance post. [doc]
   This citation appears in `docs/decisions/0001-checkpoint-granularity.md:6` ("~1.7 s per 2 GB per the SOSP paper") and in the 2026-09-22 meeting notes; both should be amended to cite the arXiv paper.

## Q1: Does StateFork's attach mode support plain Docker containers started by Docker Compose?

**Answer: yes for filesystem-only snapshots, no for memory state; StateFork has no CRIU-for-Docker attach path at all.**

Agent B flagged "what does `docker_attach` actually snapshot (docker commit vs CRIU)" as the decisive detail; the source settles it:

- `docker_attach` maps to `ContainerAttachManager` (`statefork/controller/__init__.py:50-56`), which snapshots any container by name with `docker commit` (`statefork/controller/container_env_manager.py:120`). [src]
  Compose containers are plain named containers, so attach works, but this captures the filesystem only and is functionally identical to trajlab's own `docker_commit` backend.
  It needs Docker CLI permissions only: no root, no CRIU, no special runtime.
- The only container-level memory capture is `hybrid_attach`, and it is **Podman-only**: `podman container checkpoint -e <path> --leave-running` (`statefork/controller/hybrid_env_manager.py:66-69`), requiring root, the `runc` OCI runtime instead of `crun`, and CRIU (`statefork/README.md:146-149`). [src]
  No minimum kernel or CRIU version is enforced in StateFork code; that is delegated to CRIU itself.
- `criu_attach` dumps a raw PID (`criu dump -t <pid> --shell-job --leave-running`, `statefork/controller/criu_env_manager.py:88-96`); it is not container-aware. [src]
- `gvisor_attach` uses Docker's experimental `docker checkpoint create` (`statefork/controller/gvisor_env_manager.py:73`), but only for containers created with `--runtime runsc --network host` (`statefork/controller/gvisor_env_manager.py:165`), and the documented networking catch-22 breaks checkpoint/restore (`statefork/README.md:196-201`). [src]
- Agent B's online findings are consistent: Docker's `docker checkpoint` is still experimental in 2026, requires CRIU >= 2.0, and cannot checkpoint containers with an external terminal (Docker docs); criu.org/Docker explicitly recommends Podman over Docker for checkpoint/restore. [doc]
  The terminal limitation matters for us because agent tasks run interactive PTY sessions.
- Caution for any future restore work: `ContainerAttachManager._core_restore` does `docker rm -f` then a bare `docker run` (`container_env_manager.py:134-136`), which would discard a compose container's mounts, networks, and labels. [src]
  Irrelevant for capture (we never restore), but it rules out StateFork-managed restore of Harbor containers without additional work.

Confidence: high; the manager code is short, direct subprocess calls.
Needs a live Linux test: whether `hybrid_attach` works against a Harbor trial when Harbor runs on its Podman runtime with runc and root; Harbor does ship Podman support in `environments/docker/runtime.py`, but the combination is unverified.

## Q2: Which CPU architectures do StateFork and Waypoint support?

**Answer: both are Linux-only; both support x86_64 and arm64, with one version gate on arm64.**

- Waypoint's host check handles `x86_64|amd64` and `aarch64|arm64` (`waypoint/setup:277-284`) and requires **CRIU >= 4.0 on aarch64**, because older CRIU does not checkpoint ARM pointer-authentication (PAC) keys (`waypoint/setup:201-205`, `waypoint/README.md:107`). [src]
- StateFork is pure Python 3.10+ shelling out to `docker`/`podman`/`criu`/`waypoint`, with no architecture-specific code anywhere; its effective support is whatever the chosen backend supports on that host. [src]
- CRIU upstream supports x86, arm, aarch64, ppc64, s390, and more per the criu Makefile. [doc]
- Conclusion shared by both agents: an arm64 Linux host is not ruled out by the checkpoint stack.
  The x86_64 pressure comes entirely from Terminal-Bench task images (Q5), not from StateFork/Waypoint/CRIU. [inf]

Confidence: high.

## Q3: Is Waypoint built on StateFork, and does it need container-creation-time configuration?

**Answer: no, the relationship is inverted, and Waypoint cannot attach to a Docker container at all.**

- Waypoint is a standalone Go binary (`waypoint/go.mod` depends only on `creack/pty` and `golang.org/x/sys`) combining CRIU for process state, OverlayFS for filesystem deltas, RPC-style PTY session management, and an immutable checkpoint DAG with concurrent live forks since v0.7.0. [src][doc]
- Waypoint's own README states StateFork was the *analysis tool* used to motivate Waypoint's minimalist design (`waypoint/README.md:31-34`). [src]
- StateFork wraps the Waypoint binary as one backend via subprocess (`statefork/controller/waypoint_env_manager.py:47-63`); it probes the binary at startup and refuses pre-v0.7.0 builds. [src]
- The capture-phase implication is stronger than "needs configuration at container creation": a Waypoint session is created only by `waypoint init <rootfs>` or `waypoint build <Dockerfile-dir>` (via buildah), which stages its own rootfs and starts its own bash under Waypoint's PTY management (`waypoint/README.md:220-256`). [src]
  `waypoint_attach` attaches to an existing *Waypoint session id*, not a container or PID; the `target_pid` argument is explicitly obsolete (`statefork/controller/waypoint_env_manager.py:223-250`, `statefork/README.md:95`). [src]
  So using Waypoint means the agent runs inside a Waypoint session *instead of* a Harbor Docker container.
  That is exactly the deferred `WaypointEnvironment` ADR, and it would replace Harbor's environment, not decorate it.
- Waypoint requires root on a Linux host; StateFork's Waypoint backend must itself run as root (`sudo` prefixing is unsupported because a timed-out exec cannot be cancelled through sudo, `statefork/README.md:155`). [src]
- ADR-0002's "virtual snapshots disabled" maps to a real switch: the `Decider` strategy (`statefork/decider/`), default `AlwaysTrueDecider` (always physical). [src]
  Better still, the forkable Waypoint manager *rejects* any other decider at construction (`statefork/controller/README.md:100`), so physical-only is enforced, not merely configured.

Confidence: high on the architecture.
Needs a live test: whether Claude Code's npm-installed CLI runs correctly inside a Waypoint-built rootfs (later ADR, not this phase).

## Q4: Is Harbor's platform choice final, or overridable?

**Answer: Harbor's Docker environment pins no platform anywhere; the container follows the Docker daemon's default, and two override channels exist for both built and prebuilt tasks.**

- The two compose templates defining the `main` service carry no `platform:` key: `environments/docker/docker-compose-build.yaml` (bare `build:` block) and `docker-compose-prebuilt.yaml` (`image: ${PREBUILT_IMAGE_NAME}`). [src]
- Built images go through `docker compose build` (`environments/docker/docker.py:981`); prebuilt images are pulled implicitly by `docker compose up` (`docker.py:1007`).
  `_run_docker_compose_command` (`docker.py:644-668`) adds only `--project-name`, `--project-directory`, and `-f` files, never a platform flag. [src]
- The single explicit platform in the Docker environment is the egress-control sidecar build, pinned to the daemon's own platform detected at runtime (`docker.py:585`, `environments/docker/utils.py:35-54`). [src]
- No config surface names a platform: neither the task-level `EnvironmentConfig` (`models/task/config.py:422-478`) nor the trial-level one (`models/trial/config.py:197-215`) has an arch or platform field, and the package contains no `DOCKER_DEFAULT_PLATFORM` handling of its own. [src]
- Override channel 1, env var: compose subprocesses inherit the host environment (`_compose_env_vars` merges `os.environ` at `docker.py:626`; passed at `docker.py:670`), so `DOCKER_DEFAULT_PLATFORM=linux/amd64` set before `harbor run` reaches `docker compose build` and `up`. [src]
  Agent B found this verified in the field: the terminal-bench #1601 repro used exactly this env var on Apple Silicon and the amd64-only task scored reward 1. [user]
- Override channel 2, compose overlay: a task's own `environment/docker-compose.yaml` and run-level `extra_docker_compose` files (`models/trial/config.py:212`, CLI `--ek`) are stacked as later `-f` files (`docker.py:393-395`) after the build/prebuilt template (`docker.py:387`), so `services.main.platform:` there wins under compose merge rules; this applies identically to built and prebuilt tasks. [src]
- Origin of the wrong premise: `environments/skypilot.py:145` defaults `platform="linux/amd64"` and appends `--platform` to its remote builds (`skypilot.py:266-267`); that is the SkyPilot environment only. [src]
- Broader point: Harbor treats the environment itself as pluggable (docker, podman runtime variant, skypilot, kata, apple_container, cua_cloud, plus custom classes via `--env module:Class`), so environment flexibility is a design property, not an accident. [src]

Confidence: high for "no explicit platform" and the overlay channel; medium for the env-var route across compose versions, though the #1601 field report substantially de-risks it.
Needs a live test on our own setup: run hello-world with `DOCKER_DEFAULT_PLATFORM=linux/amd64` on an arm64 Mac, check `docker inspect --format {{.Architecture}}`, and confirm `_validate_image_os` (`docker.py:983-990`) does not object.
Emulation caveat unchanged: amd64-under-Rosetta/QEMU distorts `capture_ms` and timeouts, so emulated runs are for the dev loop only, never for recorded corpora.

## Q5: Terminal-Bench tasks broken on arm64 (Agent B, online only)

The upstream tracker (harbor-framework/terminal-bench) has open platform issues as of 2026-09-23; the ones confirmed in Agent B's report: [doc]

| Task | Issue | Failure mode on arm64 |
| --- | --- | --- |
| fix-uautomizer-soundness | #1601 | Silently unsolvable: all 22 verdicts UNKNOWN, oracle scores 0, indistinguishable from an ordinary agent failure |
| coq-block-bound | #1802 | Not multi-platform |
| hof-topology-interpenetration | #1803 | Not multi-platform |

Agent B's full table cited roughly six tasks across issues #1601, #1793, and #1802-#1806, but the transcription was truncated in transit; re-pull the complete list from the tracker before finalizing the task subset.
Two standing conclusions:

1. There is no guarantee the tracked issues are the complete set; #1601's silent-zero failure mode means unknown others may exist, so an arm64-run corpus has unquantifiable task-level bias. [inf]
2. This, not the checkpoint stack, is the reason recorded corpora belong on x86_64. [inf]

## Q6: CRIU under macOS container runtimes (Agent B, online only)

- Docker Desktop: not feasible; CRIU is absent from the VM (docker/for-mac#1059 open since 2017; linuxkit#3263 went nowhere). [doc][user]
- OrbStack: `docker checkpoint` with CRIU is documented as working ("Docker's experimental mode is enabled by default", OrbStack docs). [doc]
  This softens "CRIU on macOS infeasible", but only inside OrbStack's VM; amd64 containers there run via Rosetta, and CRIU checkpointing a Rosetta-translated process is almost certainly unsupported. [inf]
- Colima/Lima: no documented CRIU support; installing CRIU inside the VM is feasible in principle, unverified. [inf]
- Structural caveat for all of them: the CRIU stack must run as root inside the VM's Linux kernel, and the resulting corpus is still arm64 (or emulated amd64).
  macOS options can serve the dev loop, never problem 2 (architecture fidelity). [inf]

## CRIU baseline facts (Agent B) [doc]

- Kernel: Linux v3.11+ with specific config options; `CONFIG_MEM_SOFT_DIRTY` for incremental dumps, `CONFIG_USERFAULTFD` for lazy migration (criu.org/Installation, criu.org/Linux_kernel).
- `docker checkpoint`: experimental, CRIU >= 2.0, fails on containers with an external terminal (Docker docs); criu.org recommends Podman for checkpoint/restore.

## Consequences for the plan

The proposed direction survives with corrections:

- **Dev on Macs, record on one x86_64 Linux host with root, record the Docker server's OS/arch in the corpus manifest: supported by everything found.**
  Additionally, the Mac dev loop *can* exercise real amd64 task images via `DOCKER_DEFAULT_PLATFORM` (Q4), just never for recorded corpora.
- **Step 8 ("statefork backend, attach mode") has exactly three concrete shapes**, and the choice deserves its own ADR before implementation:
  1. `docker_attach`: works today against Harbor containers, but is `docker commit` underneath, redundant with our `docker_commit` backend.
  2. `hybrid_attach`: real memory capture, but requires migrating Harbor runs to Podman + runc + root on Linux.
  3. Waypoint: full state and forks, but requires the deferred `WaypointEnvironment` ADR because Waypoint replaces the container environment entirely.
- **Doc amendments to file**: ADR-0001 and the 2026-09-22 meeting notes should cite arXiv:2510.05556 instead of "the SOSP paper"; the planning note's ADR-0003 framing ("Waypoint keys state by named checkpoint in a DAG") stands, but any wording implying Waypoint layers on StateFork should be corrected.
- The arXiv abstract's claim that "generic tools such as CRIU or container commits are not fast enough even in isolated testbeds" is the authors' own motivation for Waypoint; our ADR-0001 capture-cost estimate (~1.7 s per 2 GB) should be re-attributed to that paper. [doc]

## Sources

Source code at pinned commits: `vendor/statefork` (9fdb324), `vendor/waypoint` (dcb6a7b), Harbor 0.23.x installed package.
Online: DAPLab StateFork post, StateFork repo, Waypoint repo, arXiv:2510.05556, DAPLab at SOSP 2025 post, criu Makefile, criu.org/Installation, criu.org/Linux_kernel, criu.org/Docker, Docker checkpoint docs, docker/for-mac#1059, linuxkit#3263, OrbStack docs, terminal-bench issues #1601/#1793/#1802-#1806, mixster.dev Terminal-Bench on macOS guide.
