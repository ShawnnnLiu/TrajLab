# Exp 2 full run `tb40-exp2`: interim record, 2026-10-30

The run stopped at 102 of its 168 chains, before the preregistered comparison point (N = 3), when the owner shut the AWS capture host down for cost.
This file records what it produced and what is left, for analysis now and for resuming later.
The design and settings are `preregistration.md` (committed at bdea2cb); the loop and every incident during the run are ADR-0022 (its Validation section); the data export is described at the end.

## What ran

- 56 root failures (every usable failed first attempt of `tb40-sonnet-v4`, 21 tasks), 3 arms (`control`, `record-files`, `record-engine`), up to N = 3 retries per chain, a fresh environment and Claude Code session each time; 168 chains.
- From 2026-10-28 07:46 UTC to 2026-10-29 10:48 UTC, six attempts at a time (three at a time from 21:41 to 22:22 on 2026-10-28), launches ordered by attempt index and then by failure with the arms interleaved.
- Record-arm retry images were pruned once ingested from 21:41 on 2026-10-28; control retries' images were deleted once, on the owner's go-ahead.
- Every incident (a launch race, a registry outage, two disk stops, the restarts) and the fix for each is in ADR-0022's Validation section.

## State at the stop

- All 168 first retries and all second retries are counted; 102 chains are finished (repaired, or three retries counted).
- 66 chains need their third retry, and none needs an ingest first (the list is below).
- 407 attempt records: 363 failed and 32 passed (counted), 5 `infra` and 7 `lost` (harness failures, rerun, not counted); five more cancelled launches are not yet recorded by the runner and will be recorded as `infra` on restart.
- 192 ingests, none failed; 533 queries.

## Results so far

Not the preregistered test, which is at N = 3 over all 56 failures.

Repaired by N, out of the failures for which every arm's outcome at that N is known:

| | Failures | control | record-files | record-engine |
|---|---|---|---|---|
| N = 1 | 56 | 3 (5%) | 7 (13%) | 4 (7%) |
| N = 2 | 56 | 8 (14%) | 12 (21%) | 9 (16%) |
| N = 3 | 31 of 56 | 7 | 12 | 8 |

The N = 3 row covers only the chains finished in every arm, which over-represent the tasks repaired early and the short tasks; it is not comparable with the other rows.

Paired by failure at N = 2 (all 56), counting the failures one arm repaired and the other did not (exact McNemar p, descriptive only):

| | Only the first | Only the second | p |
|---|---|---|---|
| record-engine vs record-files | 2 | 5 | 0.45 |
| record-engine vs control | 4 | 3 | 1.00 |
| record-files vs control | 5 | 1 | 0.22 |

By task (repaired by N = 2; chains finished):

| Task | Failures | control / record-files / record-engine | Chains finished |
|---|---|---|---|
| bun-sourcemap-leak | 3 | 0 / 0 / 0 | 9/9 |
| cad-model | 3 | 2 / 2 / 1 | 9/9 |
| cargo-flight-dispatch | 3 | 0 / 0 / 0 | 9/9 |
| foodstuff-beta-activity | 3 | 0 / 0 / 0 | 9/9 |
| freecad-impeller | 3 | 0 / 0 / 0 | 9/9 |
| freecad-platform-drawing | 3 | 0 / 1 / 1 | 9/9 |
| freecad-spring-clip | 3 | 0 / 0 / 0 | 9/9 |
| gsea-proteomics | 3 | 0 / 0 / 0 | 9/9 |
| interleaved-vigenere | 2 | 0 / 1 / 2 | 6/6 |
| layout-config-recreation | 3 | 0 / 0 / 0 | 4/9 |
| layout-config-recreation2 | 2 | 2 / 2 / 2 | 6/6 |
| mvcc-lsm-compaction | 2 | 2 / 2 / 0 | 5/6 |
| pretrain-shard-corruption | 3 | 0 / 0 / 1 | 1/9 |
| production-planning | 3 | 0 / 1 / 0 | 1/9 |
| protein-autointerp-disulfide | 3 | 0 / 0 / 0 | 0/9 |
| roy-polymorph-cn | 3 | 0 / 0 / 0 | 0/9 |
| sglang-qwen-burst | 3 | 0 / 0 / 0 | 0/9 |
| sound-change-cascade | 1 | 1 / 1 / 1 | 3/3 |
| vba-userform-port | 1 | 1 / 1 / 1 | 3/3 |
| vf2-speedup-networkx | 3 | 0 / 1 / 0 | 1/9 |
| vllm-deepseek-streaming | 3 | 0 / 0 / 0 | 0/9 |

