# ADR-0018: Exp 2's derived record as SQL views, and the record-files export

Status: proposed (2026-10-26).

## Context

Build-order step 3 of the technical spec (§7) is `derive.py` and `export.py --verify`, done when CI is green on the fixture and the export round-trips.
It settles the spec's §8 questions 2 (the `args_json` normalization for `is_loop`, and whether derived columns are recomputed or appended when a retry is ingested) and 7 (whether derived columns are in the `record-files` bundle), which ADR-0014 carries as its open questions 1 and 4.

ADR-0014 §9 made `is_loop`, `is_revert`, and `files_touched` columns of `steps` in schema version 1, null until this step.
On the ten example trials (`docs/experiments/exp2/2026-10-19_example-runs.md`) the spec's `is_loop` and `is_revert` were zero everywhere, failures included.
The owner's direction (2026-10-20): byte-for-byte repetition is the wrong definition, the derived record is defined from observed failures, and whatever is defined is frozen and present in both record arms before the full run.
ADR-0016 (proposed 2026-10-25, item 5) sets the rule set's three layers after arXiv 2607.09510: occurrence views per trial, behavior flags per Table III row, and specification views.
It takes `unverified_completion` from ADR-0015's detector, which was never built; only ADR-0015's labeling site (`trajlab trace`) exists, with the same test-command and success-claim patterns.

A store cannot be rebuilt once its checkpoint images are deleted (ADR-0014, item 5), and the corpus box's images are deleted after the store is built.
A rule changed after that must therefore apply to an existing store without its images.

## Decision

0. **Scope.** The Exp 2 store's derived record and the `record-files` bundle: `trajlab.store.derive`, `trajlab.store.describe`, `trajlab.store.export`, and store schema version 2.

1. **Derived values are SQL views in `record.duckdb`, created with the tables.**
   `step_flags` has one row per step row, `delta_flags` one per delta row, `path_versions` one per changed path of a trial, and `trial_summary` one per trial.
   Every view is a function of one trial's rows, so a trial's values do not depend on the other trials in its store (a test checks this).
   The spec's recompute-or-append question has no case left: a retry built into a chain's store is in every view at once.
   An agent's connection (`connect_agent`) reads the views like tables.

2. **Schema version 2 drops `steps.is_loop`, `steps.is_revert`, and `steps.files_touched`.**
   `files_touched` is `step_flags.files_touched`, the spec's definition (delta rows of the step).
   `is_revert` is `delta_flags.is_revert`, the spec's definition per delta row: the later content is what an earlier row of the path replaced.
   `is_loop` is not implemented; the same-approach family that ADR-0016 has fill it is there under its own names (`delta_flags.variant_of`, `step_flags.is_idle_wait`, `trial_summary.scripts_added`), so no column carries a name its definition no longer fits.
   `StoreMeta` gains `derived`.

3. **The rules, as candidates until the preregistration freezes them (ADR-0016, item 5).**
   The patterns and thresholds are constants of `trajlab.store.derive`, each named in the view's column descriptions.
   - A *cache file* is under `__pycache__/` or `.pytest_cache/`, or ends in `.pyc`.
   - A step is *mutating* when it is a tool call (not a message, not a stop record) with a delta row outside cache files.
   - An *error marker* is `is_error`, or an output with a non-zero exit (`Exit code N`, `[exit_code] N`), a Python traceback, a failing test line (`FAILED`, `FAIL:`, `N failed`, `test result: FAILED`), or a line starting `...Error:`.
   - A *tail probe* is a tool call that is not mutating, after the trial's last mutating step (every tool call, when none mutates).
   - An *idle wait* is a tool call that is not mutating and ran over 120 s (`t_end - t_start`).
   - A *test command* is a Bash call whose command matches the trace site's test-runner pattern; a *success claim* is a message matching its success pattern.
   - `names_prompt_path` is true when the path equals an absolute path in `trials.prompt` (its last character not a period); a path under a named directory does not count.
   - `version` numbers a path's delta rows in step order.
   - A *script add* is an `add` row of a path ending in `.py`, `.sh`, `.bash`, `.js`, `.mjs`, `.ts`, `.rb`, `.pl`, `.r`, or `.jl`, outside cache files.
   - `variant_of` names, for a script add with at least 5 distinct non-blank lines, the script added at an earlier step whose first-written lines include at least half of them, the one sharing the most (the earliest on a tie).
   - `unverified_completion` is true when the trial's last agent message is a success claim, and no test command ran at or after the last mutating step, or the last test command has an error marker.
     This is ADR-0015's definition, computed from the store's rows instead of the unbuilt detector.

4. **The views are versioned apart from the tables.**
   `StoreMeta.derived` is `DERIVE_VERSION` plus the sha256 of the views' SQL, so a change to any pattern changes it.
   `create_store` refuses a store whose `derived` differs, naming the command that fixes it.
   `trajlab store derive <store>` replaces the views in place, in one transaction, and records the new id in both files' `store_meta`; rows are untouched, so it needs no images.
   It also upgrades a store of schema version 1 in place: `steps` loses the three columns, which no code ever filled (refused if any row has a value in one), and `store_meta` gains `derived`.
   Every other reader refuses a schema-1 store with an error naming that command.
   The preregistration records the id.

