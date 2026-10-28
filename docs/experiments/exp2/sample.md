# Exp 2 sample: the usable failures of `tb40-sonnet-v4`

Written 2026-10-28.
This file is build-order step 2's deliverable in the technical spec (§7): the count of usable failures and each trial's build time.
The preregistration (technical spec §5.3) fixes the sample by citing this file.

## The corpus

- `tb40-sonnet-v4` (`corpus/manifests/tb40-sonnet-v4.json`) has 23 tasks of `terminal-bench/terminal-bench@4.0.0`, 3 attempts each.
  It ran under Harbor 0.23.0 with Claude Code 2.1.278 and `anthropic/claude-sonnet-5-5`, as `configs/harbor/tb40-sonnet-v4.json` specifies.
- It was captured on 2026-10-27 with a checkpoint at every tool call and the write record of ADR-0021.
  Two trials, `layout-config-recreation__RV5qQjA` and `pretrain-shard-corruption__6egdDwc`, are reruns by `harbor jobs resume` of two attempts that a full disk interrupted before they had a reward.
  They ran with the same config, capture code, and recorder as the other 67 (ADR-0021, amendment of 2026-10-28), and are attempt 0 like the rest.
- The store is `corpus/stores/tb40-sonnet-v4` (schema 4, derived `3+c45ab40d3b46`), built at `8f6e9e9` (log `/srv/trajlab/logs/store-tb40-sonnet-v4-2026-10-27c.log` on the capture host).
- Why this corpus and not `tb40-sonnet-v3`:
  - only v4 has the write record, which settles the attribution of late deltas (ADR-0021);
  - v3's checkpoint images were deleted on 2026-10-28, so v3's store can no longer be rebuilt;
  - ground truth (ADR-0013) replays from checkpoint images.

  v4's images are on the capture host, and must stay there until ground truth has run on these failures.

## Usable failures

The rule (design spec §5): a failure is usable only if its checkpoint chain is complete through the last tool call.
The check reads the `chains` table in the store's `heldout.duckdb`.

- 69 trials: 13 passed (reward 1) and 56 failed (reward 0).
- All 69 chains are complete:
  - every hooked call was answered;
  - no call timed out, went unanswered, was interrupted, or was rejected;
  - no checkpoint image is missing;
  - the final state is recorded;
  - no chain lists a reason against it.
- **Usable failures: 56. Excluded: none.**
- The 56 come from 21 of the 23 tasks: 16 tasks failed all 3 attempts, 3 tasks failed 2, and 2 tasks failed 1.
  `risk-scorer-replay` and `vpp-loss-divergence` passed all 3 attempts and contribute nothing.
- Compute at N = 3 (ADR-0016, item 2): at most 56 failures × 3 arms × 3 retries = 504 attempts, fewer after early exits on success.
- The failures are short at the median and long in the tail: 3 to 229 tool calls (median 9.5), 6 checkpoints at the median, and 192 delta rows at the median.

### Open: one failure per task, or all of them?

Design spec §5 says both "All usable failures, one chain per failure per arm" and "Task is a random effect in the model; no task contributes twice"; ADR-0016 costs the run at 56.
Read literally, "no task contributes twice" would cut the sample to 21: one failure per task, chosen by a rule fixed before preregistration.
That is at most 189 attempts, with no task random effect needed.
Keeping all 56 lets a task contribute up to 3 failures, and the task random effect carries that.
This is not settled here; the owner decides before the preregistration.

### Per task

