# Verifier-score trend over retry chains: decisions and setup (2026-11-13)

Status: design decided by the owner on 2026-11-13; run 2026-11-13 to 2026-11-14, results in `2026-11-14_score-trend-chains-results.md`.
A probe on the development Mac, not a preregistered run.
Written as a handoff: a separate session does the setup and governance.
Open items that need the owner are in "Open decisions"; items stated but not checked are marked "not verified".

## Question

Does a growing record make successive retries score higher, or does it lift the retry once and then plateau?
The earlier probes point to a plateau: record-arm retries restore the failed attempt and land near its score (`2026-11-06_night-run-graded-retries.md`), and pass or fail hides that because a pass is a threshold.
Control has no memory, so its per-attempt score should be flat over the attempt index; a slope in record-engine is evidence that information accumulates.

## Decisions (owner, 2026-11-13)

1. **Stop ablating the engine against files.** `record-files` is not run. The comparison is `control` against `record-engine`.
2. **Report the verifier's graded score per attempt**, not only pass or fail, and plot its trend by attempt index for the two arms: one panel per task and an average panel.
3. **Four hand-picked tasks, budget N = 20, three chains per root and arm** ("pass@20 with 3 trials").
4. **Restore/resume from a checkpoint is not pursued now.**
5. **A targeted prompt arm is wanted next**, after this baseline trend: tell the record arm to find and test a case the earlier attempts' checks did not cover (from `2026-11-05_repair-anatomy.md`: all 37 mvcc passes wrote such a test). It needs an ADR (constraint 5), an equivalent instruction for control, and roots held out from the ones the wording is tuned on. Not part of this run.
6. **Exposing the graded score to the agent is not decided.** It changes the binary signal Exp 2 fixes and needs an ADR; the owner is asking Prof. Kaffes.

## What the agent sees (confirmed 2026-11-13)

