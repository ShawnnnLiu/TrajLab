# ADR-0014: The Exp 2 store: steps joined to filesystem deltas, in DuckDB

Status: proposed (2026-10-18).
Amends CLAUDE.md constraints 2 and 6 and "What not to do" (no database) for this store only.

## Context

Experiment 2 replaces Experiment 1 as the paper's experiment (`docs/experiments/exp2/`: motivation, design spec, and technical spec, all 2026-10-18).
It gives a repair agent the record of a task's prior attempts, either as a queryable store (arm `record-engine`) or as the same record exported to files (arm `record-files`), against a control with no record.
The record joins each step of an attempt to the filesystem change the step made; ground truth that scores the experiment is held out from both record arms.
Build-order step 1 of the technical spec (§7) is the store's schema and the reconciliation that turns checkpoint images into per-step deltas.

CLAUDE.md constraint 6 forbids a `store/` directory, constraint 2 lets analysis code import only `contracts/` from capture, and "What not to do" forbids a database; all three predate the analysis phase.
ADR-0013 (verifier-replay ground truth) exists only on the unmerged branch `sl/groundtruth-replay`, so this ADR takes the next free number, 0014.

The technical spec leaves seven questions to ADRs (§8).
This ADR settles three of them: the diff method and text rule (question 1), how held-out tables are kept from agents (question 4), and the path exclusion list (question 5).
It also settles choices the spec's schema leaves open: what a step row is, what `trial_id` means, the shape of the held-out ground truth, and when a checkpoint chain is complete.
Questions 2, 3, 6, and 7 stay open (last section).

## Decision

### 1. Scope and placement

- The store is `src/trajlab/store/`, on the analysis side: it imports `trajlab.contracts`, `trajlab.atif`, Harbor, and DuckDB, never `capture` or `checkpoint`.
  Its row models are in `contracts/store.py`, one model per table, fields in column order; a test keeps the models and the DDL (`store/schema.py`) equal.
- The engine is DuckDB, in-process (`duckdb` 1.5.6).
- Step 1 adds no query tool, no export, and no derived-column implementation; those are steps 3 and 4.

### 2. A store is a directory of two databases

```
<store>/record.duckdb    trials, steps, deltas, store_meta: what an agent may read
<store>/heldout.duckdb   checks, faults, blame, chains, store_meta: never shown to an agent
<store>/blobs/<sha256>   later versions of changed rows without a hunk, named by deltas.blob_ref
```

- **Held-out tables live in their own file** (question 4: a separate file, not a restricted schema in one file).
  The `record-files` export is made from `record.duckdb` only.
  The agent's query connection is `store.schema.connect_agent`: `record.duckdb`, read-only, with DuckDB's `enable_external_access` off and the configuration locked.
  The separate file alone is not enough: a default DuckDB connection can read any file the process can, so one `SELECT` on `read_blob('<store>/heldout.duckdb')` would return the held-out rows, failing check names included.
  With external access off, `read_blob`, `glob`, `read_text`, `read_csv`, `ATTACH`, `COPY`, and extension installs all fail, and no query can turn access back on; a test checks each.
  Agent connections to one store in one process share a DuckDB instance: the first sets its time zone to UTC and locks it, and later ones find both done, so several can be open at once.
  The lock has one gap: `enable_logging()` still runs, and file logging to a path it cannot write makes every later query on the instance fail and closing it abort the process.
  It reads no file, so nothing held out leaks, but step 4's query runner must run each agent query in a disposable child process and refuse statements that name the logging functions (open question 2).
- Both files carry `store_meta`: `SCHEMA_VERSION`, the exclusion list, the text rule, and the reconcile method.
  `create_store` refuses an existing store built under different values, so rows built under two rules never mix.
  A shape change bumps `SCHEMA_VERSION` (now 1) and needs an ADR.

### 3. Keys

- `trial_id` is Harbor's trial id (the UUID in `result.json`), as the glossary defines it; `trial_name` is stored beside it.
  The store holds finished trials only, so the id always exists.
- `tool_call_id` is the join key to the trajectory, the checkpoints, and the ground truth, and is never re-derived.
- `attempt_idx` is the position in a retry chain: 0 for the corpus first attempt, then 1 to N, each retry naming the attempt it followed in `parent_trial_id`.

### 4. A step row is a tool call

