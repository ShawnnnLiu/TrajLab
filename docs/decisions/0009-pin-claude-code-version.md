# ADR-0009: One pinned Claude Code version for every capture

Status: proposed (2026-09-30).

## Context

Harbor installs whatever Claude Code version a job config's `agents[0].kwargs.version` names, and the latest release when it names none.
The Sep 29 dev job ran unpinned and got 2.1.285; every job config since pins 2.1.278, but only by convention, one file at a time.
With the pre-installed image (ADR-0008) a mismatch is also silent: Harbor finds the wrong version in the derived image, reinstalls at trial time, and the install lands back in the writable layer and in every checkpoint.
The agent version shapes behavior, so trials on different versions do not belong in one corpus.

## Decision

1. **The pin lives in code**: `trajlab.capture.pins.CLAUDE_CODE_VERSION`, currently `2.1.278`, the version every corpus so far ran and the one the Sep 29 hang investigation cleared.
2. **`trajlab run` refuses** a job config whose Claude Code agent (Harbor's `claude-code`, or an import path to a subclass) is unpinned or pinned to anything else.
3. **A test checks every committed job config** under `configs/harbor/` against the pin, so changing the pin moves all configs in the same PR.
4. **The pre-installed image is locked to the pin and to one binary.**
   The build refuses any other version, and each derived image carries the sha256 of the installed `claude` binary as the `trajlab.preinstall.agent_sha256` label; `PreinstallRecord.agent_sha256` repeats it per trial.
5. **Every trial container is checked before the agent runs.**
   After Harbor starts it, `PreinstalledDockerEnvironment` runs Harbor's own version command and the same sha256 command as the agent user; a different version or a different binary fails the trial at environment start, before any model call.

## Consequences

- Changing the version is a deliberate act: edit the pin, every config, and this ADR's successor, then use a new `corpus_id`.
- Harbor run directly (`harbor run`, not `trajlab run`) is not checked by item 2, but a pre-installed trial still fails item 5 when off the pin.
- The binary hash also catches a release re-published under the same version string, which Harbor's version check alone would accept.
- The hash differs by platform and install path for the same version: on 2026-09-30, 2.1.278 hashed `7de6cab1...` as the arm64 native binary, `5c473593...` as the amd64 one, and `cc8266db...` as the npm package on Alpine.
  So the lock is per derived image: a trial runs exactly the binary its derived image recorded.
- Verified end to end: a derived image whose recorded hash was altered made the trial fail at environment start with both hashes in the error, and Harbor's finalize tore the container down.
- Derived images built before this ADR lack the hash label; `RECIPE_VERSION` moves to 2, so they are never reused.
