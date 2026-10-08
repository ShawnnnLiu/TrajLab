# TB 4.0 repair experiment, round 1: the `traj-text` arm on 21 Sonnet 5.5 failures (2026-10-08)

Results of the fifth arm of round 1, `traj-text`, as built in ADR-0012's amendment "`traj-text` as built (round 1 note)" (2026-10-07), with round 1's one-line note.
The launcher started on 2026-10-08 at 00:41 UTC from `sl/traj-text` at `3671a85` with the amendment's launch command, and finished at 04:51:52 UTC (`_repair-inputs/tb40-repair-v2.traj-text.status.json`: no running, pending, or resume-due jobs).
The rows for `fresh`, `state`, `state-traj`, and `traj` below are round 1's, from `docs/research/2026-10-03_tb40-repair-v2-round1.md`, recomputed from the same files.

## Data

- **Per-trial rows:** `corpus/jobs/tb40-sonnet-v2/repair-report.traj-text.json` (not in git; `corpus/jobs` is `/srv/trajlab/jobs`), 132 rows: 69 first attempts (`arm: "original"`) and 63 `traj-text` repairs.
  Regenerate with `uv run python scripts/2026-10-02_repair_report.py corpus/jobs/tb40-sonnet-v2 --prefix tb40-repair-v2 --arms traj-text`.
  Fields are round 1's (`docs/research/2026-10-03_tb40-repair-v2-round1.md`, "Data").
- **Per-check rows:** `corpus/jobs/tb40-sonnet-v2/repair-checks.traj-text.json`, written by the same command: 1,896 rows, 963 of first attempts and 933 of repairs. Every `traj-text` repair has verifier output, and its checks agree with its reward: reward 1 exactly when no check failed.
- **Per-trial early-stop table:** `corpus/jobs/tb40-sonnet-v2/early-stop.traj-text.csv`, 132 rows with the columns of round 1's `early-stop.csv`: outcome, effort, file-changing calls, the first command, failing checks against the source's, and the early-stop flags. Its README is `docs/research/2026-10-08_tb40-repair-v2-traj-text-early-stop.md`.
- **Round 1's files:** `--arms` names the output files after the arms when they are not the default four, so `repair-report.json` and `repair-checks.json` keep round 1's rows. Run with the default arms on 2026-10-08, the script rewrote both files byte for byte as they were (copies from before the change are in `/srv/trajlab/jobs/tb40-sonnet-v2.repair-report-backup-2026-10-08/`).
- **Which failures were repaired:** round 1's recorded draw, `_repair-inputs/tb40-repair-v2.selection.json`, read and not rewritten. The status file lists 21 queued failures and 35 not repaired: 34 not drawn and 1 with no checkpoint (`freecad-platform-drawing__DiSv3hE`). Its `queued_failures`, `not_repaired`, and `selected` are equal to those in round 1's `tb40-repair-v2.status.json`.
- **Inputs:** `_repair-inputs/<job>/transcript.txt` and `repair-source.json`. The transcript sizes, tool-output counts, and cut counts in the 21 `repair-source.json` files equal the amendment's table, and `source_compacted` is false in all 21.
- **Stratum:** as in round 1, a task's first-attempt pass rate out of 3.
- **Cost rule:** as in round 1, tokens of the trial's own assistant messages, at Sonnet 5.5 list prices (`cost_usd_own`). `traj-text` resumes no session, so every assistant message in its session is its own. Claude Code's reported cost over the 63 repairs is $38.05; the priced cost is $37.67. No `traj-text` session file was unreadable (`sessions_unreadable` is 0 in every row).

## The run

