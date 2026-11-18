# Second screen, and chains on atrx-vep-crispr: run note (2026-11-18)

Status: screen and chains finished; postprocess, report, and repeat count done; nothing committed.
A probe on the development Mac, not a preregistered run.
Same chain design, procedure, and wording as `2026-11-17_roots4-screen-and-wal-chains.md` (ADR-0025, unchanged): arms control and record-engine, three roots, one chain per root and arm, budget 20.
Facts first; the section "Reading" is leads, not measured effects.

## Runs

Machine and versions as in the roots4 note; subscription credential (`api_key_sources ["none"]`).

### Fresh first attempts: `tb40-passk-fresh-v5`

- Config `configs/harbor/tb40-passk-fresh-v5.json`: atrx-vep-crispr and embedding-drift-monitor, 5 attempts each, two at a time.
  layout-config-recreation was taken out of the config before the run: five failed attempts at about 11 GB of images each do not fit above the 30 GB floor.
- Command: `env -u ANTHROPIC_API_KEY caffeinate -dimsu uv run trajlab run configs/harbor/tb40-passk-fresh-v5.json --allow-dirty`, watcher `trajlab watch corpus/jobs --every 1 --gate change`.
- 2026-11-17 23:36 to 23:58 EST. 10 attempts, 5 passes, no exception, every checkpoint request acknowledged.
- Passed attempts were pruned as they finished (a scratch loop over `scripts/2026-11-14_exp2_chain_prune.py`'s `prune`), with `pruned.json`.
- Roots: `scripts/2026-11-14_fresh_roots_prune.py corpus/jobs/tb40-passk-fresh-v5 --out corpus/exp2/roots6/roots.json --attempts 5 --roots 3 --tasks 2 --wait`.

| Trial | Tests passed | Reward | Checkpoints | Minutes | Cost $ | Requests/acks |
|---|---|---|---|---|---|---|
| `atrx-vep-crispr__2rQmNqC` | 14 | 0 | 13 | 10 | 0.73 | 27/27 |
| `atrx-vep-crispr__6zUStrp` | 14 | 0 | 9 | 5 | 0.42 | 19/19 |
| `atrx-vep-crispr__FnXuYNp` | 11 | 0 | 10 | 6 | 0.43 | 18/18 |
| `atrx-vep-crispr__MiZk9Qc` | 6 | 0 | 11 | 5 | 0.52 | 20/20 |
| `atrx-vep-crispr__V525sES` | 13 | 0 | 7 | 7 | 0.74 | 22/22 |
| `embedding-drift-monitor__29MLQm8` | 11 | 1 | 3 | 4 | 0.15 | 7/7 |
| `embedding-drift-monitor__7oStctu` | 11 | 1 | 2 | 6 | 0.17 | 8/8 |
| `embedding-drift-monitor__NgUQvrz` | 11 | 1 | 4 | 5 | 0.18 | 7/7 |
| `embedding-drift-monitor__XoD9NCD` | 11 | 1 | 4 | 6 | 0.18 | 9/9 |
| `embedding-drift-monitor__d8SspGi` | 11 | 1 | 3 | 6 | 0.18 | 8/8 |

Tests are out of 16 (atrx) and 11 (embedding).
embedding-drift-monitor is dropped: 5 of 5 passed, no failed root.
atrx-vep-crispr is kept: 0 of 5 passed; roots `V525sES`, `2rQmNqC`, `6zUStrp`.

### Chains: `roots6`, atrx-vep-crispr

- `uv run trajlab postprocess corpus/jobs/tb40-passk-fresh-v5`, then `uv run trajlab store build <the three roots> --store corpus/stores/atrx-vep-crispr-local-v6`.
- Plan `corpus/exp2/roots6/plan.json`: lane `roots6-av`, lane config `configs/harbor/tb40-passk-fresh-v5.json`. `check`: no leaks over 120 prompts.
- Command: `env -u ANTHROPIC_API_KEY caffeinate -dimsu uv run python scripts/2026-11-10_exp2_passk_chains.py run corpus/exp2/roots6/plan.json --chains-root corpus/exp2/roots6 --jobs-dir corpus/exp2/roots6-jobs --max-running 2 --hold-below-gb 30 --prune-ingested`.
- Watcher `trajlab watch corpus/exp2/roots6-jobs --every 1 --gate change`; pruner `scripts/2026-11-14_exp2_chain_prune.py corpus/exp2/roots6-jobs --until-idle 30`.
- 2026-11-18 00:03 to 02:21 EST.
- First attempt of each arm checked (`V525sES`): `apiKeySource` "none", requests acknowledged, both prompts equal to the roots4 prompts apart from the task text, record-engine's with "an earlier attempt submitted".
- Report `corpus/exp2/roots6/report.json`; repeat count `corpus/exp2/roots6/repeats.json`; 28 enriched trajectories.

## Results

All six chains passed. 28 counted attempts of a possible 120, no exception, every prompt delivered, 28 of 28 pruned.

| Root | Arm | First pass | Cost $ | Minutes | Tests passed of 16, by retry |
|---|---|---|---|---|---|
| `V525sES` | control | 3 | 2.90 | 30 | 14, 14, 16 |
| `V525sES` | record-engine | 4 | 4.37 | 33 | 15, 15, 11, 16 |
| `2rQmNqC` | control | 10 | 9.11 | 89 | 12, 9, 10, 14, 10, 13, 13, 12, 11, 16 |
| `2rQmNqC` | record-engine | 5 | 3.13 | 30 | 9, 15, 11, 12, 16 |
| `6zUStrp` | control | 5 | 4.95 | 48 | 9, 14, 14, 10, 16 |
| `6zUStrp` | record-engine | 1 | 0.84 | 9 | 16 |

| Arm | Chains passed | Attempts | Cost $ | Minutes | Tool calls per attempt | Repeats |
|---|---|---|---|---|---|---|
| control | 3 of 3 | 18 | 16.96 | 166 | 27.0 | 5 of 18 |
| record-engine | 3 of 3 | 10 | 8.35 | 71 | 18.4 | 1 of 10 |

No final message or trajectory of this run was read; every number is from the report and the repeat count.

## Reading (leads, not measured effects)

1. Both arms pass every root; record-engine in fewer attempts in total (10 against 18) and at half the cost, but it was slower on one root of three (`V525sES`, 4 against 3).
2. Scores move up and down between retries in both arms (few repeats), unlike wal-recovery-ordering and bun-sourcemap-leak, where control stayed on the root's failing tests.
3. Retries here run 18 to 27 tool calls; the fresh attempts wrote 7 to 13 checkpoints. A middle-length task, not a long one.

## Disk

Free space 54 to 56 GB throughout; 56 GB at the end after `fstrim`. Kept: the three atrx roots and all images named in the earlier notes.

## Not done

- layout-config-recreation: not screened; deferred by the owner on 2026-11-18, to take up later.
  Expected cost is about 11 GB of checkpoint images per attempt (as layout-config-recreation2), so five fresh attempts do not fit above the 30 GB floor; screen one attempt at a time, pruning each pass as it finishes.
  layout-config-recreation2's three roots were pruned on 2026-11-18 (owner's word), each with `pruned.json`.