Every arm is told only that earlier attempts FAILED.
The record's `trials.reward` is Harbor's `rewards["reward"]`, which is 0 or 1 on all 23 tasks of the sample (read from each task's `tests/` in TB 4.0.0: 19 tasks `echo 0|1`, the three freecad tasks binarize at a threshold, vba-userform-port writes 1.0 only if all checks pass).
The graded numbers below are in verifier files the store does not ingest.

## Sample

Criteria, fixed before the run: a score with spread over fresh attempts, a record long enough that querying matters, work that is costly to redo, small checkpoints, no safety refusals.
Fresh-attempt figures are from `tb40-passk-fresh-v1` (below), 20 attempts per task.

| Task | Graded score, and where it is | Fresh spread (20 attempts) | Fresh passes | Median calls, minutes |
|---|---|---|---|---|
| production-planning | hidden tests passed of 20, pytest summary line of `verifier/test-stdout.txt` | 16 to 20; ten at 19 | 2 | 23.5, 8.0 |
| vf2-speedup-networkx | `Geomean speedup` line of `verifier/test-stdout.txt`, gate 5,000x | 2x to 26,848x, median 2,186x | 1 | 17, 9.3 |
| gsea-proteomics | hidden tests passed of 16 | 9 to 15; six at 15 | 0 | 13, 4.1 |
| mvcc-lsm-compaction | hidden tests passed of 15 | 11 (18 attempts) or 15 (2) | 2 | 4, 0.8 |

Not in the run: freecad-platform-drawing (continuous score, but 7 calls and 2.3 minutes: cheap to redo; the owner chose four tasks), bun-sourcemap-leak (all 20 fresh attempts scored exactly 27 of 36: no spread).
Candidates that need first attempts before they can be chained: cargo-flight-dispatch, freecad-impeller, freecad-spring-clip, pretrain-shard-corruption (its checkpoints were large on the AWS run).
Excluded: layout-config-recreation, layout-config-recreation2, cad-model, sglang-qwen-burst (1.5 to 35 GB of images per attempt), interleaved-vigenere (safety refusals), tasks with five or fewer tests.

### Roots

Rule: the failed fresh attempt whose graded score is closest to the task's median over its 20 fresh attempts; ties go to the earliest `started_at`.
One root per task, in `corpus/jobs/tb40-passk-fresh-v1/`:

| Task | Root | Score | Calls | Checkpoints | Attempts tied under the rule |
|---|---|---|---|---|---|
| production-planning | `production-planning__ukYKBeW` | 19 of 20 | 24 | 19 | 10 |
| vf2-speedup-networkx | `vf2-speedup-networkx__7bK2c3L` | 2,213x | 23 | 13 | 1 |
| gsea-proteomics | `gsea-proteomics__mwYeuLZ` | 11 of 16 | 13 | 7 | 9 |
| mvcc-lsm-compaction | `mvcc-lsm-compaction__tBuSNjq` | 11 of 15 | 4 | 2 | 18 |

## Design

- **Arms.** `control`, `record-engine`; prompts `trajlab.exp2.prompt.prefix` unchanged (`v4` text for record-engine, ADR-0024 amendment item 7).
- **Budget.** N = 20 retries per chain; a chain stops at its first pass, as `trajlab.exp2.loop.Exp2Runner` does. No code change to the loop.
- **Chains.** Three per root and arm: 4 tasks x 2 arms x 3 chains = 24 chains, at most 480 attempts.
  `scripts/2026-11-10_exp2_passk_chains.py` runs one chain per root and arm in a lane; on 2026-11-10 a second chain was a second lane with the same roots, so three chains are three lanes per task (12 lanes). Not verified for three lanes.
- **The record grows.** After a failed record-engine attempt, `trajlab.exp2.chain.ingest` adds it to the chain's store; the next attempt queries attempts 0 to t-1. A chain's store holds only its own attempts.
- **Capture.** One watcher on the chains' jobs dir, `--every 1 --gate change`, for both arms.
- **Order.** Two attempts at a time, lowest attempt index first, as on 2026-11-10.

## Outcomes

1. Graded score per attempt by attempt index, per task and arm, each chain drawn and the arm's median over its chains.
2. Best score so far per chain. Solved-by-k is this curve crossing the pass line; after a chain's pass its score is carried forward, so chains that stop do not drop out of the average.
3. Solved-by-k for k = 1 to 20 per task and arm.
4. The average panel: scores put on one scale first (tests passed over tests; vf2 as speedup over the 5,000x gate, capped at 1). The per-task panels are the main figure.
5. Record use by depth, as designed on 2026-11-10 and never reported: queries per attempt, `row_capped`, `cut_reads`, `attempt_filters`.

The score extraction and the trend figure are not written: `scripts/2026-11-10_exp2_passk_figures.py` draws the pooled distribution only. A new dated script, or an extension of that one (it would then have run twice: see the `dated-script` skill).

## What earlier numbers predict

- Record-engine's first retry lands near the root's score; control's lands wherever a fresh attempt lands (the fresh spread above).
- The 2026-11-10 interim figure (116 attempts, one or two chains per root; read off the figure, no table was written): on mvcc, control passed 6 times in 19 attempts and record-engine 4 times in 48, with record-engine attempts at 13 and 14 of 15, which fresh attempts never score; on vf2, control passed on attempt 9 (median about 1,600x) and record-engine on attempt 16 (median about 3,700x, clustered under the gate).
- So: a step up at attempt 1 for record-engine, then a flat line, with control reaching the pass line sooner on tasks where a fresh attempt sometimes passes.

## State of the Mac (2026-11-13)

- **Work from `~/trajlab`, not `~/Documents/ColumbiaResearch/TrajLab`.** A trial launched from under `~/Documents` hung at container start and wedged Docker; the same smoke test passed from `~/trajlab`. The cause is not confirmed (suspected: macOS's Documents permission for Colima's background service). The `~/Documents` checkout holds no trial data.
- **Docker is Colima** (8 CPUs, 12 GB, 160 GB disk, aarch64 with Rosetta), started by hand with `colima start`. The brew service is in an error state ("storage device attachment is invalid"); a reboot does not bring Docker back.
- `uv` installed with Homebrew; `.env` in `~/trajlab` has the subscription token, `CLAUDE_FORCE_OAUTH=1`, and the output cap. Launch with `env -u ANTHROPIC_API_KEY`.
- **`tb40-passk-fresh-v1`** (run 2026-11-13, 03:28 to 10:02 EST): 6 tasks x 20 fresh first attempts, two at a time, watcher `--every 1 --gate change`, no write recorder, launched with `--allow-dirty`. 120 results, 5 passed (mvcc 2, production-planning 2, vf2 1), no exception, no hook timeout, 990 checkpoint records, $45.71 as the result files report it.
  These are plain first attempts with no retry prefix: not Exp 2's control arm.
- **Uncommitted in `~/trajlab`:** `configs/harbor/tb40-passk-fresh-v1.json`, `corpus/manifests/tb40-passk-fresh-v1.json` (records a dirty tree), this file.
- **Not done for `tb40-passk-fresh-v1`:** postprocess, validate, run note, paper notes (`post-run` skill).
- **Disk:** 62 GB free in the Colima VM; the run's images use about 87 GB; none deleted.

## Setup steps for the next session

Commands as the earlier notes give them; none run for this design.

1. Postprocess the four roots (`trajlab postprocess`), then build a schema 6 store per task from the root's trial dir: `uv run trajlab store build corpus/jobs/tb40-passk-fresh-v1/<root> --store corpus/stores/<task>-local-v6`.
2. `plan` with `--budget 20 --arms control record-engine` and three lanes per task, each `--lane <name> <store> corpus/jobs/tb40-passk-fresh-v1 <root>`; then `check`, which must report no prompt that names a check or quotes verifier text.
3. Start the watcher under `caffeinate -dimsu` on the chains' jobs dir, then `run` with `env -u ANTHROPIC_API_KEY`.
4. Watch with the `job-loop` skill. Last night's pace was about 17 attempts an hour, so 480 attempts is up to about 28 hours.

## Open decisions for the owner

1. **Disk.** At last night's 0.5 GB an attempt, 480 attempts need up to about 240 GB against 62 GB free.
   The run fits only if images are deleted: the fresh run's trials that are not roots, and retry images once ingested or never to be ingested (control retries, passes).
   The owner was asked on 2026-11-13 and has not confirmed; deletion cannot be undone, and `run-watch` forbids deleting images without the owner's word.
2. **Commit.** The config and manifest of `tb40-passk-fresh-v1` and this note, on a branch (`sl/<topic>`), with one reviewer.
3. **Freecad as a fifth task**, if time allows.
4. **Whether the mvcc gap matters:** fresh first attempts passed 2 of 20 here; the control arm of 2026-11-10 passed 6 of 19 with the retry prefix and from particular roots. The control chains of this run will show which it is.