- 21 jobs of 3 trials, at most 6 trials at once. The launch-to-finish wall time was 4 h 10 min; the 63 trials took 17.3 trial-hours (`trial_s`).
- All 63 `result.json` files record Claude Code 2.1.278 and `claude-sonnet-5-5`.
- **Prompt delivery.** In all 63 trials, the first user message of `agent/trajectory.json` equals round 1's note, a blank line, the job's `transcript.txt`, a blank line, and the source trial's instruction (its first user step), compared as decoded strings. The carriage returns are part of the comparison; the delivered counts are freecad-impeller 74, freecad-platform-drawing 59, freecad-spring-clip 50, roy-polymorph-cn 113, vba-userform-port 98, vllm-deepseek-streaming 25.
- **Errors:** none. No trial has an exception: no `AgentSafetyRefusalError`, `AgentTimeoutError`, `NonZeroAgentExitCodeError`, output- or context-length error, or infra error.
- **Launcher:** no resume, no hold, and no pause. Its log has only queued, launched, exited 0, pruned, and committed lines, then "all repairs finished". It committed the 21 manifests in 20 commits (cad-model and foodstuff-beta-activity in one). Each job's Harbor log has no line about deleting a trial.
- **Verifier time** of the two tasks whose verifiers install packages: cargo-flight-dispatch 21, 22, and 79 s; vba-userform-port 55, 65, and 70 s.
- **Disk.** Free space on `/srv/trajlab/jobs` was 156 GB at launch and 63 GB at 03:00 UTC. layout-config-recreation's three trials ran 5,504 to 6,837 s, and the launcher pruned 63, 89, and 63 of their checkpoint images when the job ended at about 03:05 UTC; free space was then 125 GB. The launcher holds launches below 50 GB; it did not hold.
- **Checkpoint images.** After pruning, each of the 63 trials has one `trajlab-checkpoint` image. No checkpoint image created on 2026-10-08 belongs to a trial without a trial dir.

### Checkpoint capture

| | Hook requests | Checkpoint records | `.timeout` files | Hook error logs |
|---|---:|---:|---:|---:|
| `traj-text` | 842 | 551 | 44 | 0 |

