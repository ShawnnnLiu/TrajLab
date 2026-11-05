# ADR-0023: Exp 2 record arms told the same, and whole file versions in the record

Status: proposed (2026-11-02).
Amends ADR-0022 item 2 (what the record arms are told), ADR-0014 (the `deltas` table: store schema 5), and ADR-0018 item 5 (the bundle's layout).

## Context

`tb40-exp2` found no gain from the query engine at its interim stop (`docs/experiments/exp2/tb40-exp2-interim-2026-10-30.md`: by N = 2, control repaired 8 of 56 failures, record-files 12, record-engine 9).
Prof. Kaffes advised making sure the agents are encouraged to use the engine and know how before concluding that it does not help.
The probe of 2026-11-02 (`docs/experiments/exp2/2026-11-02_engine-uptake-probe.md`, `scripts/2026-11-02_exp2_engine_probe.py`) reran 13 retries on the development Mac and found:

- The agents use the engine and know how: every engine attempt queried in its first or second call, and none of 33 queries failed.
- The prompts `tb40-exp2` ran (ADR-0022 item 2) were not neutral between the record arms.
  Round 2's step 1, which every arm got, says to diagnose "from whatever evidence you have (the files in the environment)", which is where the record-files bundle is and the engine's record is not.
  The engine prompt carried the 14 KB schema text inline (14,949 bytes before the instruction, against record-files' 1,137), where record-files reads the same text in its README when it wants.
- Prompting changed what the agents read, not how much: they ran given queries verbatim and compared deliverables with the failed attempt's when told to.
  On `mvcc-lsm-compaction`, a record-engine prompt saying the failed attempt's own checks had passed (`guided4`) repaired 2 of 3 retries, against 0 of 4 with every other prompt (Fisher exact, two-sided, p = 0.14).
- Reading code through the engine is harder: a diff comes back as one JSON string, and agents decoded it with `python3`.

On 2026-11-02 the owner decided both record arms get the same text but for the medium, and that the record should keep each file whole as well as its diff, so the agent sees a change in context: "keep the entire file, not just the delta so the agent has context on the diff".
A hunk shows 3 lines of context (ADR-0014); a file changed several times can only be read whole by applying its hunks in order.
The build already reads every text version whole: `trajlab.store.reconcile` writes each one to its scratch dir to make hunks, then discards it.

## Decision

0. **Scope.** Exp 2 runs after `tb40-exp2`, under a new run name and preregistration; `tb40-exp2`'s prompts, store (schema 4), and results stay as they ran.
1. **`deltas.text_after`: the whole later version of every row with a hunk** (store schema 5).
   The column follows `hunk`; it is the later version's text when the row has a hunk and the path still has content after the step (a file's text, or a symlink's target as the hunk shows it), and null for a delete, a directory, and a row without a hunk.
   For a file, `sha256(text_after)` is `sha256_after`.
   The earlier version is the previous row's `text_after` for the path, or for a path's first change the start image's file, which a retry's fresh environment holds at the same path.
   `SCHEMA_VERSION` is 5, so a store of schema 4 is refused and built again from its trial dirs and images (ADR-0020's rule; no upgrade in place).
2. **The bundle has each `text_after` as a file:** `versions/<trial_id>/<step_idx>/<path>`, beside `deltas/<trial_id>/<step_idx>/<path>.diff`, written and verified the same way (`trajlab.store.export`).
   A directory with no file to hold is not made.
3. **The descriptions are on the store:** `record.duckdb` carries every relation's and column's description, the README's text, as DuckDB comments (`trajlab.store.describe.comment_statements`), set when the file is created and again when its views are replaced.
   An agent reads them with `duckdb_columns()`, `duckdb_tables()`, and `duckdb_views()`, which `connect_agent` allows.
4. **The record arms' text is one text but for the medium** (`trajlab.exp2.prompt`; replaces ADR-0022 item 2's evidence sentences for the record arms).
   After the note, both record arms get, apart by blank lines:
   - what the record holds and that it is the best evidence (`RECORD_INTRO`), whole files included;
   - how to reach it, the arm's only own paragraph: the query command, read-only DuckDB, JSON lines, the row cap (`ENGINE_ACCESS`); or the mount, `<table>.jsonl`, `deltas/`, and `versions/` (`FILES_ACCESS`);
   - four protocol steps that work from the record (`RECORD_STEPS`): the diagnosis first, from questions 1 to 3, before the first command; the failed attempts' own checks prove little, so add checks for what they missed; look up an approach before trying it; compare deliverables with the failed versions;
   - the failed attempts' conclusions as claims to check (`CLAIMS`);
   - a one-line-per-table guide (`TABLE_GUIDE`), then where the full descriptions are: a `duckdb_columns()` query, or `README.md`;
   - six questions (`QUESTIONS`), each answered by ready queries in record-engine and by the files that hold the answer in record-files.
     The file question (5) has one query whose rows carry each change's `hunk` and its `text_after` together, so every diff an engine agent reads that way comes with the whole file; record-files is pointed at the `.diff` and the `versions/` file of each step in the same sentence.
     The owner asked for whole files "included with each diff" (2026-11-03); a second query for one step's `text_after` would leave the whole file to be fetched on request, and agents run the given queries as written.
   `control` keeps round 2's four steps (`PROTOCOL_STEPS`) unchanged.
   The record-engine prefix is 5,496 bytes and record-files' 4,476 (k = 1).
5. **The preregistration fixes the record's descriptions by hash:** `Exp2Settings.schema_sha256` is the sha256 of the schema text (null in settings written before this ADR), and `drift` reports a change to it; the prefix hashes no longer depend on the store.
6. **Whole files are compared before a pilot.**
   The probe script's `v2` runs this ADR's text and store; `v2-nofile` runs the same text with every mention of whole files taken out and the chain store's `text_after` emptied, so its bundle has no `versions/`.
   The two differ only in the whole files; the comparison is run on one task in both record arms and recorded in the probe's run note.

## Consequences

- The next run needs a new run name, preregistration, and corpus store: `tb40-sonnet-v4`'s store is schema 4 and is built again on the capture host from the corpus trial dirs and their checkpoint images, which the owner's rule keeps.
- Whole versions cannot be rebuilt from images for `tb40-exp2`'s record-arm retries, whose images were pruned once ingested (ADR-0022 Validation); for those, each version is the stored hunks applied in order to the start image's file, checked against `sha256_after`, which is not built.
- Store and bundle grow by the text of every changed text version.
  A file a step added is stored twice, since its hunk is the whole file; a file changed in place adds its full size at every change.
  On the two local roots, the hunks hold 7,547 and 211,116 characters and `text_after` 14,831 and 236,021.
- A whole file still reaches a record-engine agent as one JSON string; a plain-text answer mode for the query tool (an ADR-0019 amendment) remains open.
- The paper's comparison of the record arms is under this ADR's text for any run after it; `tb40-exp2`'s comparison carries the imbalance in `docs/paper-notes.md`.
- Tests: `tests/test_store_reconcile.py` (the column's rule and hash), `tests/test_store_export.py` (`versions/` written and verified), `tests/test_exp2_prompt.py` (the record arms differ only in the medium; every query given runs on a built store; every file pointed to is in the bundle; the store's comments are the README's descriptions), and `tests/test_exp2_prereg.py` (`schema_sha256` and its drift).

## Validation (2026-11-02)

- `uv run pytest`: 1,345 passed and 13 failed on the development Mac, none of the 13 from this change: 12 in `tests/test_checkpoint_writes.py` use `time.CLOCK_BOOTTIME`, which macOS lacks (the write recorder is Linux-only, ADR-0021), and `tests/test_store_serve.py::test_the_tool_answers_a_query_and_the_host_logs_it` failed under the full suite's load and passed 3 times of 3 alone.
- `trajlab store build` from the local checkpoint images: `corpus/stores/mvcc-local-v5` (`probe-mvcc-first`, 3 trials, 16 delta rows, 7 with a hunk) and `corpus/stores/pp-local-v5` (`production-planning__PckZuoA`, 51 delta rows, 44 with a hunk); every `text_after` of a file hashes to its row's `sha256_after` (51 of 51).
- Every `v2` and `v2-nofile` prompt for both roots and both record arms passes `trajlab.exp2.leaks` against the task's tests, and each query it gives runs on its chain store.

## Validation (2026-11-03)

- After the file question became one query (item 4), `tests/test_exp2_prompt.py`, `tests/test_store_export.py`, and `tests/test_exp2_prereg.py` pass (53), and the leak check and query runs above were repeated for all 8 prompts, with no leak; on the two roots the file query returns a whole file on every row in `v2` (1 of 1 on mvcc, 5 of 5 for pp's `plan2.py`) and on none in `v2-nofile`.
- Item 6's comparison ran on `mvcc-lsm-compaction`, 5 retries in each of the 4 cells (`docs/experiments/exp2/2026-11-02_engine-uptake-probe.md`, "Results on mvcc"): `v2` passed 5 of 10 and `v2-nofile` 5 of 10 (record-engine 3 of 5 against 2 of 5, record-files 2 of 5 against 3 of 5; Fisher exact, two-sided, p = 1.0 in each).
- One `v2` attempt of 10 read a whole file; no record-engine agent ran the file query, and each wrote its own diff query without `text_after`.
  So the comparison does not show whether whole files help: offering them as a separate column or directory does not put them in front of the agent.
  A test of whole files needs them where agents already look (for example, full-context diffs in `hunk` and the `.diff` files) and roots that rewrote large files several times.
