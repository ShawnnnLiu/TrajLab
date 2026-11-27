# Third screen (short tasks), and chains on four tasks: run note (2026-11-20)

Status: screen and chains finished; postprocess, report, and repeat count done; nothing committed.
A probe on the development Mac, not a preregistered run.
Same chain design, procedure, and wording as `2026-11-18_roots6-screen-and-atrx-chains.md` (ADR-0025, unchanged): arms control and record-engine, three roots, one chain per root and arm, budget 20.
Purpose: more tasks to compare short against medium later; grouping (short: under 10 control tool calls per retry) is done afterwards and no group figure is given here.
Facts first; the section "Reading" is leads, not measured effects.

## Runs

Machine and versions as in the roots4 note; repo `6e5acbb` on `sl/exp2-short-screen-2026-11-19`, dirty; subscription credential (`api_key_sources ["none"]`).

### Fresh first attempts: `tb40-passk-fresh-v6`

- Config `configs/harbor/tb40-passk-fresh-v6.json`: eight tasks, 5 attempts each, two at a time; manifest `corpus/manifests/tb40-passk-fresh-v6.json`.
- A first launch was stopped after 20 seconds; its job dir (two cancelled trials, no agent output) was removed on the owner's word and the job rerun under the same name.
- Command: `env -u ANTHROPIC_API_KEY caffeinate -dimsu uv run trajlab run configs/harbor/tb40-passk-fresh-v6.json --allow-dirty`, watcher `trajlab watch corpus/jobs --every 1 --gate change` (the one running since 2026-11-14).
- 2026-11-19 12:51 to 17:16 EST. 40 attempts, 13 passes, no exception, every checkpoint request acknowledged, `apiKeySource` "none" in all 40, $31.46 as the result files report it.
- Passed attempts were pruned as they finished (a scratch loop over `scripts/2026-11-14_exp2_chain_prune.py`'s `prune`), with `pruned.json`.
- Roots: `scripts/2026-11-14_fresh_roots_prune.py corpus/jobs/tb40-passk-fresh-v6 --out corpus/exp2/roots7/roots.json --attempts 5 --roots 3 --tasks 8 --wait`.

| Task | Passes | Tool calls per attempt | Score per attempt | Checkpoint images per attempt (GB) | Kept |
|---|---|---|---|---|---|
| data-anonymization | 0 of 5 | 10, 10, 15, 14, 12 | 6, 6, 6, 6, 5 of 8 tests | 0.9 to 1.8 | yes, then dropped during the chains |
| freecad-platform-drawing | 0 of 5 | 8, 6, 7, 7, 10 | 0.58, 0.53, 0.47, 0.53, 0.62 (`combined_raw`, pass at 0.9) | under 0.01 | yes |
| glycan-ms2-elucidation | 0 of 5 | 6, 8, 5, 8, 8 | 11 of 12 tests, all five | 0.01 | yes |
| ontology-kg-querying | 0 of 5 | 26, 26, 23, 23, 23 | 9, 9, 9, 11, 9 of 13 tests | 0.01 | yes |
| session-window-debug | 0 of 5 | 5, 6, 4, 5, 5 | 6, 4, 5, 6, 6 of 7 tests | under 0.01 | yes |
| photonic-waveguide-routing | 5 of 5 | 6, 11, 7, 6, 7 | 14 of 14 tests, all five | under 0.01 | no: no failed root |
| telecom-entity-resolution | 4 of 5 | 54, 35, 49, 31, 37 | 10, 10, 8, 10, 10 of 10 tests | 0.4 to 2.4 | no: one failed root |
| hof-topology-interpenetration | 4 of 5 | 59, 35, 28, 50, 28 | 38, 38, 38, 38, 35 of 38 tests | 4.8 to 46.6 | no: one failed root; several GB of images per attempt |

Tool calls are counted from each `agent/trajectory.json`; scores are those of `roots.json`; image sizes are the sums of `bytes` over each attempt's checkpoint records.
Safety refusals: none found, by a count of `"stop_reason":"refusal"` in the 40 `claude-code.txt` files only.
Roots, by the median rule: data-anonymization `SpAhXfF`, `53PP7aA`, `WoqJm7u`; freecad-platform-drawing `GNv7SLw`, `QchNvkk`, `59ZCEyD`; glycan-ms2-elucidation `JVwvGia`, `XB4j2N9`, `QFgYyYb`; ontology-kg-querying `CPLteYG`, `5AzQrFB`, `8Fzx57H`; session-window-debug `j4cBxiA`, `84Hnz27`, `WK4kBtU`.

hof-topology-interpenetration's first attempt wrote 36 checkpoints and 38 GB in 54 minutes and took free space to 44 GB.
From then its checkpoint images were removed while its attempts ran, the newest kept (a scratch loop; removals in `corpus/exp2/roots7/hof_midrun_pruned.json`), and the rest at each attempt's end with `pruned.json`; its records are untouched.

### Chains: `roots7`

- `uv run trajlab postprocess corpus/jobs/tb40-passk-fresh-v6`, then `uv run trajlab store build <the three roots> --store corpus/stores/<task>-local-v6` for each of the five kept tasks; every chain complete.
- Plan `corpus/exp2/roots7/plan.json`: lanes `roots7-da`, `roots7-fp`, `roots7-gm`, `roots7-ok`, `roots7-sw`, lane config `configs/harbor/tb40-passk-fresh-v6.json`. `check`: no leaks over 600 prompts.
- Command: `env -u ANTHROPIC_API_KEY caffeinate -dimsu uv run python scripts/2026-11-10_exp2_passk_chains.py run corpus/exp2/roots7/plan.json --chains-root corpus/exp2/roots7 --jobs-dir corpus/exp2/roots7-jobs --max-running 2 --hold-below-gb 30 --prune-ingested`.
- Watcher `trajlab watch corpus/exp2/roots7-jobs --every 1 --gate change`; pruner `scripts/2026-11-14_exp2_chain_prune.py corpus/exp2/roots7-jobs --until-idle 30` (restarted with `--until-idle 60`); `fstrim` every 20 minutes.
- 2026-11-19 17:20 to 2026-11-20 15:40 EST, with nothing running from about 01:00 to 02:24 (below).
- First attempt of each arm checked (data-anonymization `SpAhXfF`): `apiKeySource` "none", requests acknowledged, both prompts equal to the roots6 prompts apart from the task text, record-engine's with "an earlier attempt submitted".
- Report `corpus/exp2/roots7/report.json`; repeat count `corpus/exp2/roots7/repeats.json`; 337 enriched trajectories.

### data-anonymization dropped during the chains

- Its retries wrote far more than its fresh attempts: 6 to 16 GB of checkpoint images in several, because the agent leaves 400 MB test outputs under `/tmp` and every checkpoint copies the writable layer.
- `WoqJm7u` record-engine retry 4 (`data-anonymization__ZZTqQiC`) reached 27 checkpoints and 47 GB in 50 minutes; free space fell to 19 GB (floor 30) and the run held every launch.
- Its container was paused at 01:00 EST; on the owner's word (2026-11-20, "Drop data-anonymization") it was killed at 01:21 and its 27 images removed un-ingested, with `pruned.json`.
  Harbor recorded it with reward 0 and `ApiRateLimitError`; the loop did not count it.
- The run was stopped with `plan.json.stop`, exited at 02:18 when that trial's verifier ended, and was restarted at 02:24 on the plan without `roots7-da`.
- Kept: the full plan as `corpus/exp2/roots7/plan.with-da.json`, a report over it (`report.with-da.json`), the 24 finished attempts, and the three record-engine chain stores (12.5 GB).
- State when dropped: no chain had passed; five chains had 4 counted retries and one had 3; tests passed 6 of 8 in every retry but one (7); 10 to 47 tool calls per retry.

## Results

Four tasks, 24 chains: 14 passed. 337 counted attempts of a possible 480, no exception, no attempt uncounted, no ingest failed, every prompt delivered, $300.52.

| Task | Arm | Chains passed | First-pass retry, by root | Attempts | Cost $ | Minutes | Tool calls per attempt (mean; range) | Repeats |
|---|---|---|---|---|---|---|---|---|
| freecad-platform-drawing | control | 0 of 3 | none | 60 | 8.40 | 174 | 8.0; 3 to 23 | 11 of 60 (see below) |
| freecad-platform-drawing | record-engine | 3 of 3 | 2, 6, 3 | 11 | 2.08 | 32 | 8.5; 4 to 17 | 1 of 11 (see below) |
| glycan-ms2-elucidation | control | 0 of 3 | none | 60 | 17.34 | 177 | 10.4; 7 to 18 | 60 of 60 |
| glycan-ms2-elucidation | record-engine | 3 of 3 | 15, 13, 12 | 40 | 20.62 | 175 | 13.2; 5 to 20 | 37 of 40 |
| ontology-kg-querying | control | 0 of 3 | none | 60 | 107.62 | 553 | 36.0; 20 to 64 | 60 of 60 |
| ontology-kg-querying | record-engine | 0 of 3 | none | 60 | 135.42 | 693 | 46.3; 20 to 79 | 60 of 60 |
| session-window-debug | control | 2 of 3 | 2, 2, none | 24 | 4.46 | 48 | 6.7; 4 to 12 | 21 of 24 |
| session-window-debug | record-engine | 3 of 3 | 9, 7, 6 | 22 | 4.58 | 44 | 5.9; 4 to 9 | 16 of 22 |

Roots are in the order `GNv7SLw`, `QchNvkk`, `59ZCEyD`; `JVwvGia`, `XB4j2N9`, `QFgYyYb`; `CPLteYG`, `5AzQrFB`, `8Fzx57H`; `j4cBxiA`, `84Hnz27`, `WK4kBtU`.

Scores by retry (from the report: `graded` for freecad, tests passed otherwise):

- freecad-platform-drawing (pass at 0.9): control stayed between 0.63 and 0.89 on all three roots for 20 retries; record-engine 0.68, 0.95; 0.65, 0.65, 0.68, 0.89, 0.89, 1.0; 0.65, 0.60, 0.95.
- glycan-ms2-elucidation (12 tests): every failed attempt of both arms passed 11; record-engine's last attempt on each root passed 12.
- ontology-kg-querying (13 tests): both arms between 8 and 11 in every retry.
- session-window-debug (7 tests): control passed two roots at retry 2 and stayed at 5 or 6 on `WK4kBtU` for 20; record-engine between 4 and 6 until its pass.

The repeat counts of freecad-platform-drawing in `repeats.json` (60 of 60 and 8 of 11) are wrong: `scripts/2026-11-16_exp2_chain_repeats.py` reads the reason strings under `base` and `target`, which this task's `reward_details.json` does not have, so every outcome compares equal.
The figures in the table are the same rule applied to the file's top-level `*_reason` strings, computed inline and not saved by a script.

No final message or trajectory of this run was read; every number is from the report and the repeat count, except the freecad repeats and the screen table as stated.

## Reading (leads, not measured effects)

1. On freecad-platform-drawing and glycan-ms2-elucidation control never passed in 60 retries and record-engine passed every root; on glycan it needed 12 to 15 retries.
2. On session-window-debug control passed two roots at retry 2 where record-engine needed 6 to 9, and record-engine passed the root control never did.
3. ontology-kg-querying passed in neither arm and took 81% of the chains' cost; its retries run 36 to 46 tool calls, so it is not a short task by the owner's rule.
4. Control's retries average under 10 tool calls on freecad-platform-drawing and session-window-debug, and 10.4 on glycan-ms2-elucidation.

## Disk

118 GB free at the start; lowest 19 GB (01:00 EST, 2026-11-20); 80 GB at the end after `fstrim`.
All chain attempts have `pruned.json` except the last failed record-engine attempt of each ontology-kg-querying chain (`zYptxng`, `bWaAAX6`, `kFiP2dZ`), which no later attempt ingested; their images are small and still in Docker.
Kept in Docker: the roots of the five kept tasks and telecom-entity-resolution's one failed attempt (`XamDthV`, 0.45 GB).
The preinstalled images of the three dropped screen tasks were removed (rebuildable).

## What the next run should change

- The screen's image check measures fresh attempts only; data-anonymization passed it and then wrote 47 GB in one retry. Cap an attempt's image total while it runs (hold or stop it), not only free space at launch.
- `--hold-below-gb` holds launches but does nothing about an attempt already growing; the floor was crossed with nothing launching.
- Run `fstrim` on a timer from the start: free space fell to 49 GB within an hour from pruned images that had not been trimmed.
- `scripts/2026-11-16_exp2_chain_repeats.py` needs freecad-platform-drawing's `reward_details.json` shape.
- A task cannot be taken out of a running Harbor job; screen suspected heavy tasks one attempt first.
- The chain script, the roots script, and now the passed-attempt prune loop have each run on several data sets: promote them to `cli.py` commands.