- The 44 timeouts: sglang-qwen-burst 40, cad-model 3, gsea-proteomics 1.
- Round 1's trial dirs, counted on 2026-10-08: `fresh` 4 timeouts in 1,367 requests, `state` 4 in 889, `state-traj` 26 in 877, `traj` 76 in 1,892.
- **sglang-qwen-burst `Wh9pjSj` and `r9ixcqr`.**
  - Each ran `pip install` with `torch` early in the trial (`Wh9pjSj` from PyTorch's CPU index). At 02:59 UTC their containers' writable layers were 8.83 and 8.81 GB.
  - From then on, every `docker commit` of either container failed with "timed out after 230 seconds" in the watcher's log, the Stop checkpoint included. A failed commit writes no record, and the hook writes a `.timeout`.
  - `Wh9pjSj`: 28 tool calls, 29 hook requests, 22 timeouts, 7 records, agent time 7,193 s. `r9ixcqr`: 21 tool calls, 22 hook requests, 18 timeouts, 3 records, agent time 6,330 s.
  - Between a timed-out call's `.req` and its `.timeout`, 240 to 471 s passed.
  - Their kept images are seq 7 and seq 3, the last commits that succeeded, made before the torch install. Neither image holds the state the agent left.
  - Round 1's sglang-qwen-burst repairs `state-traj` `mdjJ2Mw`, `traj` `BiUAs56`, and `traj` `xVYHQyV` have 18, 16, and 34 timeouts, with agent times of 5,438, 5,408, and 10,552 s.

## Results

First attempts: 13 of 69 passed. The 21 repaired failures are round 1's: 17 tasks at 0/3, 1 at 1/3, 3 at 2/3.

| Arm | Repairs passed | 0/3 tasks | 1-2/3 tasks | Own cost | Mean / median per repair | Cost per passed repair | Output tok | Cache-write tok | Cache-read tok | API calls | Tool calls | Median agent s | Longest agent min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `fresh` | 7/63 (11%) | 4/51 | 3/12 | $44.08 | $0.70 / $0.20 | $6.30 | 1.34 M | 2.78 M | 97.5 M | 1,382 | 1,474 | 292 | 98 |
| `state` | 5/63 (8%) | 2/51 | 3/12 | $22.54 | $0.36 / $0.18 | $4.51 | 0.63 M | 1.63 M | 48.5 M | 895 | 922 | 167 | 240 |
| `state-traj` | 15/63 (24%) | 5/51 | 10/12 | $39.53 | $0.63 / $0.24 | $2.64 | 0.80 M | 3.93 M | 78.9 M | 889 | 896 | 138 | 203 |
| `traj` | 17/63 (27%) | 8/51 | 9/12 | $35.10 | $0.56 / $0.16 | $2.06 | 0.95 M | 1.95 M | 88.7 M | 964 | 996 | 254 | 176 |
| `traj-text` | 3/63 (5%) | 1/51 | 2/12 | $37.67 | $0.60 / $0.23 | $12.56 | 1.04 M | 3.26 M | 70.8 M | 847 | 838 | 288 | 120 |

Errored repairs: none in `traj-text`. Round 1's are in its note: `fresh` 2 `EnvironmentStartTimeoutError` and 3 `AgentSafetyRefusalError`, `state` 3 `AgentSafetyRefusalError` and 1 `AgentTimeoutError`. Round 1's 6 refusals were all on interleaved-vigenere; its 3 `traj-text` repairs ran 622 to 942 s and ended without an exception.

Per failure, the 3 repairs of each arm (P passed, . failed, E infra error, no reward), in trial-name order:

| Failure | First attempts | `fresh` | `state` | `state-traj` | `traj` | `traj-text` |
|---|---|---|---|---|---|---|
| cad-model__owX55WW | 0/3 | `...` | `...` | `..P` | `PPP` | `...` |
| freecad-platform-drawing__MoExzvS | 0/3 | `...` | `.P.` | `...` | `...` | `...` |
| mvcc-lsm-compaction__EkrWD8Q | 0/3 | `...` | `..P` | `P..` | `P..` | `...` |
| pretrain-shard-corruption__QaSkaGD | 0/3 | `PPP` | `...` | `P..` | `P..` | `P..` |
| sglang-qwen-burst__Gzz6bVA | 0/3 | `...` | `...` | `PP.` | `PP.` | `...` |
| vf2-speedup-networkx__MC8ceMN | 0/3 | `.P.` | `...` | `...` | `P..` | `...` |
| interleaved-vigenere__ZADTzxt | 2/3 | `...` | `...` | `PP.` | `PPP` | `...` |
| layout-config-recreation2__7aXyTZX | 1/3 | `P..` | `P.P` | `PPP` | `PPP` | `.P.` |
| sound-change-cascade__f6vnRbi | 2/3 | `.EE` | `..P` | `PPP` | `PPP` | `..P` |
| vba-userform-port__zyezmFw | 2/3 | `P.P` | `...` | `P.P` | `...` | `...` |

No arm, `traj-text` included, passed any repair of the other 11 failures (all 0/3 tasks): bun-sourcemap-leak, cargo-flight-dispatch, foodstuff-beta-activity, freecad-impeller, freecad-spring-clip, gsea-proteomics, layout-config-recreation, production-planning, protein-autointerp-disulfide, roy-polymorph-cn, vllm-deepseek-streaming.

The 3 passed `traj-text` repairs: layout-config-recreation2 `5uXNdbY` (589 s, 12 tool calls, $0.36), pretrain-shard-corruption `A7Q77zn` (2,945 s, 66 tool calls, $3.39), sound-change-cascade `ysPn6oD` (482 s, 23 tool calls, $1.02).

### `traj-text` per failure

Failing checks of the source and of each repair (pass/fail checks only; for the freecad tasks the one check is the overall score), with each repair's agent time and tool calls in the same order, and the cost of the 3 repairs.

| Failure | Source failing / checks | Failing per repair | Agent s | Tool calls | Own cost |
|---|---|---|---|---|---|
| bun-sourcemap-leak__bWBkDEi | 9/36 | 9, 9, 9 | 175, 177, 177 | 2, 3, 2 | $0.36 |
| cad-model__owX55WW | 6/8 | 6, 6, 6 | 612, 633, 609 | 3, 3, 4 | $0.37 |
| cargo-flight-dispatch__dCQcazx | 8/27 | 8, 5, 8 | 218, 219, 125 | 6, 6, 3 | $0.98 |
| foodstuff-beta-activity__9sEaCB7 | 2/13 | 2, 1, 1 | 54, 80, 54 | 1, 2, 1 | $0.24 |
| freecad-impeller__5uRf9iP | 1/1 | 1, 1, 1 | 226, 210, 214 | 4, 3, 4 | $0.48 |
| freecad-platform-drawing__MoExzvS | 1/1 | 1, 1, 1 | 288, 222, 228 | 11, 7, 6 | $0.48 |
| freecad-spring-clip__iekZ2gG | 1/1 | 1, 1, 1 | 251, 324, 460 | 3, 5, 4 | $0.69 |
| gsea-proteomics__pGdXBZN | 5/16 | 6, 6, 6 | 438, 524, 527 | 6, 5, 4 | $0.52 |
| interleaved-vigenere__ZADTzxt | 2/6 | 3, 1, 1 | 942, 622, 656 | 14, 11, 10 | $2.28 |
| layout-config-recreation2__7aXyTZX | 1/9 | 1, 0, 1 | 89, 589, 82 | 3, 12, 3 | $0.76 |
| layout-config-recreation__U6tosEx | 2/12 | 2, 2, 2 | 6,837, 5,504, 6,770 | 96, 110, 103 | $9.70 |
| mvcc-lsm-compaction__EkrWD8Q | 4/15 | 4, 4, 4 | 79, 85, 90 | 2, 3, 3 | $0.35 |
| pretrain-shard-corruption__QaSkaGD | 1/12 | 0, 1, 8 | 2,945, 2,002, 1,306 | 66, 25, 24 | $6.96 |
| production-planning__TDgRk2e | 1/20 | 1, 1, 1 | 187, 235, 313 | 7, 7, 13 | $1.79 |
| protein-autointerp-disulfide__FTyekmW | 1/2 | 1, 1, 1 | 53, 65, 51 | 1, 2, 1 | $0.22 |
| roy-polymorph-cn__XazWJRu | 1/3 | 1, 1, 3 | 63, 66, 59 | 2, 2, 1 | $0.26 |
| sglang-qwen-burst__Gzz6bVA | 5/13 | 7, 7, 5 | 7,193, 89, 6,330 | 28, 6, 21 | $1.39 |
| sound-change-cascade__f6vnRbi | 2/7 | 2, 2, 0 | 109, 89, 482 | 4, 2, 23 | $1.64 |
| vba-userform-port__zyezmFw | 6/32 | 6, 6, 6 | 542, 396, 412 | 14, 12, 14 | $3.45 |
| vf2-speedup-networkx__MC8ceMN | 1/60 | 1, 1, 1 | 965, 615, 538 | 23, 12, 8 | $2.46 |
| vllm-deepseek-streaming__tHKh5W5 | 4/5 | 4, 4, 4 | 359, 364, 384 | 16, 19, 17 | $2.28 |

Pooled over all failures, from the check files: failed repairs whose set of failing checks equals their source's set.

| Arm | Same set | Subset of source's | Superset | Different | Failed repairs |
|---|---|---|---|---|---|
| `fresh` | 43 | 4 | 4 | 3 | 54 |
| `state` | 40 | 4 | 9 | 5 | 58 |
| `state-traj` | 38 | 2 | 1 | 7 | 48 |
| `traj` | 39 | 1 | 1 | 5 | 46 |
| `traj-text` | 47 | 4 | 5 | 4 | 60 |

## Gaps in the data

- `tests` in the report is the pytest summary. For vba-userform-port it reads `4 passed` in all 3 `traj-text` rows with reward 0; the grade is the trace pass rate, which is in the check file.
- As in round 1, not recorded per trial: the source's failure kind, whether the repair ran the tests itself, whether it ended its turn within 3 minutes, and the time to its first edit. The ADR's primary test and McNemar on pass@3 are not computed here.
- For sglang-qwen-burst `Wh9pjSj` and `r9ixcqr`, no checkpoint image holds the end state (see "Checkpoint capture"). Their verifier results are unaffected: the verifier runs in the trial's own container.
- The amendment's differences between `traj` and `traj-text` (hidden reasoning, truncation, conversation form, images, Claude Code's reminders) are not separated here: results are for the bundle.
- The 6-trial cap does not see ground-truth admission. The ledger (`~/.cache/trajlab/gt-admission/ledger.json`) was `[]` at launch and at every check during the run (a check each minute while a monitor was armed), and no `trajlab gt` process ran at launch.
