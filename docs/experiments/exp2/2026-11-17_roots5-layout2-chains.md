# Chains on layout-config-recreation2: run note (2026-11-17)

Status: chains finished; report and postprocess done; nothing committed.
A probe on the development Mac, not a preregistered run.
Same chain design and wording as `2026-11-17_roots4-screen-and-wal-chains.md` (ADR-0025, unchanged), with two differences: budget 10, not 20, and a disk hold at 52 GB.
Facts first; the section "Reading" is leads, not measured effects.

## Runs

- Owner's word (2026-11-17): prune what is not needed and run this task's chains; pretrain-shard-corruption is dropped.
- Pruned before the run, each with `pruned.json`: pretrain-shard-corruption's three roots in `tb40-passk-fresh-v3` (71 images) and `fin-saccr-rwa__gRTfc2w` (3 images); the two tasks' preinstalled images removed. Free space 49 GB to 58 GB after `fstrim`.
- Roots, from `tb40-passk-fresh-v4`: `mX4cMaF`, `oSM85xp`, `EGoEQ8h`, each at 8 of 9 tests.
- Store: `uv run trajlab store build <the three roots> --store corpus/stores/layout-config-recreation2-local-v6`.
- Plan `corpus/exp2/roots5/plan.json`: lane `roots5-lc`, budget 10, lane config `configs/harbor/tb40-passk-fresh-v4.json`. `check`: no leaks over 60 prompts.
- Command: `env -u ANTHROPIC_API_KEY caffeinate -dimsu uv run python scripts/2026-11-10_exp2_passk_chains.py run corpus/exp2/roots5/plan.json --chains-root corpus/exp2/roots5 --jobs-dir corpus/exp2/roots5-jobs --max-running 2 --hold-below-gb 52 --prune-ingested`.
- Watcher `trajlab watch corpus/exp2/roots5-jobs --every 1 --gate change`; pruner `scripts/2026-11-14_exp2_chain_prune.py corpus/exp2/roots5-jobs --until-idle 150`; a guard ran `fstrim` every 10 minutes under 54 GB free.
- 2026-11-17 20:59 to 23:28 EST. Machine and versions as in the roots4 note.
- Report: `corpus/exp2/roots5/report.json`. No repeat count: no chain had a failed retry.

## Results

All six chains passed at retry 1. 6 counted attempts of a possible 60, no exception, `api_key_sources ["none"]`, 6 enriched trajectories, 6 of 6 pruned.

| Root | Arm | First pass | Tests | Tool calls | Cost $ | Minutes |
|---|---|---|---|---|---|---|
| `mX4cMaF` | control | 1 | 9 of 9 | 108 | 2.55 | 76.0 |
| `mX4cMaF` | record-engine | 1 | 9 of 9 | 38 | 0.95 | 19.3 |
| `oSM85xp` | control | 1 | 9 of 9 | 66 | 1.31 | 91.1 |
| `oSM85xp` | record-engine | 1 | 9 of 9 | 50 | 1.16 | 34.0 |
| `EGoEQ8h` | control | 1 | 9 of 9 | 74 | 1.51 | 37.0 |
| `EGoEQ8h` | record-engine | 1 | 9 of 9 | 51 | 1.06 | 33.7 |

| Arm | Chains passed | Attempts | Cost $ | Minutes |
|---|---|---|---|---|
| control | 3 of 3 | 3 | 5.37 | 204.1 |
| record-engine | 3 of 3 | 3 | 3.17 | 87.0 |

No final message or trajectory of this run was read; every number is from the report.

## Reading (leads, not measured effects)

1. The task does not separate the arms on passing: one retry is enough for both.
   Knowing only that the earlier attempt failed (control) was enough here.
2. The record-engine attempts were shorter on every root (38 to 51 tool calls against 66 to 108) and cheaper in total; three pairs, so a lead only.
3. A task whose fresh pass rate is 2 of 5 is too easy for the chain design; the next long-trajectory candidates should fail more often on the first attempt.

## Disk

Free space stayed at 56 to 59 GB; the guard never tripped. The three roots' images are kept.
