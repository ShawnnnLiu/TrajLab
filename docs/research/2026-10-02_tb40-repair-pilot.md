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

Repairs of the seven failures whose four arms all finished (layout-config-recreation2 below):

| Arm | Passed | Fewer tests failing | Same | More | Agent s | API calls | Tool calls | Output tokens | Own cost |
|---|---|---|---|---|---|---|---|---|---|
| `fresh` | 0/7 | 3 | 3 | 1 | 1086 | 37 | 31 | 63,577 | $1.42 |
| `state` | 0/7 | 1 | 5 | 1 | 917 | 41 | 35 | 45,759 | $1.16 |
| `state-traj` | 0/7 | 3 | 3 | 1 | 677 | 24 | 19 | 33,079 | $0.71 |
| `traj` | 0/7 | 2 | 5 | 0 | 780 | 36 | 29 | 37,633 | $0.86 |

Per task, tests failing after the first attempt and after each repair:

| Task | First attempt | `fresh` | `state` | `state-traj` | `traj` |
|---|---|---|---|---|---|
| wal-recovery-ordering (97 tests) | 2 | 2 | 2 | 2 | 2 |
| mvcc-lsm-compaction (15) | 4 | 4 | 4 | 4 | 4 |
| cargo-flight-dispatch (27) | 8 | 5 | 8 | 5 | 6 |
| session-window-debug (7) | 2 | 1 | 1 | 1 | 2 |
| foodstuff-beta-activity (13) | 2 | 3 | 2 | 3 | 2 |
| music-harmony (rule violations) | 13 | 10 | 16 | 8 | 8 |
| roy-polymorph-cn (3) | 1 | 1 | 1 | 1 | 1 |

Full per-trial rows, including tokens by kind: `corpus/jobs/<source job>/repair-report.json` (not in git); regenerate with the report script.

## What the pilot shows

- **No arm repaired anything.** 0 of 28 repairs passed; the arms cannot be ranked on completion at this size.
- **The repair inherits the first attempt's failure mode.** Most first-attempt failures were short: in 5 of 8 the agent ended its turn within 3 minutes (51-172 s), typically after one or two patches and without running the task's tests; the passes ran 6-17 minutes.
  The repairs mostly did the same, whatever they started from, and the "previous attempt failed" note did not change that.
- **Loading the trajectory makes repairs shorter and cheaper.** `state-traj` used the fewest API calls (24 vs 37 for `fresh`), the least agent time, and half the own cost of `fresh`, with the same number of partial improvements.
  This is the clearest signal in the pilot, and it is an efficiency result, not a completion one.
- **`traj` saw the mismatch.** In wal-recovery-ordering the `traj` agent noticed "the files are back to their original state" and reapplied its earlier fixes from memory; the arm's premise (history without its state) is visible to the agent.
- **`state` without history did worst on partial credit.** Starting in the failed state with no conversation, the agent improved only one task and made music-harmony worse (13 to 16 violations); in wal-recovery-ordering it said it "couldn't find why the previous attempt failed".

## Accounting facts found on the way

- **Claude Code's reported cost for a resumed run is cumulative, not $0.** For `state-traj` and `traj`, `total_cost_usd` equals the source trial's cost plus the repair's own (mvcc-lsm-compaction: $0.0958 + $0.0491 = $0.1450).
  This corrects the 2026-10-02 note that resumed runs report $0. Use the repair's own messages: assistant messages whose `uuid` is not in the source session, deduplicated by API message id.
- **The priced own cost matches Claude Code's to the cent** on every non-resumed trial, which validates the price table and the dedup rule.
- **Resumed sessions contain the loaded history verbatim** (same `uuid`s), followed by the repair's messages.

## Problems with the design to fix before the real run

1. **Too few repairable failures.** With Sonnet 5.5 at medium effort, failures are mostly early stops at high partial credit, and every arm stops early again.
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
