# Paper notes: capture method

What the Dec 4 paper can claim about how trajectories and checkpoints are captured, the evidence for each claim, and what is still missing.
Every number here points at the ADR, script, or corpus manifest it comes from, so a sentence in the paper can be traced to data.
Terms follow `docs/glossary.md`.
Last updated 2026-10-02; add to it as evidence changes, and do not delete superseded entries, mark them.

## Claims and their evidence

### C1. Every state-changing tool call is joined to the environment state after it

- **Mechanism:** a Claude Code hook on `PostToolUse` and `PostToolUseFailure` blocks the agent until the host watcher has measured the container and, if needed, committed it (ADR-0006, ADR-0010, `docs/checkpoint-protocol.md`).
- **Join key:** Claude Code's `tool_use_id`, which is also ATIF's `tool_call_id` (`docs/harbor-facts.md`, "The join key").
- **Evidence:** in the acceptance runs every `.req` got an `.ack` with no timeouts (ADR-0006 and ADR-0008 acceptance sections; `tb21-preinstall-check-v1`: 8 of 8 hooked calls). Since ADR-0010 each answered call also has a `CallRecord`; the failing-call probe had records for all 3 of its calls.
- **In the enriched trajectory:** every checkpoint of every finished trial in `tb21-audit-v1`, `tb21-change-gate-v1`, and `tb21-preinstall-check-v1` joined to the agent step owning its `tool_call_id`; postprocess refuses a trial otherwise (ADR-0003, 2026-10-01 amendment).
- **Caveat to state:** before 2026-10-01 a tool call that ended in error was not hooked; `tb21-audit-v1` and `tb21-change-gate-v1` predate the fix (ADR-0010, amendment). Only corpora captured after commit `06e2061` support C1 without that caveat.

### C1b. The state the agent leaves behind is always recorded

- **Mechanism:** the hook also fires on `Stop` and `StopFailure`; the watcher records the end-of-turn state as a stop checkpoint, or as a stop record naming the checkpoint that already holds it (ADR-0011).
- **Why it matters:** this is the state the verifier grades, and it includes writes by background jobs that outlive the last hooked call.
- **Evidence:** a real trial whose background job kept writing after the last hooked call: the stop checkpoint caught `~/app/tick.txt` changing, which no other checkpoint held (ADR-0011, Verification).

### C2. A call gets a checkpoint only if it changed the filesystem, and this is measured, not inferred

- **Why not by tool name or command text:** in our TB 2.1 trials Claude Code made 32 Bash calls and no `Read` or `Grep` calls, inspecting files through Bash and mixing reads and writes in one call (ADR-0010, Context). Parsing Bash would be an unverifiable heuristic.
- **Rule:** compare a GNU `find` listing of the container with the listing at the last checkpoint; files by type, mode, owner, size, change time, and link target; directories by type, mode, and owner (`src/trajlab/checkpoint/changes.py`).
- **"Minor" is "changed nothing":** no size threshold, because a one-byte edit can be the consequential action (ADR-0010, item 4).
- **Change time, not modification time:** back-dating with `touch -d` or `cp -p` cannot hide a write; verified by probe (below).
- **Exclusions:** six harness path patterns, each justified, found from the top layers of real checkpoints (ADR-0010, item 5). They affect the decision only; checkpoints still contain them. Report a sensitivity analysis with an empty exclusion list.

### C3. The detector is sound against an independent ground truth

- **Ground truth:** content hashes of the top layers of consecutive checkpoints in an audit run, sharing no code with `find` (`scripts/2026-09-30_audit_detector.py`).
- **Probe validation:** 11 scripted calls on real overlayfs, each targeting one rule, all judged as designed on arm64 and amd64 images (`scripts/2026-09-30_change_gate_check.py`; ADR-0010, Validation). The audit script reproduced the probes' known truth exactly: 4 content, 2 timestamp-only, 4 none.
- **Real agent calls** (`tb21-audit-v1`, 3 tasks, 14 calls compared):

| Detector \ truth | content changed | only times changed | nothing changed |
| --- | --- | --- | --- |
| changed | 9 | 1 | 0 |
| unchanged | 0 | 0 | 4 |

