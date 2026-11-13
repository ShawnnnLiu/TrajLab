# ADR-0023: Exp 2 record arms told the same, and whole file versions in the record

Status: proposed (2026-11-02); amended 2026-11-04 (whole-file hunks, store schema 6; below).
Amends ADR-0022 item 2 (what the record arms are told), ADR-0014 (the `deltas` table: store schema 5, and from the amendment on, the hunk's context: schema 6), and ADR-0018 item 5 (the bundle's layout).

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

## Amendment (2026-11-04): whole files inside the hunks, and the file question in the diagnosis

### Context

Item 6's comparison (Validation 2026-11-03) did not test whether whole files help, because the agents did not read them: one `v2` attempt of 10 opened a whole file.
Engine agents ran question 1's query as written, then wrote their own diff query (`SELECT d.path, d.hunk FROM deltas d`, or `left(hunk, 3000)`) and never ran question 5's, which needed a path filled in (`'/path/to/file'`); files agents read the `.diff` files and, but for one, not `versions/`.
Protocol step 1 said "Answer questions 1 to 3", and the file question was question 5, so the diagnosis never pointed at what the attempts wrote.
So whole files go where agents already look, the `hunk` column and the `.diff` files, and the file question goes where the diagnosis looks.
Agents' own cuts on 2026-11-03 were `left(..., 300)` for calls, `left(output, 600)`, and `left(hunk, 3000)`.

A hunk that holds a whole file can be long, so the limits of the agent's shell decide how it is read.
Claude Code 2.1.278 (`trajlab.capture.pins.CLAUDE_CODE_VERSION`), read from the binary in the pre-installed mvcc image on 2026-11-04: a Bash result longer than `bashOutputMaxChars` characters (default 30,000, clamped to 4,000 to 128,000; `BASH_MAX_OUTPUT_LENGTH` alone sizes only the read-back window) is saved to a file, and the agent receives "Output too large (...). Full output saved to: <path>" and a 2,000-byte preview; its Read tool reads 2,000 lines by default, at most 262,144 bytes and 25,000 tokens per read, with `offset` and `limit` for parts.
Harbor's `ClaudeCode` agent sets none of these (`harbor/agents/installed/claude_code.py`), and trajlab sets only `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (ADR-0005).

### Decision

1. **A text row's hunk holds the whole file** (`trajlab.store.reconcile`; ADR-0014's 3 lines of context no longer).
   It is one hunk with every line of both versions: an unchanged line once with " ", a removed line with "-", an added line with "+", with the same headers, line splitting, "\\ No newline at end of file" markers, and line matching as before.
   So its " " and "+" lines are the later version and its " " and "-" lines the earlier (`hunk_versions`), and the changed lines and their counts are what they were.
   `narrow(hunk, n)` cuts a hunk to n lines of context around each change; with n = 3 it gives schema 5's hunk byte for byte.
   `RECONCILE_METHOD` is `top-layer-view-v2`, whose text also corrects v1's exact-matching cap to the code's 60,000 line product (v1's text said 250,000).
2. **`deltas.text_after` and the bundle's `versions/` are gone** (store schema 6; replaces items 1 and 2).
   The hunk holds both versions, a separate place went unread (item 6), and keeping both would store every later version twice.
   The bundle's `.diff` files are the whole-file hunks; its README, the `deltas` and `hunk` descriptions, and therefore the store's comments (item 3) say a hunk holds the whole file.
3. **Five questions, the files among the diagnosis's** (`trajlab.exp2.prompt.QUESTIONS`, `RECORD_STEPS`; replaces item 4's six questions).
   1 and 2 are as before (how each attempt ended; the checks it ran and what it did after its last change).
   3 is "What each attempt wrote into its files, version by version, the files the task names first: each change as a diff that holds the whole file, the changed lines marked": for record-engine, the file list from `path_versions` and every text change's hunk outside cache files, task-named files first, both queries running as written; for record-files, `path_versions.jsonl` and the `.diff` files.
   It folds in the old question 4 (which files) and replaces the old question 5 (one file, with a path to fill in).
   4 is every call in order (the old 3), 5 one call in full (the old 6).
   Step 1 answers questions 1 to 4, so the diagnosis reads the files; step 4 compares the deliverables with question 3's versions.
4. **The given queries are at least as wide as the agents' own, and none cuts a hunk:** calls at 300 characters, outputs at 600 (question 4) and 800 (question 2), hunks whole.
5. **Each access paragraph says how to read past the output cap** (`OUTPUT_CAP`, `BASH_OUTPUT_CHARS`, `PAGE_CHARS`): "A command's output longer than 30,000 characters is cut to its first 2,000, with the path of a file that holds all of it", then for record-engine "read a long value in parts: substr(column, 1, 20000), then substr(column, 20001, 20000), and so on", and for record-files "read a long file in parts: with the Read tool's offset and limit, or sed -n".
   20,000 leaves room under the cap for JSON escaping and the other columns of a row.
   `RECORD_INTRO` and the table guide say each change is "a diff that holds the whole file with the changed lines marked".
   The record-engine prefix is 5,810 bytes and record-files' 4,632 (k = 1); control's is unchanged.
