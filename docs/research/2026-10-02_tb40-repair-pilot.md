# TB 4.0 repair pilot: four repair arms on Sonnet 5.5 failures (2026-10-02, overnight)

A pilot of the repair experiment: run Claude Sonnet 5.5 at medium effort on TB 4.0 tasks once, then repair every failed trial once under four arms, with the same model and effort.
The question is whether the failed trial's environment, its trajectory, or both help a repair agent, measured by completion, tests passed, agent time, tokens, and cost.
This is a pilot: one trial per (task, arm), run on an arm64 Mac under amd64 emulation.
It finds problems with the design; it does not support claims about which arm is better.

## Setup

- **Model:** `anthropic/claude-sonnet-5-5`, `reasoning_effort: medium`, Claude Code 2.1.278 (pinned, ADR-0009), subscription auth.
- **Capture:** every trial captured as usual (`trajlab watch --every 1 --gate change`, pre-installed agent image, ADR-0008, ADR-0010, ADR-0011).
- **Time cap:** `agent_timeout_multiplier: 0.25`, so 2 h of agent time instead of TB 4.0's 8 h; the same cap for first attempts and repairs.
- **Tasks:** TB 4.0 (`terminal-bench/terminal-bench@4.0.0`) has no difficulty labels, so "medium" was approximated by `expert_time_estimate_hours` of 1-8 h, excluding GPU tasks, multi-container tasks, tasks needing more than 4 GB, and tasks with large images.
  Three jobs with identical settings: `tb40-sonnet-v1` (6 tasks), `tb40-sonnet-v1b` (4), `tb40-sonnet-v1c` (4); configs in `configs/harbor/`, manifests in `corpus/manifests/`.
- **Arms** (`scripts/2026-10-02_repair_arms.py`; each (task, arm) is its own job `tb40-repair-v1-<task>-<arm>`):

| Arm | Environment the repair starts in | Conversation it starts with |
|---|---|---|
| `fresh` | the task image, as a new attempt would | none |
| `state` | the failed trial's final checkpoint | none |
| `state-traj` | the failed trial's final checkpoint | the failed trial's full native session (`claude --resume`) |
| `traj` | the task image | the failed trial's full native session |

- **Prompt:** every arm runs `trajlab.capture.repair:RepairClaudeCode`, which puts one line before the task instruction: "A previous attempt at this task did not pass the task's tests."
  Arms differ only in starting environment and loaded conversation, not in what they are told.
- **Final checkpoint:** the failed trial's highest-seq checkpoint, its stop checkpoint when the filesystem changed after the last hooked call (ADR-0011).
- **Metrics** (`scripts/2026-10-02_repair_report.py`): reward; tests failing (or rule violations, for music-harmony); agent execution seconds; API calls, tool calls, and tokens of the repair's own messages only; cost at Sonnet 5.5 list prices ($2 input, $2.50 / $4 cache write 5 m / 1 h, $0.20 cache read, $10 output, per million tokens).

## Results

First attempts: 6 of 14 passed (43%); cost $8.56 at list prices.
Passed: embedding-drift-monitor, production-planning, sound-change-cascade, interleaved-vigenere, risk-scorer-replay, fin-saccr-rwa.
Failed: wal-recovery-ordering, mvcc-lsm-compaction, cargo-flight-dispatch, session-window-debug, foodstuff-beta-activity, music-harmony, roy-polymorph-cn, layout-config-recreation2 (agent timeout).

Repairs of all eight failures (32 repair trials, $6.19 own cost in total):

| Arm | Passed | Fewer tests failing | Same | More | Agent s | API calls | Tool calls | Output tokens | Own cost |
|---|---|---|---|---|---|---|---|---|---|
| `fresh` | 0/8 | 3 | 4 | 1 | 8286 | 85 | 94 | 94,705 | $2.61 |
| `state` | 1/8 | 2 | 5 | 1 | 3707 | 60 | 57 | 52,984 | $1.50 |
| `state-traj` | 1/8 | 4 | 3 | 1 | 3312 | 36 | 29 | 37,857 | $1.01 |
| `traj` | 0/8 | 2 | 6 | 0 | 1156 | 44 | 35 | 42,629 | $1.07 |

"Fewer tests failing" counts a pass.
`fresh`'s agent time is dominated by one 2 h timeout (layout-config-recreation2); without that task the ordering of the arms by time and cost is the same.

Per task, tests failing after the first attempt and after each repair (bold: passed):

| Task | First attempt | `fresh` | `state` | `state-traj` | `traj` |
|---|---|---|---|---|---|
| wal-recovery-ordering (97 tests) | 2 | 2 | 2 | 2 | 2 |
| mvcc-lsm-compaction (15) | 4 | 4 | 4 | 4 | 4 |
| cargo-flight-dispatch (27) | 8 | 5 | 8 | 5 | 6 |
| session-window-debug (7) | 2 | 1 | 1 | 1 | 2 |
| foodstuff-beta-activity (13) | 2 | 3 | 2 | 3 | 2 |
| music-harmony (rule violations) | 13 | 10 | 16 | 8 | 8 |
| roy-polymorph-cn (3) | 1 | 1 | 1 | 1 | 1 |
| layout-config-recreation2 (9) | 1 (2 h timeout) | 1 (2 h timeout) | **0** (46 min) | **0** (44 min) | 1 (6 min) |