- An ATIF step with k tool calls gives k rows, in call order, because checkpoints, deltas, and ground-truth blame are all per tool call.
- An ATIF step without tool calls gives one row (the agent's final message, a later user turn), and so does a compaction step.
- Each stop record (ADR-0011) gives a row with `tool` `Stop` or `StopFailure`, so changes a background process made after the last tool call have a step to belong to.
  The row goes where the stop happened: before the first row that started after its `requested_at`.
  Under a stop hook that turns a stop into another turn (the repair protocol's), a trial has several stops, and each sits between its turns rather than at the end, where postprocess puts stop checkpoints.
- The first user step is `trials.prompt`, not a row; a step's message and reasoning sit on its first row only, so no text is stored twice.
  `atif_step_id` maps a row back to `trajectory.json`.
- `checkpoint_seq` is the checkpoint holding the environment state right after the row: the call's own `CallRecord.checkpoint_seq` when it has one, otherwise the highest seen so far (ADR-0001's join for read-only steps; the highest, because parallel calls of one step can be answered out of order).
  It is null before the first checkpoint (the start image's state).
  It is also null after a hooked call that timed out, was deferred under every-N (ADR-0007), was stopped while it ran, or has no record at all, for that row and every later one, until a call's record names a checkpoint again: no checkpoint holds that state.
  A call whose changes a checkpoint already reached covers (in its `covered_tool_call_ids`, as when parallel calls are answered out of order) keeps that checkpoint instead.
- Rows come from the enriched trajectory (`trajlab postprocess`), which already carries every `CallRecord` and `CheckpointRecord` on the right step (ADR-0003).

### 5. Deltas come from checkpoint top layers (question 1)

**What a delta is.**
A `docker commit` image of a trial container is the trial's **start image** (the image its container started from: the derived image, ADR-0008, or for a resumed trial the checkpoint it resumed from) plus one layer: the container's whole writable layer at that moment.
So checkpoint k's top layer holds every change since the container started.
The environment at checkpoint k is the **view**: the start image's filesystem with top layer k applied by the OCI rules: `.wh.<name>` deletes a path and its subtree, `.wh..wh..opq` hides a directory's lower contents, and a member that is not a directory replaces the lower subtree at its path.
The **delta** of checkpoint k is the set of paths whose entries differ between view k-1 and view k, with view 0 the start image itself.
Entries are compared by kind, permission bits, owner, and the sha256 of a file's content or a symlink's target; times are never compared.
The delta's rows carry the `step_idx` of the step that owns the checkpoint (`CheckpointRecord.tool_call_id`).

**Alternatives considered.**
- *Export and diff whole filesystems* (`docker export` of a container from each checkpoint): exact, but every export is the whole root filesystem, several GB per checkpoint for the larger TB 4.0 images.
  It is kept as the independent oracle of the validation script.
- *The detector's paths* (`changed_paths` in `calls.jsonl`, contents by `docker cp`): cheap, but capped at 100 paths per call, absent for the baseline call, judged by change time rather than content, and it would make the record depend on the detector, whose soundness rests on 14 audited calls (`docs/paper-notes.md`, C3).
- *Top layers* (chosen): exact, independent of the detector, and each top layer holds only the trial's own changes (ADR-0008).

**Facts this rests on**, checked on 2026-10-18 with Docker 29.8 and the containerd image store (the capture server's storage driver family), on an Alpine container committed twice:
- a commit's top layer is cumulative: the second repeated every still-present entry and whiteout of the first;
- a copied-up file appears in the layer even with unchanged content (a rewrite with the same bytes), so presence in a layer is not a change;
- `rm -rf` of a start-image directory gives one whiteout and no listing of the subtree, so the start image must be read to enumerate what was deleted, and a file first modified at step k has its earlier version only in the start image;
- a removed and recreated directory gave one whiteout per removed child, no opaque marker; opaque markers are still handled, since other writers use them;
- a directory replaced by a file or symlink appears as that one member, with no whiteouts for its old children, so a non-directory member hides the lower subtree at its path;
- committing a container with no changes adds no layer;
- `docker image save` writes layer blobs first and `manifest.json` last, and the layer blobs are gzip, so a save must land in a file before it can be read.

**How it is read** (`store/docker_images.py`, `store/reconcile.py`).
One `docker image save` per trial, of all its checkpoint tags, writes the start image's layers once and every top layer.
Each top layer is then read twice (names, then content hashes) and the start image's layers once, restricted to the paths the top layers touch, delete, or replace; a start-image layer is read again when a hard link it keeps names a member it did not keep.
A layer is read a third time only when a row needs a version the second read did not keep (a non-text file over 1 MiB, a hard link to an unchanged member, or a hard link whose member is under an excluded path), found by size and then hash.
At most two top layers are in memory at a time; every distinct text version of a non-excluded file is written once to scratch space, by hash, so any earlier version is at hand for a hunk.
Every checkpoint image must be the start image plus at most one layer.
`trajlab.store.build.start_image` names the start image from `trajlab-resume.json` or `trajlab-preinstall.json`, and the Docker provider requires it: a checkpoint image over any other image is refused instead of diffed, and a commit that added no layer gets an empty top layer.
Without the start image, such a commit could not be told from one that added a layer, and the start image's own last layer would be read as the checkpoint's changes.
Saves go to a directory of the provider's own, removed with anything in it when the provider closes, including the temporary file the docker CLI leaves if it is killed.
Layers are gzip or plain tar, told apart by their first bytes; gzip is read with `gzip.GzipFile`, which reads every member of a multi-member stream (eStargz writes one per entry; Python's `tarfile` reader stops silently after the first).
zstd layers are refused with an error that names them.
`reconcile` also refuses records with gaps or repeats in their seqs, records of more than one trial, a checkpoint with no owning step or two sharing one, and an unreadable layer.

**Rows.**
One row per changed path, directories included (added, deleted, or mode or owner changed).
A file version is text if it is at most 1 MiB, contains no NUL byte, and decodes as UTF-8; a symlink target is text.
A version without content (absent, a directory, another kind) counts as text, so a row is text when each of its file or symlink versions is; `is_text` is null for a directory or other kind on both sides.
A text row whose content changed gets a unified diff (3 lines of context, lines split on `\n` only, `\ No newline at end of file` where needed, `/dev/null` for a side without content).
Lines are matched after trimming common leading and trailing lines, in three stages, so every hunk is a correct diff made in a few seconds at most:
- exactly, by difflib without autojunk, when the two sides' line counts multiply to at most 60,000 (its cost grows about as that product to the 1.5; 0.09 s at the cap on the worst shape measured);
- else by a shortest edit script (Myers) within 2,000,000 steps, about 0.5 s (500 scattered edits in 30,000 lines take 0.13 s);
- else, for a wholesale rewrite, by anchoring on lines that occur once on each side (patience diff), matching each gap between anchors exactly while the gaps' line products add up to at most 1,200,000 (about 2 s at worst), and as replacements after that.

So a hunk takes under about 3 s.
It is small, except for a file of few distinct lines (say, of 0s and 1s) edited in many scattered places: with no unique line to anchor on, it gets one hunk replacing the whole file, up to about 3 MB for a 1 MiB file.

Plain difflib is near cubic when lines repeat: 207 s for one inserted line in 1 MiB of repetitive text, and 167 s for a 1 MiB pretty-printed JSON whose every record's score changed.
With autojunk, the one-line edit became a 632 KB hunk.
The staged match gives a 63-character hunk for the first in 0.03 s, and for the second a hunk that changes exactly the 19,000 score lines, in 0.7 s.
The method, hunks included, is recorded in `store_meta` as `reconcile_method`, so stores built under different methods never mix.
A row whose content changed but which gets no hunk (a version that is not text) stores its later version once in `blobs/<sha256>`, named in `blob_ref`: a file's bytes, text or not, or a symlink's target.
So the content a step wrote is always stored, as a hunk or a blob: a UTF-16 file rewritten as UTF-8, or a 2 MiB log truncated to one line, keeps its new text.
A mode- or owner-only change stores nothing, as it gets no hunk either.
Earlier versions keep only their sha256 and size.
A path holding bytes that are not UTF-8 is stored with each such byte written `\xNN`, as are such bytes of a symlink target shown in a hunk; sha256 and size are of the raw bytes.
In hunk headers, a path with a newline, tab, double quote, backslash, or such a byte is C-quoted as git and GNU diff quote it (the byte as an octal escape), so the header stays one line and names the real file.
A row's content changes when its sha256 does; a file replaced by a symlink whose target is that file's bytes has a kind change and no content change, so no hunk and no blob.
There is no rename operation: a rename is a delete and an add, which a derived column can pair later by sha256.

### 6. Exclusions (question 5)

- The store excludes exactly the change detector's harness paths (ADR-0010, item 5, found from the top layers of real checkpoints), plus `/proc` and `/sys`: `/logs/**`, `/tmp/claude-<uid>/**`, `/tmp/claude-code-settings/**`, and per user `~/.claude/**`, `~/.claude.json`, `~/.cache/claude/**`, `~/.local/state/claude/**`.
  An added or deleted directory whose only changes underneath are excluded gives no row either, as in the detector.
  A call the detector judged unchanged therefore has no delta rows.
- `/tmp` as a whole is not excluded, though the technical spec suggests excluding "/tmp scratch": an agent's own scripts and outputs often live there, and only Claude Code's `/tmp/claude-<uid>` is the harness's.
- `__pycache__` files stay: they are effects of the agent running Python (`docs/paper-notes.md`, threats).
- The list is one file, `store/exclusions.py`, recorded in `store_meta`; a test keeps it a superset of the detector's.

### 7. Ground truth is held out per fault

ADR-0013's ground truth is a set of items per failed trial, each with one or more failing checks, zero to 17 blamed calls, alternative confirmed fixes with their own blamed calls, and related calls that are neutral to cite.
The technical spec's single `checks` table, with one blamed step per check, cannot hold that.
So the held-out side has three tables:
- `checks` (trial, check, passed, fault): every verifier check and the fault a failing one belongs to;
- `faults` (trial, fault, kind, method, flags): one row per ground-truth item;
- `blame` (trial, fault, role, fix, call, step): the calls an item names, with role `blamed`, `alternative` (grouped by fix), or `related`.
They are loaded from each trial's `groundtruth/items.jsonl` once `sl/groundtruth-replay` is merged (`store/checks.py`, a later step); the spec's `blamed_step_idx` is `blame.step_idx` with role `blamed`.

### 8. When a checkpoint chain is complete

A trial's chain is **complete** when every hooked call that ran has a `CallRecord` and every checkpoint's image was read.
Hooked calls fall into five kinds, reported separately in its `ChainReport`:
- *answered*: a `CallRecord` exists;
- *timed out*: the hook gave up (`.timeout`); the chain is incomplete;
- *interrupted*: the call has no result at all, because the agent was stopped while it ran (the time cap); what it changed may be in no checkpoint, so the chain is incomplete, with its own reason so it is not mistaken for a capture fault;
- *rejected*: Claude Code refused the call before running it, and its result is a `<tool_use_error>` (Edit's "String to replace not found"); no hook fires and nothing changes, so it does not count against completeness;
- *unanswered*: anything else, a capture fault; the chain is incomplete.

Rejected calls are real: both failed trials of `tb21-haiku-v1` would otherwise be incomplete, each for one refused Edit.
Interrupted calls are expected wherever the time cap ends a trial mid-call, as in 2 of 2 local time-capped trials (`tb40-sonnet-v1`, `tb40-repair-v1`); whether Exp 2's sample keeps such failures is decided at step 2, where they are counted apart from capture faults.
`final_state_recorded` says whether the trial's last step row is a stop record's, so a record names the state the agent left; it is reported but not part of completeness, since a trial stopped at its time cap has no final stop.
Each `ChainReport`, with one reason per problem, is written to `heldout.duckdb` (`chains`).
Incomplete chains are built and flagged; `exp2 run` will refuse them.

### 9. Derived columns

`is_loop`, `is_revert`, and `files_touched` are columns of `steps` from schema version 1, null until step 3 fills them, so step 3 does not change the schema.
Each column is an interface: `DerivedSpec` names it, and the implementation is a registered, versioned function, so a later LLM implementation of the same name changes the recorded implementation, not the schema.

## Open questions, decided in later steps

1. **Question 2, at step 3:** the `args_json` normalization for `is_loop`, and whether derived columns are recomputed or appended when a retry is ingested.
   As the design spec defines it ("same tool and args as an earlier step, empty delta"), every repeated read-only call is a loop; step 3 should say whether that is intended.
2. **Question 3, at step 4:** the query tool's channel.
   The recommendation stands: a host process answering `.sql` files through the bind-mounted `/logs/agent`, separate from the checkpoint watcher, so a slow query never delays an ack the agent is waiting on, and every query is logged on the host by construction.
   It must open the store with `connect_agent`, run each query in a disposable child process (which also gives the statement timeout), and refuse statements that name DuckDB's logging functions (item 2).
3. **Question 6, the owner's call:** a fresh environment for every retry in every arm (recommended in the design spec).
4. **Question 7, at step 3:** derived columns in the `record-files` bundle (recommended: yes, so the two record arms differ only in access).

## Consequences

- Deltas are what Docker's own view of each checkpoint says changed, independent of the change detector; a disagreement between a `CallRecord`'s verdict and a delta becomes observable.
- Every trial's save includes its start image's layers (0.04 to 3.6 GB compressed for ADR-0012's tasks).
  Step 2 measures the per-trial time on the capture server and writes it to `docs/experiments/exp2/sample.md`.
  In the retry loop each retry is saved again; if that misses the spec's one-minute ingest target, a later ADR can record each checkpoint's changes at capture time instead, which changes what a trial records and needs a new `corpus_id`.
- The record is the filesystem only (ADR-0004): processes, shell state, and network are not in it; the paper says so in its first paragraph.
- A package install gives one row, and for text one hunk, per installed file; step 2 reports store size per trial.
- The first checkpoint's delta includes harness writes outside the exclusion list, such as Harbor's mount points; ADR-0013 flags blame on it as `baseline_checkpoint`.
- `store_meta` is in `record.duckdb` and so visible to an agent; the `record-files` README will state the same values.
- Rows are loaded in bulk (newline-delimited JSON read by DuckDB with the table's own column types): 20,000 delta rows load in under a second, where `executemany` managed about 600 rows a second.
  A string with a lone surrogate, which DuckDB refuses, is stored with it written `\uXXXX`.
- A store's database files are created under a temporary name and renamed, so a build killed while creating them leaves no file without its tables; an existing file without readable `store_meta` is refused with a message to delete it, and a `.wal` left beside a deleted database file is refused rather than replayed into the new one.
- A trial built without images (steps only) is rebuilt by the first build that has them; its old rows are deleted first, in their own transaction, so a stop at any point leaves the trial whole or absent.
- Tests run offline on synthetic layers built to the observed format, including a differential test against an independent filesystem simulator; the check against real Docker is a dated script run by hand.

## Validation (2026-10-18)

**Against Docker, with an independent oracle.**
`scripts/2026-10-18_reconcile_docker_check.py` runs an Alpine 3.20 container on the development Mac (Docker 29.8, containerd store).
It applies 22 scripted steps and commits after each exactly as the watcher does (`DockerCommitBackend.snapshot`: repository `trajlab-checkpoint`, the watcher's tag and labels).
It then compares the rows of `DockerImageProvider` plus `reconcile` with an oracle that shares no code with them: `docker export` of a container created from the start image and from each checkpoint image, parsed as whole filesystems and compared consecutively.
Only the exclusion predicate is shared, since it is the contract.
Besides the ten identity fields of each row, it checks `is_text` against the text rule, file sizes, every blob's content, and that each hunk turns the earlier text into the later one.
It also checks that computing a checkpoint from layers k-1 and k alone gives the same rows as the whole chain.
The steps cover: text and binary adds; appends; deletes of start-image files and of directories with children; a directory removed and recreated; chmod (setuid included, and a binary whose content did not change), chown, and `chown -h` on a symlink; same-byte rewrites and `touch` (no rows); symlink create and retarget; hard links; text over 1 MiB; a FIFO; empty directories; CRLF and missing final newlines; text turning binary and binary turning text; file to directory, directory to file, symlink to directory, directory to symlink, and binary file to symlink; harness paths, and a directory holding only them; and a bulk step that adds 2,000 files and deletes a directory of many.

The first run failed on one step: a directory replaced by a file or symlink kept its old children in the view, because Docker writes no whiteouts for them.
The view rule now treats a non-directory member as replacing the lower subtree, the differential test's simulator now emits that encoding, and the run passes:

| Step | Rows (oracle = reconcile) | Step | Rows |
|---|---:|---|---:|
| add-text | 6 | mkdir-and-temp-file | 3 |
| append-base | 1 | rmdir-and-rm-temp-file | 2 |
| rm-base-file | 1 | excluded-only | 0 |
| rm-rf-base-dirs | 14 | cache-sibling | 1 |
| replace-base-dir | 47 | text-edits | 7 |
| chmod-only | 3 | kind-changes | 14 |
| chown-only | 3 | rm-cache-dir | 2 |
| same-bytes-and-touch | 0 | bulk | 2,225 |
| symlink-create, -retarget | 1, 1 | after-bulk | 55 |
| hard-links | 2 | binary | 2 |
| big-text | 2 | | |

Whole chain: 22 checkpoints, 2,392 rows, 0 mismatches.
Times on Alpine: the provider (45 inspects, one 4.4 MB save, the manifest) took 1.4 s, and reconcile took 0.9 s for the chain.
Per checkpoint, reconcile took 0.03 to 0.05 s for small steps and 0.7 s for the bulk step and the one after it.
These are not the corpus's times: the derived images there are 1 to 10 GB; step 2 measures them on the capture server.
Every container and image the script made was removed afterwards.

**Offline tests** (`make test`: 704 passing, 300 of them the store's; the store's also pass with `TZ=Pacific/Kiritimati LANG=C`):
- scenario tests for each layer encoding and row rule, including a reproduction of the two layers the Docker probe saw;
- a differential test over 320 seeded sequences of a filesystem simulator that writes cumulative containerd-shaped layers (explicit whiteouts, opaque markers, a whiteout beside a recreated directory, a non-directory over a subtree, hard links, `./` and trailing `/` names), with expected rows from comparing consecutive simulated filesystems directly, every hunk applied by a standalone patcher, and the blob directory holding exactly the blobs some row names;
- the step-1 acceptance check: the hello-world fixture builds to its hand count (1 delta row, 3 step rows, a complete chain), the second build is skipped, and a build that fails while writing any table leaves the trial buildable;
- with non-directory hiding disabled, the differential test fails in 8 of its 9 parts.

**Adversarial review.**
Five independent reviewers (layer semantics, rows and build, the Docker provider at corpus scale, conformance to this ADR and the specs, and mutation testing: 197 planted bugs, of which 151 failed a test and 16 of the 46 others were real) reported 33 findings, each with a reproduction; a second reviewer per lens tried to refute each, and none was refuted.
All are addressed in this ADR and the code:
- *correctness:* text written over a non-text version was stored nowhere (now a blob); a call Claude Code rejected counted as a missing record (now rejected, item 8); a call killed by the time cap read as a capture fault (now interrupted); `final_state_recorded` was true after any stop (now: the last row is a stop); rows after a timed-out or deferred call named a checkpoint that does not hold their state (now null); a hard-link chain in the start image crashed the filtered read; a multi-member gzip layer was read only to its first member, silently;
- *the held-out guarantee:* a default DuckDB connection to `record.duckdb` could read `heldout.duckdb` with `read_blob` (now `connect_agent`, item 2);
- *bounds:* hunks could take minutes (now staged, item 5); `executemany` loaded about 600 rows a second (now bulk); a commit that added no layer could be misread without the start image (now required);
- *robustness:* a build killed while creating the store wedged it; a trial built without images could never get its deltas; the docker CLI's temporary file leaked when a save was killed;
- *docs:* terminology against the glossary ("base image", the top layer of a no-change commit, unqualified "record"), CLAUDE.md constraint 2, field descriptions, counts in this section;
- *tests:* each surviving mutant now fails a test (write atomicity, duplicate checkpoint seqs, 64-bit size columns, composite keys, file-type bits in tar modes, unreferenced blobs, the 1 MiB text limit, `.wh.` inside a name, records of two trials, non-UTF-8 symlink targets).

A second review of the fixes (two lenses, each with a refuting verifier) reported 20 findings, of which none was refuted; all are addressed above: the logging gap in the agent connection's lock (documented, for step 4), a second agent connection failing while one was open, a stale `.wal` replayed into a new store, rows after a call without a record naming a checkpoint, deferred calls already covered, hunk time bounds stated too low, paths with a newline breaking hunk headers, a file-to-symlink change with equal bytes, a single short read misjudging a gzip layer, and docs.

**Known limits.**
zstd-compressed layers are refused; Docker's default is gzip.
A layer with two members for one path (Docker writes none) can resolve a hard link through the wrong one when the start image is read filtered, and can leave a blob no row names.
`start_image` names a start image only from `trajlab-resume.json` or `trajlab-preinstall.json`; a trial run without pre-install (its start image is its task image) needs the image passed to the provider by hand.
`other` entries (FIFOs, devices) compare by kind, mode, and owner only, not device numbers.
A path's bytes that are not UTF-8 are stored escaped (`\xNN`), so two paths differing only in how such bytes are spelled would collide; no such path is known in the corpus.