By N = 2, repairs come from 10 of the 21 tasks; the other 11 have none in any arm.

Effort per counted attempt (medians; cost is Claude Code's API-equivalent figure, the run used the subscription):

| | Attempts | Agent minutes | Tool calls | Output tokens | Total cost |
|---|---|---|---|---|---|
| control | 133 | 5.2 | 9 | 9,018 | $103 |
| record-files | 129 | 5.1 | 9 | 9,447 | $102 |
| record-engine | 133 | 4.0 | 7 | 7,652 | $92 |

Query use, over counted record-engine attempts (every one queried at least once; 5 queries were refused or failed):

| Retry | Attempts | Queries | Median per attempt | Max | Name `attempt_idx` or `trials` | Touch `deltas` | Use a derived view |
|---|---|---|---|---|---|---|---|
| 1 | 56 | 265 | 3 | 55 | 7% | 42% | 5% |
| 2 | 52 | 182 | 3 | 13 | 36% | 14% | 10% |
| 3 | 25 | 84 | 3 | 9 | 58% | 8% | 13% |

With one earlier attempt the agents go to its file changes; with more, they first ask which attempts there are and how each scored.
Later retries are the chains that failed earlier ones, so the rows are not the same failures.

## What is left

66 chains, each needing attempt 3:

- layout-config-recreation (5): RN8QV4W/control, RV5qQjA/control, kb38wvM/control, kb38wvM/record-files, kb38wvM/record-engine (all five cancelled at 13:07 UTC on 2026-10-29 and to be rerun)
- mvcc-lsm-compaction (1): ccQd9vh/record-engine
- pretrain-shard-corruption (8): 6egdDwc × 3, Cgizn8v × 3, R4QX4a7/control, R4QX4a7/record-files
- production-planning (8): mXg4cWZ × 3, un4NZWE/control, un4NZWE/record-engine, xU3cHDY × 3
- protein-autointerp-disulfide (9): 3KtgkCt, eaT4bXV, hetXe9M × 3 arms
- roy-polymorph-cn (9): KVWqqpZ, hrYnChi, jizFeHm × 3 arms
- sglang-qwen-burst (9): 4z9SyjA, Y6ovEBk, exuuLAH × 3 arms
- vf2-speedup-networkx (8): UVeaJc9 × 3, bDWTHvP/control, bDWTHvP/record-engine, us8GDaB × 3
- vllm-deepseek-streaming (9): quctgYr, rQhEWt9, t8HdFNj × 3 arms

The disk is the constraint: an attempt's checkpoint images are kept until it is ingested, and a third retry is never ingested, so the remaining sglang (about 35 GB each), layout and pretrain retries need room.
Deleting the images of attempts never to be ingested (control retries, record-arm passes, record-arm third retries) frees it; Exp 2 never reads them.

## Data

- Export (on the host, outside git): `/srv/trajlab/exports/tb40-exp2-2026-10-30/`, 467 MB, with its own README: one row per attempt with its metadata (`attempts.csv`), every chain's state (`chains.csv`), the unfinished chains (`pending.csv`), every query (`queries.jsonl`), every ingest (`ingests.jsonl`), the five launches not yet recorded, a summary, and a tarball of each attempt's small files (results, trajectories, query logs, diagnoses, checkpoint records, inputs).
- Only on the host's root volume: the chain stores and record-files bundles (`corpus/exp2/tb40-exp2/`, 73 GB), the full trial dirs (`/srv/trajlab/jobs/tb40-exp2-*`), the remaining checkpoint images, and the ops scripts (`/srv/trajlab/ops/exp2/`).
- Resuming: the export's README; the runner resumes from the chain logs.
