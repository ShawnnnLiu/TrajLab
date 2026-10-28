# ADR-0022: The Exp 2 retry loop as built: where it lives, what each arm is told, and the chain store

Status: proposed (2026-10-28).
Amends CLAUDE.md constraint 6 and its layout, ADR-0016 item 1 (the evidence sentences), and the Exp 2 design spec §2 and §10 (the stop hook); settles ADR-0018 item 6.

## Context

Build-order step 5 of the technical spec (§7) is the retry loop, the three arms and their mounts, and `trajlab exp2 prereg`, done when a dry run of 3 failures × 3 arms × N = 1 runs with every prompt grep-clean.
ADR-0016 settled the retry prompt, N = 3, and what a chain's store holds; the owner settled a fresh environment and a fresh session for every retry on 2026-10-19.
Four things the specs leave open, or say in ways the repo cannot follow, were put to the owner on 2026-10-28 before the code they affect was written:

1. **Where the loop lives.** The technical spec (§2) puts it in `src/trajlab/repair/`, which CLAUDE.md's layout does not list, and constraint 6 forbids new analysis modules.
   The loop runs Harbor trials (capture), postprocesses them (atif), and builds, exports, and serves the store (store), which constraints 3 and 6 keep apart.
2. **What the record arms are told about the schema.** ADR-0016 item 1 gives `record-engine` "`Tables: {DESCRIBE output}. Example rows: {one per table}.`"; ADR-0018 item 6 left to the owner whether both arms get that or the schema text (`trajlab.store.describe.describe`), which is the bundle's README.
   Measured on `tb40-sonnet-v4`'s store: the schema text is 14,007 bytes, and one `deltas` row is up to 1,193,998 bytes as JSON, above the 131,072-byte cap Harbor's prompt is under (`docs/harbor-facts.md`); example rows would also differ by chain and by retry, so no prompt hash could be fixed in advance.
   ADR-0016's `record-files` sentence names `trials.jsonl`, `steps.jsonl`, and `deltas/`; the bundle holds 12 relation files besides `deltas/` and the README (ADR-0018 item 5).
3. **The stop hook.** The design spec applies "the v2 §2 stop hook" in every arm and lists "Stop hook gated on `TRAJLAB_REPAIR=1`, same as v2" in its checklist.
   Its source, `claude/exp1_repair_protocol_v2_spec_2026-10-02.md`, is not in the repo, on any branch, or on the capture host, and round 2 (`sl/repair-v3-round2`) ran with the protocol prompt and no stop hook; ADR-0015 §4 describes the mechanism (block a stop once) but no hook was ever built for repairs.
4. **Disk.** Every attempt is captured as the corpus was (watcher, write recorder, checkpoints).
   `tb40-sonnet-v4`'s 1,098 checkpoint images take about 222 GB (120.1 GB of layer bytes in its records, times the 1.85 measured on 2026-10-27; median 0.05 GB per trial, largest 22.9 GB); up to 504 attempts at that rate is about 1.6 TB, against 641 GB free.
   The owner's standing rule is never to delete checkpoint images.

## Decision

0. **Scope.** Exp 2's retry loop: `src/trajlab/exp2/`, `trajlab.contracts.exp2`, and the commands `trajlab exp2 prereg | check | run`.
   Capture is unchanged: the hook, settings file, watcher, recorder, backend, and postprocess are those of `tb40-sonnet-v4`, and each attempt is a new corpus id (its job name).

