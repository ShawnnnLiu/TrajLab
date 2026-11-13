# ADR-0016: The Exp 2 retry prompt, budget, chain membership, and the failure-timeline rule set

Status: proposed (2026-10-25).

## Context

On 2026-10-25 the owner read "Failure as a Process: An Anatomy of CLI Coding Agent Trajectories" (Zhao, Li, Li, Zhao, Barr, Sarro, Ye; arXiv 2607.09510, July 2026) against the Exp 2 specs and asked how it should shape the query engine's rules, what the retry prompt is, what the retry budget N is, which failed attempts a chain's store holds, and whether labeling the paper's three timestamps on our failures needs an LLM judge.
This ADR records the answers.
It settles the retry prompt and N (technical spec §5.1; design spec §4, marked **[decide]**), the membership of a chain's store (technical spec §3.1 and §9; design spec §5), the shape of the derived rule set that step 3 freezes (technical spec §3.3 and §8 questions 2 and 7; ADR-0014 "Open questions", item 1), and how the three timestamps are produced on the held-out side (ADR-0013 §4 to §7).
The exact predicates and thresholds of the rules are not settled here; they are frozen in the preregistration (technical spec §5.3), and this ADR is amended with them before the full run.

### What the paper contains

- 1,794 hand-annotated Terminal-Bench trajectories over 89 tasks (so Terminal-Bench 2.0), from Terminus2, OpenHands, and MiniSWE under seven models; 1,184 are failures.
  No environment state was captured, and Claude Code is not among the scaffolds.
  The dataset link in the paper (`github.com/xz-Sean/cli_trajectory_analysis`) returned 404 on 2026-10-25.
- Three annotated steps per failure: `t_err`, the decisive error (median step 7); `t_lock`, after which no correct recovery is observed (median 12, so a median recovery window of one step); `t_obs`, the first observable sign (median 16; 28% of failures never surface).
  Claude Opus 4.6 drafted the annotations with evidence and two people verified every label, κ 0.78 to 0.94.
- Table II, root causes of the decisive error: epistemic 57.9% (false premise 30.7%, specification neglect 14.9%, output misreading 4.4%, ignored signal 4.1%, premature action 3.7%), competence 32.8%, environment 9.4%.
  For Claude under Terminus2 the epistemic share is 80%.
- Table III, what the agent does after lock-in, with share of failures and share of wasted steps: gives up immediately 18% / 4%; repairs the wrong problem 24% / 39% (median tail 21 steps); keeps repeating the same approach 15% / 29%; performs checks that cannot change the outcome 28% / 15%; claims success with fabricated evidence 15% / 13% (26% of failures overall, median onset exactly at lock-in).
- Finding 4: a Claude Sonnet 4.6 prefix monitor flags locked-in runs at 82% precision with zero median lead time; real-time recall is 18.2% with the task name and 28.8% with the task's requirements.
- Finding 10: successful recoveries take a median of 5 steps, failed ones 12.
- Fig. 5: a `cd` that fails silently; no output reveals it for 17 steps, while every file the agent writes lands in the wrong directory.

### What the repo already has

- The retry prompt the technical spec names ("the Exp 1 v2 protocol prompt") is not on `main`.
  `main` has the one-line `REPAIR_NOTE` of Exp 1 round 1 (`src/trajlab/capture/repair.py`).
  The four-step diagnose, list, fix, re-check protocol is `REPAIR_PROTOCOL` on the unmerged branch `sl/repair-v3-round2`; it ran as round 2 on 2026-10-03 and 2026-10-04 and was withdrawn without results (ADR-0012, amendment of 2026-10-08).
- Exp 1 round 1 ran three independent repairs per failure and arm (`docs/research/2026-10-03_tb40-repair-v2-round1.md`).
  Read by the index of the first passing repair, the curve per arm is:

| Arm | Solved by repair 1 | by repair 2 | by repair 3 |
|---|---|---|---|
| `fresh` | 3 / 21 | 4 / 21 | 4 / 21 |
| `traj` | 8 / 21 | 8 / 21 | 8 / 21 |
| `state-traj` | 7 / 21 | 7 / 21 | 8 / 21 |
| `state` | 1 / 21 | 2 / 21 | 4 / 21 |

- The spec's `is_loop` and `is_revert` fired zero times on the ten example trials, failures included, and the owner ruled on 2026-10-20 that byte-for-byte repetition is the wrong definition (`docs/experiments/exp2/2026-10-19_example-runs.md`).
- ADR-0013's ground truth blames calls per fault, each confirmed by replaying the verifier on a counterfactual fix, and names one decisive call per failed trial, the earliest-introduced blamed call, for Who&When comparability (§7).
  On `tb40-sonnet-v2`, 195 of 202 failing checks never passed at any checkpoint, there were 0 regressions, and 56 of 76 fault items were `wrong_edit` (ADR-0015, Context).
  ADR-0013 §6 already plans a blind subset of 15 failed trials in which a person names the decisive call before seeing the items.