5. **The `record-files` bundle is every relation an agent's connection can read, exported from `record.duckdb` alone.**
   `trajlab store export <store> <out>` writes `<relation>.jsonl` for `trials`, `steps`, `deltas`, the four views, and `store_meta`, one JSON object per row in DuckDB's own encoding, sorted.
   Each delta row's hunk is also written to `deltas/<trial_id>/<step_idx>/<path>.diff`, so the record can be grepped as files.
   Blob contents are not in the bundle, because `connect_agent` cannot read `blobs/` either; both arms see `blob_ref` only.
   The bundle is written beside `<out>`, verified, and only then renamed into place, so `<out>` either holds a verified bundle or does not exist.
   `--verify` checks an existing bundle: the file set, every row's keys, each relation's rows as a multiset, the README, and every hunk file.

6. **One schema text for both record arms.**
   `trajlab.store.describe.describe(con)` lists every relation in a fixed order, each column with its type from DuckDB's catalog and its description from the row models of `trajlab.contracts` or the views' specs.
   A relation or column without a description is an error.
   The bundle's `README.md` is a fixed layout paragraph followed by this text.
   The `record-engine` prompt should carry the same text, so the two arms are told the same about the record.
   ADR-0016 item 1 gives that arm `DESCRIBE` output and an example row per table, and the README only field names; which one both get is the owner's call before step 5.

## Consequences

- Stores of schema version 1 (the Mac example stores, and a `tb40-sonnet-v3` store built before this lands) are upgraded with `trajlab store derive`, which needs no images; rebuilding them would.
- The bundle has more files than ADR-0016 item 1's evidence sentence names (`deltas.jsonl`, the four views, `store_meta.jsonl`); step 5 renders that sentence from the bundle's layout.
- Parity between the arms is checked, not assumed: a test compares the bundle's files with the relations `connect_agent` lists, and `--verify` compares every row.
- Not here: ADR-0016's per-failure timeline (a held-out table that needs the ground-truth loader), `first_signal_step`, and the query tool (step 4).
- `scripts/2026-10-20_exp2_example_rules.py` was the source of several rules and reads schema-1 stores only; it is not run again.
- Changing a rule before the preregistration is an edit to `trajlab.store.derive` and a `trajlab store derive` on each store; after it, a new ADR amendment and a new derived id.

## Validation (2026-10-26)

- `make test`: 1015 passed, 2 skipped.
  `tests/test_store_derive.py` checks each rule on hand-made rows; `tests/test_store_export.py` exports the fixture trial's store and reads it back exactly, and checks that `--verify` catches an edited, dropped, or extra row, an extra key, missing and extra files, an edited README, and edited, missing, and extra hunk files.
- The production-planning example (`corpus/jobs/exp2-example-v2`, 5 trials), rebuilt under schema 2 on the Mac from its Docker images:

      uv run trajlab store build corpus/jobs/exp2-example-v2 --store <store> --work-dir <scratch>
      uv run trajlab store export <store> <bundle>

  117 steps and 236 deltas, as in the schema-1 store; every chain complete; 11 to 14 s per trial.
  The export took 0.9 s: 3.4 MB, 196 hunk files, verified.
- The same trials' schema-1 store, built by the previous code (`corpus/stores/exp2-example-v2`), upgraded in place with `trajlab store derive`: every relation, views included, equals the freshly built schema-2 store's row for row.

| Trial | Reward | Tool calls | Mutating | First error | Tail probes | Idle waits | Scripts | Variants | Deliverable versions | Unverified completion |
|---|---|---|---|---|---|---|---|---|---|---|
| `5hnrmLq` | 1 | 17 | 14 | 4 | 0 | 0 | 8 | 1 | 9 | yes |
| `H2AcDwK` | 0 | 19 | 8 | 4 | 0 | 1 | 1 | 0 | 16 | yes |
| `PckZuoA` | 0 | 21 | 19 | 4 | 0 | 0 | 5 | 1 | 9 | yes |
| `kugWvNv` | 0 | 32 | 23 | 4 | 0 | 1 | 11 | 6 | 10 | yes |
| `qNsiDSN` | 0 | 18 | 15 | 4 | 0 | 0 | 6 | 0 | 23 | yes |

- Deliverable versions (`prompt_path_versions`) and idle waits equal the example runs' table.
- `variant_of` marks `kugWvNv`'s thrashing: `p2.py` to `p6.py` as variants of `plan.py` (then of `p2.py`), and `ls.py` of `rnd.py`; the pass has one (`optB.py` of `opt.py`), and so does `PckZuoA` (`plan2.py` of `plan.py`).
- No trial has a tail probe: the task's gateway appends to `/app/output/audit.log` on every call, so every call there is mutating.
- The first error is step 4 in every trial, a failed query while exploring the data (a traceback in four, a non-zero exit in `qNsiDSN`); this is why the first error is not the decisive step (ADR-0016's `t_err`).
- `unverified_completion` is true for all five, the pass included, because no attempt ran a recognized test runner.
  On a task without one the flag says nothing; before the preregistration it is either narrowed (for example to tasks whose instruction names a test command) or reported with that caveat.
