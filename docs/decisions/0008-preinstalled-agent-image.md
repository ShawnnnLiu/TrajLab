# ADR-0008: Run trials on a derived image with the agent pre-installed

Status: proposed (2026-09-30).

## Context

The step-6 acceptance run (ADR-0006) showed that each checkpoint re-captures Harbor's agent install: 1.33 GB and 36 s per `docker commit` on hello-world.
Harbor installs Claude Code and its system packages into the running trial container during `agent.setup()`, so they sit in the container's writable layer, and `docker commit` captures the whole writable layer every time.
ADR-0007 reduced the number of checkpoints; this ADR removes the install from each one.

Harbor 0.23.0 skips its install when Claude Code at the requested version is already on the agent user's `PATH` (`ClaudeCode.install`, `_installed_claude_satisfies_version`).
A trial container started from an image that already holds the install therefore runs Harbor's setup as a no-op, and its writable layer holds only what the agent and the trial do.

## Decision

1. **A custom Harbor environment**, `trajlab.capture.preinstall:PreinstalledDockerEnvironment`, subclasses Harbor's `DockerEnvironment` and is selected in the job config with `environment.import_path`.
   Harbor is not patched (CLAUDE.md constraint 1).
2. **Terminal-Bench's images are not modified.**
   The task image is built or pulled exactly as Harbor would: the task's `docker_image`, or `docker build` of the task's `environment/` dir when the task has no prebuilt image or `force_build` is set.
   The environment then derives a new image from it and starts the trial from the derived image through Harbor's own prebuilt-image path.
3. **The derived image is made by Harbor's own install code.**
   The environment starts a temporary container from the task image, runs `ClaudeCode.install` against it through a small adapter that implements the one environment method install uses (`exec`, as `bash -c`, under the task's agent user and environment variables, like Harbor's), checks that Harbor's own "already installed" test now passes, and commits the container.
   The derived image keeps the task image's `CMD`, entrypoint, and working directory.
   No install recipe is copied into trajlab, so the derived image holds what a runtime install would have added.
4. **Derived images are cached** under `trajlab-preinstalled:<tag>`, where the tag hashes the task image's content (its filesystem layers and runtime config), the agent name and version, the Harbor version, and a recipe version.
   Not the task image id: BuildKit attaches provenance with a build timestamp, so rebuilding an unchanged Dockerfile yields a new id over identical layers, and an id-keyed cache rebuilt the derived image on every trial (found by the integration check).
   A trial whose derived image exists reuses it; concurrent trials of one task build it once.
5. **The agent version must be pinned** in the job config; an unpinned install would be "latest at build time" and could not be cached honestly.
6. **Each trial records what it ran on** in `<trial>/trajlab-preinstall.json`, a `PreinstallRecord`: task image and id, derived image and id, agent and version, Harbor version, and whether the image came from the cache.
7. **Unsupported cases fail loudly**, before the trial starts: Windows containers, agents other than Claude Code or a subclass of it, an unpinned version, and tasks whose own `docker-compose.yaml` sets `build` or `image` on the `main` service, since Compose would then build the task image under the derived tag.

## Consequences

- Checkpoints hold only the trial's own changes, so per-call checkpoints (ADR-0007 with N = 1) may become affordable again; choosing N stays a corpus decision.
- The verifier sees the same packages as before: the install is the same code, run earlier, on the same task image.
  It can differ only where a package mirror changed between image build and trial time, which the cache makes rarer, not more common.
- Agent setup time drops for every trial after the first of each task.
- The trial's network policy does not apply to the one-time install, which always needs the network; Harbor's runtime install needed it too.
- The environment depends on Harbor internals beyond the public interface: `DockerEnvironment._env_vars.prebuilt_image_name`, `_main_image_name`, and `ClaudeCode._installed_claude_satisfies_version`.
  A Harbor version bump must re-run `tests/test_capture_preinstall.py` and `scripts/2026-09-30_preinstall_check.py`.
- Running on the derived image is a capture change: corpora that use it need a new `corpus_id`, and the manifest's `environment_type` records the import path.
- Derived images accumulate locally, one per task and agent version; `docker images trajlab-preinstalled` lists them.