- `trajlab trace` takes step-level labels and a `first_wrong_step` per trial (`contracts/labels.py`, ADR-0015 §8).

## Decision

0. **Scope.** This binds Exp 2's retry loop (technical spec §5), the derived rule set of step 3 (technical spec §3.3), and the failure-timeline labels on the held-out side of the store.
   It changes nothing in capture; no capture `corpus_id` changes.

1. **The retry prompt is the round 2 protocol with one slot per arm.**
   The prompt an attempt's agent receives is, in order and separated by blank lines: the note, the arm's evidence sentence, the four protocol steps, and the task instruction.
   - The note is `{k} previous attempt(s) at this task FAILED the task's tests. The tests are not visible to you and may check cases beyond the examples in the environment.`, where `k` is the number of prior attempts in the chain.
   - The evidence sentence is empty in `control`.
     In `record-files` it names the mount: `The record of those attempts is under /logs/agent/record/: trials.jsonl, steps.jsonl, and deltas/ (one diff per changed file per step). README.md lists the fields.`
     In `record-engine` it names the command and the schema: `The record of those attempts is a read-only SQL database. Run a query with: /logs/agent/bin/query "SELECT ...". Tables: {DESCRIBE output}. Example rows: {one per table}.`
     Nothing else about how to use either is said (technical spec §4.1).
   - The four steps are `REPAIR_PROTOCOL` from `sl/repair-v3-round2` with the clause "and your earlier work on this task if you can see it" removed, since a fresh environment has none and the evidence sentence takes its place: diagnose and write `/logs/agent/diagnosis.md` before changing any other file; list every requirement with a check that would fail without it; fix and re-run until the checks pass; run every check again before finishing.
   - None of the paper's recommendations (validate assumptions before planning, cap recovery effort, verify claimed success) is added to the prompt.
     Each would be a second treatment applied to every arm and would move `control` further from the resample baseline.
   - The prompt differs across arms only in the evidence sentence; a test renders all three for one chain and asserts that.
     The rendered prompt contains `FAILED` and no check name or verifier text (technical spec §9), and its sha256 goes in the preregistration.

2. **N = 3, reported at N = 1, 2, 3.**
   In Exp 1 the resample arm gained nothing from a third repair, and the arms that carried the failed attempt solved almost everything they ever solved at the first repair (table above).
   The record arms are the unknown, and their store grows, so retry 3 is the first retry whose store holds three attempts.
   N = 5 doubles the cost for a control arm whose curve is flat after retry 2.

3. **A chain's store holds its own root failure and its retries, and nothing else.**
   Every attempt is one row of `trials`, keyed by Harbor's `trial_id`, with `attempt_idx` and `parent_trial_id` as ADR-0014 §3 defines them; `steps` and `deltas` rows carry the same `trial_id`.
   At retry t the store holds exactly attempts 0 to t-1 (technical spec §9), and the `record-files` bundle is those rows exported.
   Other failed first attempts of the same task (sibling failures) are not in the store, so every failure meets retry t with exactly t prior attempts and the pairing by failure stays clean.
   A variant that adds sibling failures is a separate arm decided before preregistration, identical in both record arms, and needs its own ADR, because `parent_trial_id` holds one parent and the schema would gain a root column.

4. **The three timeline steps are produced without an LLM judge, on the held-out side.**
   - `t_err` is ADR-0013's decisive call: the earliest-introduced blamed call among the trial's confirmed faults, as a step row.
     A trial whose faults blame no call (`omission`, `missed_fix`, `missing_artifact`) has no `t_err` and is reported in its own stratum; a hand label may mark the step at which the agent moved on, as a separately reported human-only class (ADR-0013 §6).
   - `t_lock` is the last step after `t_err` whose delta touches a path among the fault's blamed files, or `t_err` itself when there is none.
     It is a query over `deltas` and `blame`.
     Both `t_err` and `t_lock` use the causal step of ADR-0020 when a late delta has one, and the landing step otherwise (ADR-0020, item 4).
   - `t_obs` is first computed by a deterministic proxy: the first tool-call step at or after `t_err` whose `is_error` is true or whose output matches a fixed, versioned list of error markers (a non-zero exit, a Python traceback, a failing test line, an `Error:` line).
     The proxy is validated against hand labels: `TrialLabels` gains `first_signal_step`, the `trajlab trace` site asks for it beside `first_wrong_step`, and at least the 15-trial blind subset of ADR-0013 §6 is labeled, all failures if time allows.
     Agreement between the proxy and the labels is reported with the proxy's version.
   - None of the three gates the retry loop; they score localization and describe the sample.
     They are written beside ADR-0013's items in each trial's `groundtruth/` directory and loaded into `heldout.duckdb` as a `timeline` table by the step that loads `checks`; that step bumps `SCHEMA_VERSION`.
     The table's columns are `err_step`, `lock_step`, and `obs_step`, each a `step_idx` of the trial's `steps` rows and null when the step does not exist.
     The `t_` names are the paper's notation and stay in prose only, because `t_start` and `t_end` in `steps` are wall-clock times.
   - An LLM pass, if one is ever wanted for Table II-style root-cause labels, follows the paper's protocol (model drafts with quoted evidence, two people verify, κ reported), stays out of `store/` and off every agent-visible connection, and needs its own ADR.