| Task | Failed | Failed trials |
|---|---|---|
| `bun-sourcemap-leak` | 3 of 3 | `3f2BwTC`, `X6KbUKo`, `b5DA76u` |
| `cad-model` | 3 of 3 | `Gun964v`, `MEwahRs`, `jhjUsFh` |
| `cargo-flight-dispatch` | 3 of 3 | `DAgHbSz`, `FVgfyVQ`, `J8Ce4cR` |
| `foodstuff-beta-activity` | 3 of 3 | `2iHyDYM`, `9XLzFHA`, `XZsfWUj` |
| `freecad-impeller` | 3 of 3 | `DddVQ9g`, `GUqwwwf`, `o7pg8wY` |
| `freecad-platform-drawing` | 3 of 3 | `6tXhKU6`, `ibz96hK`, `yd3EsQP` |
| `freecad-spring-clip` | 3 of 3 | `EmXRHs3`, `GHZZCFs`, `qnC44Cc` |
| `gsea-proteomics` | 3 of 3 | `jB5QsQj`, `mqM8Mpm`, `sqY9QmQ` |
| `layout-config-recreation` | 3 of 3 | `RN8QV4W`, `RV5qQjA`, `kb38wvM` |
| `pretrain-shard-corruption` | 3 of 3 | `6egdDwc`, `Cgizn8v`, `R4QX4a7` |
| `production-planning` | 3 of 3 | `mXg4cWZ`, `un4NZWE`, `xU3cHDY` |
| `protein-autointerp-disulfide` | 3 of 3 | `3KtgkCt`, `eaT4bXV`, `hetXe9M` |
| `roy-polymorph-cn` | 3 of 3 | `KVWqqpZ`, `hrYnChi`, `jizFeHm` |
| `sglang-qwen-burst` | 3 of 3 | `4z9SyjA`, `Y6ovEBk`, `exuuLAH` |
| `vf2-speedup-networkx` | 3 of 3 | `UVeaJc9`, `bDWTHvP`, `us8GDaB` |
| `vllm-deepseek-streaming` | 3 of 3 | `quctgYr`, `rQhEWt9`, `t8HdFNj` |
| `interleaved-vigenere` | 2 of 3 | `N4Wfs8g`, `wMtCHHH` |
| `layout-config-recreation2` | 2 of 3 | `qGZer69`, `yq2Vt6U` |
| `mvcc-lsm-compaction` | 2 of 3 | `PFDAk9Y`, `ccQd9vh` |
| `sound-change-cascade` | 1 of 3 | `WGwuZ5g` |
| `vba-userform-port` | 1 of 3 | `PFng8aw` |
| `risk-scorer-replay` | 0 of 3 |  |
| `vpp-loss-divergence` | 0 of 3 |  |

## Build time per trial

`trajlab store build corpus/jobs/tb40-sonnet-v4 --store corpus/stores/tb40-sonnet-v4 --work-dir /srv/trajlab/work`, on the capture host (32 CPUs, ext4 root), one trial at a time, from 2026-10-27 23:20 to 2026-10-28 00:43 UTC.
A trial's time is its `built ... s` line in the log.
It covers the `docker image save` of the trial's checkpoint images, the reconcile of the deltas from their top layers, and the insert of the trial's rows.
The save time is the log's `saved N checkpoint images in ... s` line before it.

- All 69: 4,927 s (82 min) in total; median 39.4 s, from 14.6 s to 535.7 s (`vpp-loss-divergence__tcSiVai`, a pass with 25 checkpoints and 15.3 GB of images). The image save is 53% of the total.
- The 56 failures: median 39.8 s, longest 496.9 s (`layout-config-recreation__RV5qQjA`, 111 checkpoints); 17 of the 56 took over 60 s.
- For the retry loop: the technical spec's target is to ingest a retry in under one minute (§5.2).
  A retry is one trial, and here the build took longer than 60 s for 22 of the 69 trials.
  So ingesting retries this way would miss the target for about a third of them; step 5 should either revisit the target or speed up ingest.

