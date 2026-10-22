# Exp 2 design spec: does a query engine over state deltas help an agent repair its own failures?

Written 2026-10-18, after the decision to build the #16 system on state deltas joined to tool calls, with Terminal-Bench 4.0 as the use case (`claude/direction_trajectorydb_vs_wu_2026-10-16.md`). Hand this file to the Claude Code agent working in the trajlab repo. Items marked **[decide]** need Shawn's call; items marked **[team]** need Dean's or the team's. Decisions already made by Shawn on 10-18 are marked **[fixed]**.

## 0. Why Exp 2 replaces Exp 1

Exp 1 handed the repair agent raw state (the failed filesystem) or raw history (the transcript). Neither is the premise. The premise is state deltas joined to the actions that produced them, queryable. The arm that tests that (`traj-ckpt`, method 4 in the v2 protocol) was never built. Exp 1's bar chart stays in the paper only as motivation: the form of the record is the variable (resume own session 27.0%, fresh start 11.5%, failed filesystem alone 7.9%, flattened transcript 4.8%; 21 failures x 5 arms x 3 repairs; arms differ at p ~ 0.006, no pair survives correction).

## 1. Question

Given a failed Terminal-Bench attempt, does giving the repair agent the record of prior attempts as a queryable store (schema + SQL + derived columns) raise the repair rate or lower the cost to repair, compared with giving it the same record as files, compared with no record?

## 2. Arms **[fixed: three arms]**

| Arm | Name | What the agent gets | What it stands for |
|---|---|---|---|
| 1 | `control` | Fresh environment, original task, no record. Each retry is an independent resample. | Independent resampling (the SWE-Replay baseline) |
| 2 | `record-files` | Fresh environment, original task, the record of all prior attempts as files: `trials.jsonl`, `steps.jsonl`, and a `deltas/` directory with one diff per step. No engine. | The course brief's ad hoc workflow, with the agent as the analyst |
| 3 | `record-engine` | Fresh environment, original task, the same record through a `query(sql)` tool over the DuckDB store, with derived columns materialized. | The proposed system |

Rules:
- Arms 2 and 3 carry identical information. Arm 2's files are exported from arm 3's database in the same build step, so the only difference is the access form.
- Arm 2 may install DuckDB or write its own scripts. That is ad hoc cost and is recorded, not forbidden.
- Fresh environment every retry, all arms **[decide; recommended]**. Exp 1 showed being in the failed environment hurt (23.8% vs 27.0% resuming the session; 7.9% for a fresh agent in the failed filesystem), and the premise is the record, not the environment. Alternative: resume in the failed environment for all arms; then control is the 7.9% condition.
- Same model, effort, timeout, and repair protocol prompt (v2 §1, diagnose-then-fix) in every arm, with the "same environment" paragraph rewritten for a fresh environment. Stop hook from v2 §2 applies in every arm.

## 3. What the agent is told **[fixed: binary]**

The failure signal is binary: "the previous attempt(s) FAILED." No check names, no verifier output. This is Terminal-Bench's native setting, requires no discretion about which tests to show, and is the strongest test of the premise. The verifier-output condition from v2 §4 is not run.

Consequence for the store: the `checks` table (per-check pass/fail, blamed step) is **held out** from both record arms. The agent sees `trials.reward` (0/1) only. `checks` is used for scoring (§6) and for the Oct 30 design diagram's analyst-side use case, not here.

## 4. Retry loop **[fixed: agent-written test runs are not retries]**

- A **retry** is one harness-run of the hidden Terminal-Bench suite. The agent cannot trigger it; it happens when the agent stops.
- The agent may run its own tests, build commands, or anything else inside an attempt. Those are tool calls in the trajectory and do not count.
- Loop: attempt t ends -> verifier runs -> reward recorded -> if 0 and t < N, attempt t's steps and deltas are ingested into the store (arms 2, 3) -> attempt t+1 starts in a fresh environment with a fresh agent session. Attempt 0 is the corpus first attempt.
- Retries are **sequential with memory** in arms 2 and 3 (the store grows) and independent in arm 1. Name it that way in the paper; this is not pass@k in the independent sense.
- Budget N = 3 **[decide]**. Report at N = 1, 2, 3. N = 5 doubles cost and Exp 1's 11-of-21 never-repaired fraction suggests the curve flattens early.

## 5. Sample

- Candidates: the 56 failed first attempts in the 69-trial Sonnet 5.5 corpus.
- A failure is usable only if its checkpoint chain is complete through the last tool call. **Count complete chains in the registry before fixing N here.** Write the count and the exclusion reasons into the manifest.
- All usable failures, one chain per failure per arm, rather than Exp 1's 21 failures x 3 repeats. Task is a random effect in the model; no task contributes twice.
- Compute: usable failures x 3 arms x up to 3 retries. At 56 that is up to 504 runs, less early exits on success. Comparable to Exp 1's 315.

## 6. Outcomes

**Primary.** Repair rate at budget N: fraction of failures with reward 1 within N retries, per arm. Pre-registered comparison: **arm 3 vs arm 2 at N = 3**, paired by failure (McNemar, or mixed logistic with task random effect). One test. Arm 1 is reported, not tested.