5. **The derived rule set is structured on the paper's timeline and Table III, not on Table II.**
   Step 3's registry (technical spec §3.3) ships three layers, each deterministic and versioned:
   - **Occurrence views, per trial:** the first step with an error marker in its output, the last mutating step, and the count of tool calls after it with no delta (the trial's tail of probes).
   - **Behavior flags, per step, one family per Table III row:** `is_tail_probe` (a call with no delta after the trial's last mutating step: checks that cannot change the outcome); the same-approach family (`scripts_added` per trial, `variant_of` for a new script whose line set mostly overlaps an earlier script's, `idle_wait` for a call over 120 s with no delta); churn per path (versions of one path, the agent-visible form of repairing the wrong problem); and `unverified_completion` ingested from ADR-0015's detector (fabricated success).
     `is_loop` of schema version 1 is filled by the same-approach family, not by byte-identical arguments; `is_revert` keeps its spec definition as a row flag; `files_touched` stays.
   - **Specification views, from `trials.prompt`:** `names_prompt_path` on delta rows, and per named path its version count and its last hunk.
     This is the one Table II category the store can reach (specification neglect), and the paper's Finding 4 is the evidence that the specification is what makes failures detectable.
   - Root cause is not a column.
     No rule names an epistemic or competence cause, and the paper is cited for why: a frontier-model monitor over the whole prefix reaches 28.8% recall with zero lead time.
   - Every derived column and view is present in both record arms (technical spec §8, question 7: yes).
   - Exact predicates, thresholds, and the error-marker list are frozen in the preregistration and recorded here by amendment before the full run; until then they are candidates.

## Consequences

- **Code to land for step 5:** `REPAIR_PROTOCOL` moves from `sl/repair-v3-round2` to `main` as the body of the retry prompt, with the arm slot; the prompt renderer, the three-arm equality test, and the `FAILED`-and-no-verifier-text grep.
- **Schema:** `heldout.duckdb` gains `timeline` and `TrialLabels` gains `first_signal_step` when step 3 and the ground-truth loader land; each bump is recorded in that PR, and the retry-loop tables of ADR-0014 are unchanged.
- **Cost:** at most usable failures × 3 arms × 3 retries; at 56 failures that is 504 attempts, less early exits on success, on top of rerunning the first attempts on the Linux machine, since the corpus's checkpoint images are gone (`docs/experiments/exp2/2026-10-19_example-runs.md`).
- **Sample reading:** Table III's base rates at 56 failures predict about 10 trials that give up at once, 13 that repair the wrong problem, 8 that repeat an approach, 16 that run futile checks, and 8 that fabricate success.
  A rule that fires zero times on the four example failures is therefore not evidence against it; the rules are judged on the sample.
- **What the paper must say:** Exp 2's `control` is a fresh resample under the protocol prompt, not a bare resample; sibling failures are not given to any arm; `t_lock` is operationalized from deltas, which is narrower than the paper's behavioral judgment; `t_obs` is a proxy with a measured agreement; the paper's numbers come from Terminal-Bench 2.0 under other scaffolds without state capture, so comparisons with them are indicative.
- **What the paper takes from arXiv 2607.09510**, recorded in `docs/paper-notes.md`: the rule set's provenance and base rates (Table III); the per-failure timeline and the `t_err` to `t_obs` gap as the measure of what token space cannot see (median 10 steps, 28% never, in their data); the bound on root-cause detection that keeps root cause out of the store; and the silent-`cd` case and the epistemic share as the motivation for state over tokens.
- **Open:** whether omission faults get a hand-labeled `t_err`; the sibling-failure variant; the error-marker list and the overlap threshold of `variant_of`, both fixed on the sample before preregistration.

## Amendment (2026-10-28): the evidence sentences, and no stop hook (ADR-0022)

Item 1's two evidence sentences are replaced by the owner's decision of 2026-10-28, recorded in ADR-0022 item 2: both record arms are told the same about the record, the schema text (`trajlab.store.describe.describe`), with no example rows.
`record-files` names every file of the bundle, rendered from its layout, and keeps "README.md lists the fields"; the README is the schema text.
`record-engine` names the command and that it prints JSON lines (ADR-0019 item 9), then carries the schema text after "Tables:".
Example rows were dropped because one `deltas` row can exceed the prompt's 128 KiB cap and because rows differ by chain and retry, so no prompt hash could be fixed before the run.
The note, the four steps (from "Work in this order:", without the clause on earlier work), the order, and the blank lines are as item 1 states.
No arm has a stop hook (ADR-0022 item 4).