Full per-trial rows, including tokens by kind: `corpus/jobs/<source job>/repair-report.json` (not in git); regenerate with the report script.

## What the pilot shows

- **Two repairs passed, both on the one failure that had made real progress, and both started from its final checkpoint.** layout-config-recreation2 ran its full 2 h and left near-finished work on disk (8 of 9 tests passing, plus its own fitting scripts and intermediate data in `/tmp`).
  `state-traj` opened those files first (`/tmp/E.json`, `/tmp/opt.py`), kept running the first attempt's optimizer, and reached a pixel-exact match in 44 min for $0.30.
  `state`, with no conversation, found the existing `config.json`, diffed its rendering against the target, and refined the image positions to pass in 46 min.
  `traj` had the conversation but a fresh environment: the files were gone, it rebuilt the layout from memory, declared 99.3% pixel agreement after 6 min, and failed the same test as the first attempt.
  `fresh` started over and hit the 2 h cap again with the same test failing.
  This is the case the checkpoint exists for: state the agent built up and that the conversation alone cannot restore.
- **Early-stop failures are not repaired by any arm.** In 5 of 8 failures the first attempt ended its turn within 3 minutes (51-172 s), typically after one or two patches and without running the task's tests; the passes ran 6-17 minutes.
  The repairs of those mostly did the same, whatever they started from, and the "previous attempt failed" note did not change that: 0 of 20 passed.
- **Loading the trajectory makes repairs shorter and cheaper.** `state-traj` used the fewest API calls in total (36 vs 85 for `fresh`) and the lowest own cost ($1.01 vs $2.61), and had the most partial improvements (4 of 8).
- **`traj` sees the mismatch between history and state.** In wal-recovery-ordering the `traj` agent noticed "the files are back to their original state" and reapplied its earlier fixes from memory; in layout-config-recreation2 it could not recover the working files its history referred to.
- **`state` without history is uneven.** It passed layout-config-recreation2 but improved only one other task, made music-harmony worse (13 to 16 violations), and in wal-recovery-ordering said it "couldn't find why the previous attempt failed".

## Accounting facts found on the way

- **Claude Code's reported cost for a resumed run is cumulative, not $0.** For `state-traj` and `traj`, `total_cost_usd` equals the source trial's cost plus the repair's own (mvcc-lsm-compaction: $0.0958 + $0.0491 = $0.1450).
  This corrects the 2026-10-02 note that resumed runs report $0. Use the repair's own messages: assistant messages whose `uuid` is not in the source session, deduplicated by API message id.
- **The priced own cost matches Claude Code's to the cent** on every non-resumed trial, which validates the price table and the dedup rule.
- **Resumed sessions contain the loaded history verbatim** (same `uuid`s), followed by the repair's messages.

## Problems with the design to fix before the real run

1. **Too few repairable failures.** With Sonnet 5.5 at medium effort, failures are mostly early stops at high partial credit, and every arm stops early again; only the one long failure was repairable.
   A useful run needs failures that a repair can plausibly fix: higher effort for the repair, a larger task set, or tasks chosen by measured pass rate rather than expert-time estimate.
2. **n = 1 per cell.** At this size only discordant pairs mean anything (exact McNemar); the real run needs several tasks' worth of discordance, which means many more failures.
3. **The note is a confound across arms only in what it means.** In `fresh` it is information about a run the agent cannot see; in the resumed arms it follows a full transcript. Keep it identical, but state this.
4. **Resumed arms see extra messages** (Harbor re-sends the instruction; Claude Code may add notices), as found on 2026-10-02.
5. **Emulation.** amd64 images under emulation on arm64 inflate agent and verifier time; do not compare these seconds with other machines.

## Operational notes

- **Separate verifier environments.** TB 4.0 tasks verify in a separate environment that Harbor builds with the trial's own environment class; pre-install and checkpoint resume now leave that environment alone (commit `dd34191`, test `test_separate_verifier_environment_starts_as_harbor_would`).
- **Disk.** The run had under 20 GB free. Checkpoints of one long trial (layout-config-recreation2, 47 checkpoints) reached 5 GB because each `docker commit` holds the trial's whole writable layer, not the delta from the previous checkpoint.
  The launcher's disk guard keeps only each trial's latest checkpoint image once free space falls below 8 GB; `checkpoints.jsonl` keeps every record, so intermediate checkpoints of this pilot are records without images.
- **Manifest names.** `trajlab run` names a manifest after the config file's stem; the launcher passes `--corpus-id <job>` because every repair config is `config.json`.