**Efficiency.** Cost to first pass among the solved: tokens, tool calls, wall-clock. Total cost per solve (all attempts, including failed, divided by successes). This is the number the course brief measures and the one Eugene asked for ("make that workload fast"). Arm 3 matching arm 2 on repair rate at lower cost is a result.

**Localization, independent of repair.** Record every `query(sql)` the agent issues (arm 3) and every file read under the record directory (arm 2). Score `touched_blamed_step` 0/1: did the agent's analysis reach the step that the verifier-replay ground truth blames. This links Exp 2 to the analysis framing and survives an all-zero repair table.

**Diagnosis.** Carry over v2 §3 scoring of `/logs/agent/diagnosis.md` (`diag_present`, `diag_localizes_step`, `diag_correct`, `verify_ran`), plus `diag_cites_record` for arms 2 and 3. Ground truth for `diag_correct` is the verifier-replay blame on `sl/groundtruth-replay` (76 fault items over 202 failing checks), not a hand label.

**Also report** the all-zero fraction (failures no arm repaired), per arXiv 2609.34215.

**Not measured here.** pass^k (reliability of a fresh run with a populated store) is a different experiment. Enforcement (pushing a policy into the store) is a different experiment with a different metric; Terminal-Bench has no policy to violate. Human analyst time is not measured; the agent is the analyst.

## 7. The store

Schema (from the 10-15 meeting notes):

```
trials  (trial_id, task, attempt_idx, prompt, model, harness, reward)
steps   (trial_id, step_idx, tool_call_id, reasoning, tool, args, output, t_start, t_end)
deltas  (trial_id, step_idx, path, op, hunk_or_blob_ref)
checks  (trial_id, check_id, passed, blamed_step_idx)   -- held out from the agent in Exp 2
```

- Engine: DuckDB, in-process, Python UDFs. Not the full system.
- Derived columns, materialized at ingest **[decide which]**: `is_loop` (same tool and args as an earlier step, empty delta), `is_revert` (delta undoes an earlier delta on the same path), `files_touched` (count per step). Each has a deterministic implementation; the LLM implementation is a later ablation, not Exp 2.
- `query(sql)` tool: read-only, row cap, returns rows as text. Exposed to the agent as a Claude Code tool **[decide: MCP server vs CLI script in the container]**. Log every call with the SQL, row count, and latency.
- Arm 2 export: `trials.jsonl`, `steps.jsonl`, `deltas/<trial>/<step_idx>.diff`, plus a `README` with the field names. No derived columns as separate fields **[decide]**; if they are included, arm 2 and arm 3 carry identical information and the comparison isolates the engine, which is cleaner. Recommended: include them.
- State is **filesystem deltas from `docker commit`**. Process, shell, and network state are not captured. State this as a limitation in the paper's first paragraph; it is the reason to talk to Alex Xu about Waypoint, not a reason to wait.

## 8. Critical path

1. Count complete checkpoint chains -> sample size.
2. Build `deltas` from the 917 commits: diff consecutive images, join on `tool_call_id`. This is the "state reconciliation" step and nothing else runs before it.
3. Build the DuckDB store with derived columns; export arm-2 files from it.
4. `query(sql)` tool inside the repair container.
5. Retry loop in trajlab (`trajlab exp2` or a v4 repair job): attempt -> verify -> ingest -> retry, fresh environment each time.
6. Dry run on 3 failures, all arms, N = 1. Read the three arm-3 trajectories: did the agent use the tool, and what did it ask?
7. Full run.

Dates: 2 and 3 are the Oct 30 design diagram (schema + predicate interface). 3 and 4 are the Nov 6 prototype. 5 to 7 are mid-November. Final paper Dec 4.

## 9. Relationship to Exp 1

| Exp 1 arm | Exp 2 equivalent |
|---|---|
| fresh start (11.5%) | arm 1 at N = 1 |
| failed filesystem alone (7.9%) | replaced by arm 2: record as deltas, not raw state |
| transcript as text (4.8%) | subsumed by arm 2: steps as a file, never pasted |
| resume own session (27.0%, 23.8%) | not run; session resumption is not the premise |
| `traj-ckpt` (never built) | arm 3 |

## 10. Checklist before running

- [ ] Complete-chain count and exclusions in the manifest.
- [ ] Pre-registration note committed before any arm-3 run: primary comparison, N, test, derived-column list, prompt hash.
- [ ] `checks` table confirmed absent from both record arms' mounts.
- [ ] Arm-2 export diffed against arm-3 tables for information equality.
- [ ] Query log and file-read log verified on the dry run.
- [ ] Stop hook gated on `TRAJLAB_REPAIR=1`, same as v2.
- [ ] **[team]** Dean's agreement on the three arms and the binary signal, before the Oct 30 diagram.

## Sources

Literature behind the protocol prompt, stop hook, diagnosis scoring, and the all-zero problem is in `claude/exp1_repair_protocol_v2_spec_2026-10-02.md` §Motivation and is not repeated here. Directly relied on above: SWE-Replay (arXiv 2601.22129) for the independent-resampling control; Fail-Fast, Restart-Smart (arXiv 2608.03222) for "record as an inspectable artifact, never pasted"; Same Winners, Different Success Rates (arXiv 2609.34215) for reporting the all-zero fraction. Numbers from Exp 1 are from `claude/exp1_round1_full_results_2026-10-06.md`.