6. **Whole-file hunks are compared with 3-line hunks before a pilot** (replaces item 6 for runs after this amendment).
   The probe script's `v3` runs this text and store; `v3-ctx3` runs the same text with each mention of whole-file diffs replaced (`with_3_line_hunks`), every hunk of the chain store narrowed to 3 lines (`narrow_store`), and the `deltas` and `hunk` descriptions put back to a plain unified diff in the store's comments and, for record-files, the bundle's README (`readme_3_lines`).
   The two differ only in how much of each file a diff shows.
   It runs in both record arms on `mvcc-lsm-compaction` (5 reps) and `production-planning` (3 reps), the two tasks at once with one container each (`scripts/2026-11-04_exp2_full_context.sh`).

7. **The diagnosis is due before any file is changed, and record-files agents are pointed at the diffs first** (2026-11-05, after the comparison of item 6; `RECORD_STEPS`, question 3's files answer).
   Step 1 asked for the diagnosis "before your first command in the environment"; a record-files agent's first read of its record is such a command, and record-files agents wrote the diagnosis in 2 of 10 mvcc attempts against 9 of 10 under the text of 2026-11-03 (Validation 2026-11-05).
   It now reads "Before you change any file, write them to /logs/agent/diagnosis.md", as control's step 1 asks before changing a file; both record arms get the same step.
   Question 3's files answer named path_versions.jsonl first, and 4 of 5 record-files agents read the calls in steps.jsonl and never opened a `.diff`; it now starts with the `.diff` files, "the whole file with the changed lines marked", and the command that lists them (`find /logs/agent/record/deltas -name '*.diff'`), then path_versions.jsonl.
   The record-engine prefix is 5,793 bytes and record-files' 4,711 (k = 1).
   The probe's `v4` runs this text; `v3` and `v3-ctx3` ran the text before it.

### Consequences

- Schema 5 stores are refused and built again from their trial dirs and images; the two local roots are rebuilt as `corpus/stores/mvcc-local-v6` and `corpus/stores/pp-local-v6`, and the next run's corpus store is built at schema 6 on the capture host.
- The store gets smaller: on the two roots, the hunks hold 16,137 and 266,227 characters, against 22,378 and 447,137 for schema 5's hunks and `text_after` together (28% and 40% less).
  Against schema 5's hunks alone they are 2.1 and 1.3 times as long; production-planning's root added most of its files whole (input data dumped to JSON), which a 3-line hunk already shows whole, while its `plan2.py` and `plan.py` diffs grow 3.5 and 2.8 times.
- A hunk is at most about both versions of a file of up to `MAX_TEXT_BYTES` (1 MiB), so a long answer is normal: production-planning's file query answers 300,252 characters with whole-file hunks and 241,349 with 3-line hunks, both past the cap, against 5,075 and 2,596 on mvcc.
- A record-engine agent still reads a hunk as one JSON string; a plain-text answer mode for the query tool (an ADR-0019 amendment) remains open.
- Analysis that reads hunks is unchanged in what it computes: `delta_flags` reads the "+" lines of an add, which are the same, and `path_versions.last_hunk` is the last hunk, now whole; code that wants short diffs calls `narrow`.
- `tb40-exp2` (schema 4) and item 6's probe runs (schema 5, at 7a4001a) stay as they ran; the probe's `v2` and `v2-nofile` refuse to run at schema 6.
- Tests: `tests/test_store_reconcile.py` (whole-file hunks; `hunk_versions` and `narrow` round-trip against the renderer for every context on 1,500 random edits under each matching method, and equal difflib's at 3 lines and at the whole file; malformed hunks refused), `tests/test_exp2_prompt.py` (the diagnosis answers the file question; no given query cuts a hunk; the cuts' widths; the paging reads a hunk back whole; every given query runs as written), `tests/test_store_export.py` and `tests/test_store_build.py` (no `versions/`, no `text_after`).

### Validation (2026-11-04)

- `make lint` passes; `make test`: 1,353 passed, 2 skipped, and the 12 known failures of `tests/test_checkpoint_writes.py` on macOS (`time.CLOCK_BOOTTIME`).
- `uv run trajlab store build corpus/jobs/probe-mvcc-first --store corpus/stores/mvcc-local-v6 --work-dir <scratch>` (26.6 s; 3 trials, 16 delta rows, 7 with a hunk) and `uv run trajlab store build corpus/jobs/exp2-example-v2/production-planning__PckZuoA --store corpus/stores/pp-local-v6 --work-dir <scratch>` (10.7 s; 51 delta rows, 44 with a hunk), from the kept checkpoint images.
- `uv run python scripts/2026-11-04_full_context_stores.py --pair corpus/stores/mvcc-local-v6 corpus/stores/mvcc-local-v5 --pair corpus/stores/pp-local-v6 corpus/stores/pp-local-v5`: the same rows in both, every hunk's " " and "-" lines hash to `sha256_before` and its " " and "+" lines to `sha256_after`, `narrow(hunk, 3)` is the schema 5 hunk byte for byte, and the later version is schema 5's `text_after`, for 7 of 7 and 44 of 44 hunks.
- `uv run python scripts/2026-11-02_exp2_engine_probe.py check` on both roots: all 8 prompts (2 roots, 2 record arms, `v3` and `v3-ctx3`) pass `trajlab.exp2.leaks` against the task's tests, each of the 7 queries a record-engine prompt gives runs through the query tool on its chain store, and every file a record-files prompt names is in its bundle.
  Every chain-store hunk of `v3` holds one `@@` header; `v3-ctx3`'s descriptions name no whole file.

### Validation (2026-11-05): the comparison

- Item 6 of this amendment ran on 2026-11-05 from 02:39Z to 05:53Z (UTC) on the development Mac: 32 attempts, 32 finished, no infra error, every hook request acked, $40.05 API-equivalent (`docs/experiments/exp2/2026-11-02_engine-uptake-probe.md`, "Whole-file hunks", Results).
  These 32 attempts ran on an API key and without ADR-0005's output cap, because the worktree they ran from had no `.env` and `trajlab run` did not check; both conditions alike, so the comparison stands (run note, Results).
- Outcomes: on `mvcc-lsm-compaction`, whole-file hunks passed 4 of 10 and 3-line hunks 3 of 10 (record-engine 3 of 5 against 2 of 5, record-files 1 of 5 against 1 of 5; Fisher exact, two-sided, p = 1.0 each); on `production-planning`, 0 of 6 and 0 of 6.
  The control arm on the same mvcc root passed 3 of 10 the day before (same run note, "Control on mvcc").
- Uptake: every `v3` record-engine agent on mvcc read the `hunk` column with a query of its own and so saw both changed files whole (5 of 5); one ran question 3's diffs query.
  One `v3` record-files agent of 5 opened a `.diff`; the others read the root's calls in `steps.jsonl`.
  On 2026-11-03, 1 attempt of 10 opened a whole file.
  So item 1 works for record-engine, where the hunk is what agents already read, and not yet for record-files, whose agents read the calls.
- Record-files agents wrote the diagnosis in 2 of 10 mvcc attempts under this text, against 9 of 10 under the text of 2026-11-03 (Fisher exact, p = 0.006); record-engine agents in 7 of 10, against 3 of 10.
  Which change of step 1 or question 3 does this is not known, and it is to be settled before a preregistration.
- Neither root tests whether whole files help: mvcc's two changed files are short and their originals are in the retry's environment, and production-planning's retries fail on a conclusion no file shows.
  Roots that rewrote large files are to be picked on the capture host (`scripts/2026-11-04_large_rewrite_roots.py`).

### Validation (2026-11-05): item 7 and the wider run

- `make lint` passes; the prompt tests (19) and CLI tests (40, with `trajlab run`'s new refusal of a run without a credential or ADR-0005's output cap) pass; the probe's `check` passes `v4` on the three mvcc roots and the vigenere root.
- A check before the run, 5 record-files attempts on mvcc: the `.diff` files were opened (2 of 5 saw a hunk; others filtered the files to changed lines), and none wrote the diagnosis.
- The wider run (run note, "Wider run"): on mvcc, 20 attempts an arm, all on the subscription and the cap, control passed 2, record-engine 6, record-files 6 (Fisher exact, each record arm against control, p = 0.235; the three arms at once, p = 0.256).
  Record-engine agents saw whole-file hunks in 18 of 20 attempts, record-files agents in 10 of 20.
  The diagnosis was written by 20 of 20 control, 2 of 20 record-engine, and 7 of 20 record-files agents: item 7 did not restore it, and with it record-engine wrote it less often than under the text before (7 of 10, p = 0.002).
  The difference from control, whose step asks the same, says that agents with a record treat the diagnosis as optional; settling it is open (a required first file write, or dropping the diagnosis from the record arms' steps and from control's, are the two ways to make the arms alike).
- New tasks: cad-model (3 roots) and layout-config-recreation2 (1 root) write 1.5 to 4 GB into each checkpoint, so retries on them need pruning of never-ingested probe images or an acknowledge-only watcher mode, both outside this ADR; interleaved-vigenere ended in an API safety refusal in 2 of its 6 sessions, and was not run further.