- **Claim supported:** zero misses, so an unchanged call's join to the previous checkpoint was exact in every case. The one disagreement is conservative.
- **Not yet supported:** a rate with a confidence interval; 14 calls is too few (see Open work).

### C4. Checkpoints hold the trial's own changes, not the harness's agent install

- **Problem measured:** on Harbor's stock image every checkpoint re-captured the runtime Claude Code install: 1.33 GB and 36 s per commit on hello-world (ADR-0006, Acceptance run).
- **Method:** trials run on a cached derived image that Harbor's own `ClaudeCode.install` built from the unmodified task image; Harbor's install then skips itself (ADR-0008).
- **Evidence:**

| Trial | Reward | Agent setup | Layer per checkpoint | Commit |
| --- | --- | --- | --- | --- |
| hello-world, stock image | 1.0 | 44 to 68 s | 1.33 GB | 36 s |
| hello-world, derived image | 1.0 | 0.3 s | 100 KB | 5.3 s |
| kv-store-grpc, derived image | 1.0 | 1 s | 57 MB | 5.5 to 15.6 s |
| write-compressor, derived image | 1.0 | 1 s | 106 to 147 KB | 3.7 to 6.1 s |

  Sources: `hello-world-checkpoint-v1` (stale job dir), `hello-world-preinstall-v1`, `tb21-preinstall-check-v1`; ADR-0008, Acceptance.
- **Task behavior unchanged:** both TB tasks scored 1.0 with and without the derived image (`dev-tb21-strict-check` vs `tb21-preinstall-check-v1`). This is 2 tasks, 1 attempt each: evidence of no breakage, not of equivalence.

### C5. Every trial runs one pinned agent binary

- Claude Code 2.1.278 is pinned in `trajlab.capture.pins`; `trajlab run` refuses others, and each trial container is checked for the version and the sha256 of the binary recorded in its derived image before the agent runs (ADR-0009).
- The binary hash differs by platform and install path for the same version (arm64, amd64, npm on Alpine); report the hash per platform.
- Verified by tampering: an altered recorded hash failed the trial at environment start (ADR-0009, Consequences).

### C6. A checkpoint restores to a state the task's verifier grades like the live one

- **Method:** start a fresh container from each checkpoint image, with no Harbor and no agent; check that every path the calls added up to that checkpoint is present and every path added later is absent, compare `Write` contents byte for byte, and run the task's own `tests/test.sh` (`scripts/2026-10-02_restore_check.py`).
- **Evidence** (`tb21-demo-v1`, `schemelike-metacircular-eval`, 1 trial): all 5 checkpoints restored in 0.1 to 0.24 s and passed the path checks; the verifier scored 0.0 on checkpoint 1, 1.0 on checkpoint 3, and 1.0 on checkpoint 5, matching Harbor's reward of 1.0.
- **By-product:** checkpoint 3, 4 min 53 s into a 6.6 min agent run, already passes the verifier; the rest was the agent checking its work.
- **Superseded 2026-10-02:** agent resume was then untested; C7 below now covers it.

### C7. An agent resumed from a checkpoint continues the work

- **Method:** the resumed trial starts from the checkpoint image (`trajlab.capture.resume:CheckpointResumeEnvironment`) and from Claude Code's native session cut right after the checkpointed call, loaded with Harbor's `load_trajectory` (`claude --resume`); `scripts/2026-10-02_resume_trial.py`.
- **Evidence:** 4 of 4 resumed trials passed.
  `resume-demo-v1`: Sonnet from #3 of its own schemelike trial, 1.0.
  `resume-kv-sonnet-v1` and `resume-kv-haiku-v1`: Sonnet and Haiku from #6 of Haiku's failed kv-store-grpc trial (`tb21-haiku-v1`, 0.0), both 1.0.
  `resume-scheme-sonnet-v1`: Sonnet from #10 of 16 of Haiku's failed schemelike trial (0.0, 10 of 63 tests), 1.0 with 63 of 63; its first act was to revert Haiku's edit to the reference `interp.py`.
