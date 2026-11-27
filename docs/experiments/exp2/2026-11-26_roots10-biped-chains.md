# Chains on biped-contact-dynamics: run note (2026-11-26)

Status: chains finished; postprocess, report, and repeat count done; nothing committed.
A probe on the development Mac, not a preregistered run.
Same chain design, procedure, and wording as `2026-11-18_roots6-screen-and-atrx-chains.md` (ADR-0025, unchanged): arms control and record-engine, three roots, one chain per root and arm, budget 20.
The screen that produced the roots (`tb40-passk-fresh-v9`: 1 of 5 passed; 55 to 139 tool calls per attempt) is in `2026-11-25_roots8-roots9-short-screens-and-chains.md`.
Facts first; the section "Reading" is leads, not measured effects.

## Run

- Machine, versions, repo state, and credential as in the roots8/roots9 note; the two scripts changed in that note's follow-up are the only code difference, and neither is on the run path.
- Roots `6DEuKzM`, `vmyudzm`, `CRPSoZY` (0, 0, 0 of 3 tests); store `corpus/stores/biped-contact-dynamics-local-v6`.
- Plan `corpus/exp2/roots10/plan.json`: lane `roots10-bc`, lane config `configs/harbor/tb40-passk-fresh-v9.json`. `check`: no leaks over 120 prompts.
- Command: `env -u ANTHROPIC_API_KEY caffeinate -dimsu uv run python scripts/2026-11-10_exp2_passk_chains.py run corpus/exp2/roots10/plan.json --chains-root corpus/exp2/roots10 --jobs-dir corpus/exp2/roots10-jobs --max-running 2 --hold-below-gb 30 --prune-ingested`.
- Watcher `trajlab watch corpus/exp2/roots10-jobs --every 1 --gate change`; pruner `scripts/2026-11-14_exp2_chain_prune.py corpus/exp2/roots10-jobs --until-idle 30`; the scratch checkpoint-size guard ran from the first attempt at 25 GB and never fired.
- 2026-11-25 23:25 to 2026-11-26 07:07 EST.
- Report `corpus/exp2/roots10/report.json`; repeat count `corpus/exp2/roots10/repeats.json`; 8 enriched trajectories of 9.

## Results

All six chains passed. 9 counted attempts of a possible 120, every prompt delivered, `apiKeySource` "none", every checkpoint request acknowledged, no checkpoint timeout.

| Root | Arm | First pass | Cost $ | Minutes | Tool calls, by retry | Tests passed of 3, by retry |
|---|---|---|---|---|---|---|
| `6DEuKzM` | control | 1 | 5.40 | 50 | 81 | 3 |
| `6DEuKzM` | record-engine | 2 | 7.55 | 197 | 60, 116 | 0, 3 |
| `vmyudzm` | control | 2 | 8.60 | 286 | 122, 93 | 0, 3 |
| `vmyudzm` | record-engine | 2 | 3.72 | 144 | 26, 82 | 0, 3 |
| `CRPSoZY` | control | 1 | 4.65 | 41 | 56 | 3 |
| `CRPSoZY` | record-engine | 1 | 4.98 | 138 | 141 | 3 |

| Arm | Chains passed | First-pass retry | Attempts | Cost $ | Minutes | Tool calls per attempt | Repeats |
|---|---|---|---|---|---|---|---|
| control | 3 of 3 | 1, 2, 1 | 4 | 18.64 | 377 | 88.0 | 1 of 4 |
| record-engine | 3 of 3 | 2, 2, 1 | 5 | 16.26 | 479 | 85.0 | 2 of 5 |

- One exception: `vmyudzm` control retry 1 (`biped-contact-dynamics__3tcGaLa`) hit `AgentTimeoutError` at the 14400 s agent cap; the verifier then ran and gave reward 0, and the report counts it as a failed attempt (`not_counted` is empty).
  `trajlab postprocess` refuses that trial ("checkpoint toolu_01U9bpNyQihLZxgSmvgRRskU names no tool call in trajectory.json"): the trajectory ends before the last checkpointed call. No enriched trajectory was written for it.
- The three failed retries each fail all three tests, as the three roots do; that is every repeat counted.
- Test counts are from the pytest summary line of each `verifier/test-stdout.txt`; the report's `graded` field is empty for this task.

No final message or trajectory of this run was read; every number is from the report, the repeat count, and the verifier summary lines.

## Reading (leads, not measured effects)

1. Both arms pass every root within two retries; control first on one root (`6DEuKzM`, 1 against 2), level on the other two. No arm difference to see in nine attempts.
2. Retries run 26 to 141 tool calls: a long task by the grouping rule, not a short or medium one.
3. The screen's 1 of 5 fresh passes against 5 of 9 retry passes is not a retry effect to claim: the timeout multiplier, the prompt, and the sample all differ.

## Disk

56 GB free at the start, 81 GB at the end after `fstrim`.
Pruned after the chains, each with `pruned.json`: all nine attempts and the three roots; Docker holds no checkpoint image.
Kept: every chain store, trial directory, record, and report.

## What the next run should change

1. A retry that hits the 4 h agent cap holds one of two slots for the whole time; give long tasks their own run, or a lower cap fixed in the config before the run.
2. Decide before the run whether an attempt that ends in `AgentTimeoutError` with a verifier result counts toward the budget; the report counts it, and the fresh-roots script excludes attempts with exceptions.

## Figures (2026-11-26)

- `figures/2026-11-26_score_progression/score_by_retry.{png,pdf}`: all 21 tasks, control beside record-engine (`scripts/2026-11-16_exp2_score_progression_figures.py` with `--more` and `--more-roots` for roots4 to roots10); the numbers drawn are in `drawn.json` beside it.
- `figures/2026-11-26_normalized_score/normalized_score_by_retry.{png,pdf}`: the 21 tasks pooled, 63 chains per arm (`scripts/2026-11-18_exp2_normalized_score_figure.py --exp2 corpus/exp2`); `drawn.json` beside it.
  Chains passed by retry 5, 10, 15, 20: control 16, 19, 22, 25; record-engine 20, 31, 35, 40.
  The twelve tasks of 2026-11-18 alone: control 11, 14, 17, 20 of 36; record-engine 14, 20, 21, 25 of 36 (unchanged).
  The nine tasks added since: control 5, 5, 5, 5 of 27; record-engine 6, 11, 14, 15 of 27.
- So that the figures read the report, `scripts/2026-11-10_exp2_passk_chains.py report` now fills `checks_passed` and `checks_total` for a verifier without `ctrf.json` from its `[PASS]`/`[FAIL]` lines (ks-solver-cpp) or its rule table (music-harmony); the roots8 and roots9 reports were regenerated and differ from the earlier ones in those two fields only.
  The ks-solver-cpp root scores in `corpus/exp2/roots9/roots.json` were filled the same way (1 of 2), roots unchanged.
- The pool mixes the first run's wording with ADR-0025's, one task at budget 10, and tasks of very different length; a direction, not an effect size.
- `make lint` clean.