1. **The loop lives in `src/trajlab/exp2/`, an orchestration package at `cli.py`'s level** (owner, 2026-10-28).
   It may import `contracts`, `capture`, `checkpoint`, `atif`, `store`, and Harbor; nothing in `src/` but `cli.py` imports it.
   So capture still never imports analysis, the store still imports only `contracts`, `atif`, Harbor, and DuckDB, and the one place that joins them is the one that must.
   The technical spec's modules map onto it as `loop.py` (the state machine), `arms.py` (the arms, an attempt's job config, the bundle mount, and the per-attempt query server: its `arms.py` and `mount.py`), `prompt.py` and `leaks.py` (the prompt and its grep), `chain.py` (a chain's directory, logs, and store), and `prereg.py`; the query tool's container side is ADR-0019's `store/query.sh`.
   The agent is unchanged: `trajlab.capture.repair:RepairClaudeCode`, with the retry prompt's prefix as its `repair_note` kwarg; round 2's `REPAIR_PROTOCOL` and `DIAGNOSIS_PATH` move from `sl/repair-v3-round2` into `capture/repair.py` verbatim (ADR-0016, Consequences).

2. **Both record arms are told the same about the record: the schema text, no example rows** (owner, 2026-10-28; amends ADR-0016 item 1, settles ADR-0018 item 6).
   The prompt is still the note, the arm's evidence sentence (none in `control`), the four protocol steps, and the task instruction, apart by blank lines:
   - the note, unchanged: `{k} previous attempt(s) at this task FAILED the task's tests. The tests are not visible to you and may check cases beyond the examples in the environment.`;
   - `record-files`: `The record of those attempts is under /logs/agent/record/: trials.jsonl, steps.jsonl, deltas.jsonl, job_notices.jsonl, writes.jsonl, step_flags.jsonl, delta_flags.jsonl, path_versions.jsonl, trial_summary.jsonl, step_jobs.jsonl, delta_attribution.jsonl, store_meta.jsonl, and deltas/ (one diff per changed file per step). README.md lists the fields.`, the file list rendered from `trajlab.store.describe.relations`, which is the bundle's layout; the README is a layout paragraph and then the schema text;
   - `record-engine`: `The record of those attempts is a read-only SQL database. Run a query with: /logs/agent/bin/query "SELECT ...". It prints the rows as JSON lines. Tables:`, a blank line, and the schema text; "JSON lines" is what ADR-0019 item 9 requires the sentence to name;
   - the four steps: `REPAIR_PROTOCOL` from its "Work in this order:" line on, its first paragraph (round 1's note, which the new note replaces) dropped and ", and your earlier work on this task if you can see it" removed (`trajlab.exp2.prompt.PROTOCOL_STEPS`).

   The prefix (everything before the instruction) therefore depends only on the arm and k, since the schema text is the same for every store of one schema and derived id; the preregistration fixes the sha256 of each of the arms × N prefixes, and the instruction is the root failure's own `trials.prompt`, whose sha256 it lists per failure.
   On the 56 usable failures the whole prompt is 1,041 to 3,632 bytes in `control`, 351 bytes more in `record-files`, and 14,163 bytes more in `record-engine` (largest 17,795); the loop refuses one over 130,048 bytes.
   After each attempt the loop checks that the trajectory's first user step equals the rendered prompt (`AttemptResult.prompt_delivered`).

3. **No prompt names a check or quotes the verifier** (technical spec §9), checked when the preregistration is written, by `trajlab exp2 check`, and again before each launch (`trajlab.exp2.leaks`).
   The needles come from the task's own tests in Harbor's package cache (found from the root failure's `lock.json`): every `def test...` and `class Test...`; and from the root failure's verifier: every test name in `ctrf.json` (after `::`, without `[params]`), every line of `test-stdout.txt` and `test-stderr.txt`, and every string in the verifier's JSON files, each line at least 24 characters once stripped.
   Check names match as whole identifiers, verifier text as substrings, and a needle the task instruction holds itself is not a leak.
   A task scored by a grader rather than tests (the 9 freecad roots) has no check names, and its needles are its verifier text; a task whose tests cannot be read at all is refused rather than passed as clean.
   On `tb40-sonnet-v4`: 47 of the 56 roots have check names, and all 504 prompts (56 roots × 3 arms × k = 1 to 3) are clean.

4. **No stop hook in any arm** (owner, 2026-10-28; amends the design spec §2 and §10).
   Every attempt runs `configs/claude-code/settings.hooks.json` as the corpus did, whose `Stop` entry is the checkpoint hook (ADR-0011) and never blocks; nothing is gated on `TRAJLAB_REPAIR`.
   The preregistration records `stop_hook: null` and the settings file's sha256.

5. **An attempt is Exp 1's `fresh` repair job with the arm's note and mount.**
   - `trajlab.capture.repair_launcher.repair_job_config(arm="fresh")` gives the corpus job's model, agent kwargs (medium effort, Claude Code 2.1.278), hooks, and timeout multipliers, the pre-installed task image, one trial, and no loaded session; the loop adds `repair_note` and, for `record-files`, the mount.
   - It runs as `trajlab run <config> --corpus-id <job>`, so it gets the pin, watcher check, manifest, and repo-state check of any corpus run; the loop commits new manifests before each launch, as Exp 1's launcher does.
   - Job names are `<run>-<root trial>-<arm>-a<t>`, and `-r<n>` for the n-th rerun; the config, the rendered prompt, the launch record (`AttemptLaunch`), and the run log are in `<jobs dir>/_exp2-inputs/<job>/`.
   - `record-files`: the bundle the attempt sees, `bundles/attempt-<t>/` in the chain dir, is a read-only bind mount at `/logs/agent/record` through Harbor's `environment.mounts`, so the agent cannot change it and no Harbor code is patched.
     Harbor makes no attempt to empty `/logs/agent` on a single-step trial; its `chown -R` of `/logs/agent` at the end logs an error for each file of the mount and goes on (`service_exec` does not raise), which the dry run is to confirm.
   - `record-engine`: a `trajlab.store.serve.Server` runs in a thread of the loop for the attempt's duration, as ADR-0019 has it.
     `trajlab run` refuses an existing job dir, so the thread waits for Harbor to make it and serves from then; Harbor makes the trial's `agent/` before the container starts, and the loop records whether `bin/query` was in place before the agent's first step (`query_tool_ready`) and the query log's length.
     The server is stopped before the attempt is ingested.
   - An attempt counts when the verifier scored it or the agent itself failed (`ended_turn`, `timeout`, `agent_error`: ADR-0012 item 4).
     One the harness failed (`infra`) or that left no `result.json` (`lost`) is not an attempt: it runs again under `-r<n>`, its job dir kept, and three such tries stop the chain; an `unclassified` exception stops it until a person decides (`--rerun-unclassified`), as Exp 1 has a person read those.
     A usage limit pauses launches for 30 minutes.
   - The loop holds every launch while the watcher or the write recorder is not running, while the jobs disk has under 50 GB free, and while the tree has changes `trajlab run` would refuse; `exp2 run` refuses to start without both capture processes.

6. **A chain's store is seeded by copying, and grows by building** (ADR-0016 item 3).
   - Attempt 0's rows are copied from the corpus store, not rebuilt: every row of the root failure in `trials`, `steps`, `deltas`, `job_notices`, and `writes`, its `chains` row, and the blobs its deltas name (hard links where the file system allows).
     The corpus store must have been built under this code's `store_meta`, so the retries' rows beside them are built by the same rules; seeding needs no image and takes seconds.
   - A failed retry is postprocessed and built in with `trajlab.store.build.build_trial(attempt_idx=t, parent_trial_id=...)`, its images read through the Docker provider; for `record-files` the bundle the next attempt sees is exported and verified.
     The derived views cover the new rows at once, so nothing is recomputed (ADR-0018 item 1).
   - Before each launch the store must hold exactly attempts 0 to t-1, each naming the one before; `AttemptResult.store_trials` records them, and the model refuses any other count.
   - The last attempt (t = N) and a passing one are never ingested: no attempt follows them.
   - An ingest that fails stops its chain (technical spec §5.2); the store is left whole without the attempt, and `--retry-failed-ingests` tries it once more.
     A retry whose own checkpoint chain is incomplete (ADR-0014 item 8) is built and flagged in its `IngestResult`, not a stop, so the record arms stay paired with `control`; **proposed, the owner's call**.
   - The technical spec's one-minute ingest target is not a gate: an ingest delays only its own chain, runs in a worker thread, and its time is recorded per attempt (`IngestResult.seconds`).
     `sample.md` measured whole-trial builds of 14.6 to 535.7 s, 22 of 69 over 60 s; the dry run at N = 1 ingests nothing, so retry ingest times come from the first run at N > 1.

7. **Disk: every image is kept for now** (owner, 2026-10-28).
   `trajlab exp2 run --prune-ingested` removes a failed retry's checkpoint images once its rows are in the chain store and its bundle verifies, listing them in the trial's `agent/checkpoints/pruned.json` as Exp 1's prune does (records stay, `kept_seq` null); it is off unless passed, never touches a root failure's images, and makes ADR-0013's ground truth impossible for that retry.
   Whether the full run uses it is decided before the full run, from the dry run's sizes.

8. **The preregistration is the run's configuration** (technical spec §5.3).
   `trajlab exp2 prereg` writes Markdown with one JSON block, `Exp2Settings`: the sample (named trials, or every usable failure with each exclusion's reason), N, the arms, the primary comparison, the test (a required option), the schema version, derived id, and view names, the protocol's and every prefix's sha256, the hooks file's sha256, the stop hook, the query tool's caps (200 rows, 10 s, 512 MB, 2 threads), the model and its kwargs, and the repo sha.
   It refuses to write a file if any prompt leaks.
   `trajlab exp2 run FILE` runs exactly that file's settings; it refuses when the code would render a different prefix, protocol, or view than the file fixes, or when a root failure's instruction changed (`drift`), and it refuses `record-engine` unless the file on disk is the version committed at HEAD, whose commit every `record-engine` `AttemptResult` records.
   A dry run has its own file (`--dry-run --out docs/experiments/exp2/<run>.preregistration.md`), so the full run's `preregistration.md` is not written until its sample and test are fixed.

9. **Contracts.** `trajlab.contracts.exp2` holds `ArmConfig`, `AttemptResult`, `AttemptLaunch`, `IngestResult`, `RootFailure`, `Exclusion`, and `Exp2Settings`; `QueryLogEntry` stays in `contracts.store`, where ADR-0019 put it, and is named in `contracts.exp2` too.
   A chain is `<root>/<run>/<root trial>/<arm>/` (default root `corpus/exp2/`, git-ignored) with `attempts.jsonl`, `ingests.jsonl`, `store/`, and `bundles/`; the loop resumes from the two logs, and an attempt still running when the loop restarts is adopted from its launch record.

## Open, for the owner

- One failure per task or all 56 (`sample.md`); `exp2 prereg` takes named trials or every usable failure, and a per-task draw would be added with its rule.
- The primary comparison's test (McNemar, or a mixed logistic model with a task random effect; design spec §6), given to `exp2 prereg --test`.
- `--prune-ingested` for the full run (item 7), and whether a retry with an incomplete checkpoint chain stops its chain (item 6).

## Consequences

- CLAUDE.md lists `src/trajlab/exp2/` and its import rule; the glossary gains chain store, rerun, and preregistration.
- The design spec's stop hook and its checklist item are struck, with this ADR named; ADR-0016 and ADR-0018 carry amendments pointing here.
- Each attempt adds one manifest under `corpus/manifests/` and one commit of new manifests on the branch the loop runs from, as Exp 1's launcher did.
- The record-engine arm's prompt is 14 KB longer than the others, all of it the schema text that the `record-files` agent reads in its README: the arms carry the same information and differ in how the agent gets it, which is the comparison.

## Validation (2026-10-28)

- `make test`: 1352 passed; `make lint` clean.
  The new tests are offline: `tests/test_exp2_prompt.py` (the three arms of one chain differ only in the evidence sentence, each says `FAILED`, none names a check of the task's tests or quotes the verifier, and `RepairClaudeCode` pipes exactly the rendered prompt), `test_exp2_chain.py`, `test_exp2_arms.py`, `test_exp2_loop.py` (the runner against a fake `trajlab run`: budget, pass, reruns, a usage limit, a failed ingest, a leaking prompt, a prefix not preregistered, a restart mid-attempt), `test_exp2_prereg.py`, and `test_contracts_exp2.py`.
- On `tb40-sonnet-v4`'s store: 56 usable failures, none excluded; no drift; all 504 prompts clean, checked in 1.3 s.
- The dry run (3 failures × 3 arms × N = 1) is not yet run; it needs the owner's go-ahead (Claude subscription and Docker), and its commands and outcomes are added here.