- **Confound to state:** a resumed agent sees messages the original did not: Claude Code's notices about stopped background tasks, its "Continue from where you left off" turn, and Harbor's re-sent instruction. The kv-store failure was a server run as a Claude Code background task, which dies when the agent exits; the stopped-task notice alone led both models to restart it, so that pair shows a resume effect, not model strength.
- **Cost:** Claude Code reports $0 for a `--resume` run and Harbor counts the loaded history (`docs/upstream-notes.md`); resumed costs here count only messages after the cut.

## Reproducibility facts to report

- Harbor 0.23.0, pinned, unpatched; all custom pieces attach from outside (CLAUDE.md constraint 1).
- Claude Code 2.1.278 (ADR-0009); `CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000` (ADR-0005); model `anthropic/claude-sonnet-5-5`, reasoning effort high in TB configs.
- Terminal-Bench 2.1, dataset digest `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a` (`configs/harbor/tb21-*.json`).
- Capture policy per corpus: `checkpoint_gate`, `checkpoint_every` in the corpus manifest; derived-image recipe version 2 (`trajlab.capture.preinstall.RECIPE_VERSION`).
- Backend: `docker commit`, filesystem only (ADR-0004); Docker Desktop with the containerd image store on the development Mac.
- Every number in the paper should cite a `corpus_id`; manifests in `corpus/manifests/` record the repo commit that produced them.

## Threats to validity

- **Filesystem only.** Process and memory state are not captured (ADR-0004). A call that only starts a server, as the agent did with `nohup` in kv-store-grpc, counts as unchanged.
- **Background writes.** Files written by background processes between hooked calls are attributed to the next measured call.
- **Clock.** A root agent that rewinds the system clock could evade change-time comparison; we know of no case, but it is possible.
- **Harness exclusions.** Excluding Claude Code's own paths assumes the agent's task-relevant work never lives there; the agent did read `/tmp/claude-0/.../tasks/*.output` (background-task output) in one trial. Report the sensitivity analysis.
- **`__pycache__`.** Running Python writes bytecode caches, even under `/usr/local/lib`; they count as changes. State this, and consider reporting change counts with and without them.
- **Derived image timing.** The install runs at image-build time, not trial time; package versions could differ if a mirror changed in between (ADR-0008, Consequences).
- **Excluded tasks.** `qemu-alpine-ssh` and `qemu-startup` (Debian 11) cannot install Claude Code at all since Debian 11's LTS ended; they are out of any corpus (`docs/upstream-notes.md`). Report the task count as 87 of 89.
- **Capture overhead grows with trial length.** The hook held the agent 3.7% of a 7-minute Sonnet trial but 16% of Haiku's 24-minute `write-compressor` trial (64 checkpoints, about 2.2 s each; `tb21-haiku-v1`). Report it per trial.
- **Platform.** All measurements so far are from one Mac running amd64 tasks under emulation. Commit and detection times are expected to differ on the Linux server.
- **Final state.** Since ADR-0011 the end-of-turn state is recorded, including background writes before the turn ends; anything a background process writes after that, and the verifier's own effects, are not.

## Open work before claims are paper-ready

1. **Larger audit.** Rerun `--gate audit` on a slice of at least 10 to 20 TB 2.1 tasks after the failed-call fix, and report the confusion table with an interval for the miss rate.
2. **Exclusion sensitivity.** Recompute the audit table with an empty exclusion list.
3. **Per-tool change rates.** From `calls.jsonl`: the share of Bash, Write, and Edit calls that changed the filesystem; a likely result in its own right.
4. **Linux numbers.** Repeat the C4 table and detection timings on the Linux server.
5. **Final-state checkpoint.** Done in ADR-0011 (`Stop` and `StopFailure` hooks); report how often the stop checkpoint differs from the last call's, i.e. how often background work changed the final state.
6. **Join in the trajectory.** Done in build-order step 7 (ADR-0003, 2026-10-01 amendment): `trajlab postprocess` writes `trajectory.enriched.json` with checkpoint and compaction steps and each call's `CallRecord`, and refuses a trial whose records do not all join; the paper's figures should come from that file.
7. **Compaction on real data.** No trial so far has compacted, so compaction recovery is tested only on synthetic events run through Harbor's converter; confirm it on the first long trial that does.
8. **Agent resume from a checkpoint.** Done (C7); open: a resume protocol that controls for the added messages, and resumed-run cost accounting in the pipeline.