| Trial | Tool calls | Checkpoints | Delta rows | Build s | Of it, image save s |
|---|---|---|---|---|---|
| `bun-sourcemap-leak__3f2BwTC` | 4 | 2 | 6 | 20.7 | 12.7 |
| `bun-sourcemap-leak__X6KbUKo` | 4 | 2 | 6 | 17.1 | 9.2 |
| `bun-sourcemap-leak__b5DA76u` | 4 | 2 | 6 | 17.4 | 9.6 |
| `cad-model__Gun964v` | 6 | 4 | 17,042 | 152.8 | 77.5 |
| `cad-model__MEwahRs` | 7 | 5 | 17,043 | 149.7 | 74.8 |
| `cad-model__jhjUsFh` | 8 | 6 | 17,052 | 189.9 | 98.3 |
| `cargo-flight-dispatch__DAgHbSz` | 5 | 3 | 46 | 17.9 | 10.1 |
| `cargo-flight-dispatch__FVgfyVQ` | 8 | 4 | 46 | 15.4 | 7.6 |
| `cargo-flight-dispatch__J8Ce4cR` | 6 | 4 | 47 | 15.5 | 7.7 |
| `foodstuff-beta-activity__2iHyDYM` | 7 | 5 | 1,715 | 29.8 | 17.0 |
| `foodstuff-beta-activity__9XLzFHA` | 7 | 5 | 711 | 19.3 | 9.6 |
| `foodstuff-beta-activity__XZsfWUj` | 8 | 4 | 710 | 18.9 | 9.5 |
| `freecad-impeller__DddVQ9g` | 5 | 4 | 18 | 59.3 | 35.3 |
| `freecad-impeller__GUqwwwf` | 7 | 6 | 19 | 50.8 | 26.7 |
| `freecad-impeller__o7pg8wY` | 11 | 10 | 33 | 51.7 | 27.0 |
| `freecad-platform-drawing__6tXhKU6` | 5 | 3 | 16 | 58.3 | 34.5 |
| `freecad-platform-drawing__ibz96hK` | 6 | 3 | 16 | 49.8 | 26.1 |
| `freecad-platform-drawing__yd3EsQP` | 3 | 1 | 13 | 49.6 | 26.0 |
| `freecad-spring-clip__EmXRHs3` | 19 | 13 | 36 | 60.6 | 35.5 |
| `freecad-spring-clip__GHZZCFs` | 6 | 5 | 28 | 50.5 | 26.5 |
| `freecad-spring-clip__qnC44Cc` | 28 | 26 | 55 | 54.7 | 28.0 |
| `gsea-proteomics__jB5QsQj` | 14 | 8 | 15,326 | 87.4 | 42.4 |
| `gsea-proteomics__mqM8Mpm` | 13 | 7 | 16,701 | 63.8 | 25.2 |
| `gsea-proteomics__sqY9QmQ` | 13 | 8 | 15,358 | 89.6 | 41.2 |
| `interleaved-vigenere__N4Wfs8g` | 29 | 11 | 31 | 20.7 | 10.7 |
| `interleaved-vigenere__wMtCHHH` | 31 | 10 | 260 | 19.2 | 8.8 |
| `layout-config-recreation2__qGZer69` | 65 | 39 | 1,815 | 94.8 | 49.0 |
| `layout-config-recreation2__yq2Vt6U` | 64 | 38 | 68 | 39.8 | 20.8 |
| `layout-config-recreation__RN8QV4W` | 75 | 43 | 1,667 | 160.5 | 93.4 |
| `layout-config-recreation__RV5qQjA` | 173 | 111 | 18,055 | 496.9 | 203.2 |
| `layout-config-recreation__kb38wvM` | 229 | 146 | 4,787 | 267.0 | 117.4 |
| `mvcc-lsm-compaction__PFDAk9Y` | 5 | 2 | 5 | 17.7 | 10.7 |
| `mvcc-lsm-compaction__ccQd9vh` | 4 | 2 | 5 | 14.7 | 7.8 |
| `pretrain-shard-corruption__6egdDwc` | 62 | 30 | 2,843 | 106.6 | 60.3 |
| `pretrain-shard-corruption__Cgizn8v` | 82 | 24 | 218 | 76.6 | 53.9 |
| `pretrain-shard-corruption__R4QX4a7` | 112 | 54 | 3,109 | 106.8 | 61.6 |
| `production-planning__mXg4cWZ` | 19 | 16 | 47 | 19.7 | 11.2 |
| `production-planning__un4NZWE` | 25 | 18 | 46 | 17.4 | 8.7 |
| `production-planning__xU3cHDY` | 20 | 17 | 43 | 17.4 | 8.8 |
| `protein-autointerp-disulfide__3KtgkCt` | 3 | 2 | 21 | 18.5 | 11.1 |
| `protein-autointerp-disulfide__eaT4bXV` | 5 | 2 | 21 | 16.0 | 8.5 |
| `protein-autointerp-disulfide__hetXe9M` | 4 | 2 | 21 | 15.7 | 8.3 |
| `roy-polymorph-cn__KVWqqpZ` | 4 | 3 | 595 | 25.0 | 15.1 |
| `roy-polymorph-cn__hrYnChi` | 5 | 4 | 596 | 21.5 | 11.4 |
| `roy-polymorph-cn__jizFeHm` | 5 | 4 | 609 | 21.4 | 11.3 |
| `sglang-qwen-burst__4z9SyjA` | 8 | 4 | 166 | 28.6 | 19.6 |
| `sglang-qwen-burst__Y6ovEBk` | 8 | 3 | 161 | 23.0 | 14.2 |
| `sglang-qwen-burst__exuuLAH` | 17 | 11 | 16,751 | 46.9 | 22.7 |
| `sound-change-cascade__WGwuZ5g` | 37 | 25 | 90 | 23.3 | 13.0 |
| `vba-userform-port__PFng8aw` | 32 | 22 | 9,633 | 224.0 | 108.9 |
| `vf2-speedup-networkx__UVeaJc9` | 11 | 8 | 1,287 | 35.9 | 20.1 |
| `vf2-speedup-networkx__bDWTHvP` | 35 | 26 | 1,327 | 39.4 | 18.5 |
| `vf2-speedup-networkx__us8GDaB` | 28 | 21 | 1,309 | 36.8 | 17.4 |
| `vllm-deepseek-streaming__quctgYr` | 27 | 5 | 770 | 109.4 | 76.5 |
| `vllm-deepseek-streaming__rQhEWt9` | 24 | 9 | 3,596 | 104.2 | 69.3 |
| `vllm-deepseek-streaming__t8HdFNj` | 33 | 6 | 2,864 | 57.8 | 31.5 |