## Disk clear-out (2026-11-18, owner's word: "prune as much as possible")

- Every remaining checkpoint image was removed, trial by trial, each with `agent/checkpoints/pruned.json`: 75 trials, among them the roots of every run (trend, roots2 to roots6), all of `tb40-passk-fresh-v1`, and the four unpruned `roots3` record-engine attempts.
- Then `docker image prune -a -f`: the preinstalled images and the Terminal-Bench base images (rebuilt and pulled again on the next run).
- `fstrim` released 107.3 GiB; free space on the Mac 47 GB to 125 GB. Docker now holds no images.
- Kept: every trial directory, checkpoint record, report, and chain store, including the three gsea-proteomics stores under `corpus/exp2/trend/` (10.3 GB).
- Consequence: no store can be rebuilt and no retry can start from an existing root; new chains need fresh roots.

## Figures (2026-11-18)

- `figures/2026-11-18_score_progression/score_by_retry.{png,pdf}`: all twelve tasks, control beside record-engine (`scripts/2026-11-16_exp2_score_progression_figures.py` with `--more` and `--more-roots` for roots4 to roots6).
- `figures/2026-11-18_normalized_score/normalized_score_by_retry.{png,pdf}`: the twelve tasks pooled, 36 chains per arm (`scripts/2026-11-18_exp2_normalized_score_figure.py --exp2 corpus/exp2`).
  Chains passed by retry 5, 10, 15, 20: control 11, 14, 17, 20; record-engine 14, 20, 21, 25.
  The pool mixes the first run's wording with ADR-0025's and one task at budget 10; a direction, not an effect size.
- `make lint` clean; `pytest -q` with the waypoint test deselected: 1427 passed, 2 skipped.
