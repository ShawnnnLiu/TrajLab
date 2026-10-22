# ADR-0015: A live failure-pattern detector, and an experiment on state-verified feedback mid-run

Status: accepted (2026-10-22, with the fixes listed under §2's storage rules); proposed 2026-10-18.
Numbered 0015 because 0013 (two branches) and 0014 (`sl/exp2-store`) are taken.
Built on `main`, not on ADR-0014's unmerged branch. Needs no existing corpus: it captures its own.

## TL;DR

- **What:** a detector that runs in the checkpoint watcher at every hooked call. It flags 7 patterns, each tied to the `tool_call_id` that triggered it: `loop`, `no_effect_write`, `false_edit_claim`, `false_test_claim`, `unverified_completion`, `server_state`, `forbidden_action`. Flags are checked against measured state, not against what the agent says.
- **Capture additions:** the hook keeps its full input; the watcher records, per call, running processes and ports, plus line diffs and content hashes of the changed files. Flags and diffs are stored where the agent cannot see them.
- **Experiment:** 8 TB 4.0 tasks × 4 arms × 5 attempts (160 trials), with arms `off`, `nudge`, `state`, and `nudge+record`. `off` is both the baseline and the validation corpus.
- **Validation first:** the `off` trials are hand-labeled blind against a guide fixed in advance, and the detector's precision and recall are measured before any feedback arm runs.
- **Why:**
  - It is the 10-15 meeting's proposal made concrete: a tool that lets an agent debug from state deltas, with `isLoop`-style columns in two forms (from state, and from an LLM over the reasoning) and rules enforced by the system.
  - It covers brief classes 1 and 3, which ADR-0013 leaves to "another source".
  - It targets the observed failure mode, a confident wrong change and then stopping, and it acts within an attempt, where Exp 2 (ADR-0014) acts between attempts.
- **Nudges** reach the agent only through the hook's output, at the call they answer. Flags, diffs, and the log of every message sent stay host-side.
- **Later:** Waypoint saves that keep running programs, and a `rollback` arm; ingesting the flags into ADR-0014's store.

## Context

- ADR-0013's ground truth covers "which call caused a failing check" (brief classes 2, 4, 5), and states that classes 1 (recurring strategies) and 3 (conformance to a process) "need another source".
- On `tb40-sonnet-v2`, first attempts fail by committing to a wrong change and stopping:
  - 56 of 76 causes are `wrong_edit`, and 51 trace to a single call;
  - 195 of 202 failing checks never pass at any checkpoint, and there are 0 regressions;
  - 10 of 55 failures made at most one file-changing call.
- **What the agent knows and does not know.** The ATIF trajectory is the agent's own conversation: its messages, its calls, and the outputs it read. During a run that is already in its context.
  - Harbor writes `trajectory.json` only after the agent finishes, and handing the agent its history back added little in Exp 1: the transcript arm passed 3 of 63 repairs, against 7 of 63 for a fresh start (ADR-0012).
  - What the agent cannot see is the state record: which files each call actually changed, and what was running afterwards.
- **What already exists.** The watcher pauses the agent at every hooked call and measures which paths changed (`CallRecord.changed_paths`). It records no line diffs, no process state, and no checks of claims.
- ADR-0014 plans deterministic `is_loop`, `is_revert`, `files_touched` columns for a store built after trials finish, with no validation.
- The 10-15 advisor meeting asked for a tool that lets agents debug from state diffs, with derived columns such as `isLoop`, and rules pushed into the system as operations. The next section maps each of its points to this ADR.

## Fit with the 10-15 Eugene meeting

| Meeting note | Here |
|---|---|
| Trajectories live in token space, which is not enough; the richest information is in the state deltas | Every pattern is checked against measured state (diffs, hashes, processes), not against the agent's words; `false_*` patterns are exactly the cases where the two disagree |
| How to let agents access the state data; the contribution is a tool that lets an agent debug from state diffs | §4: the agent is told (`state`) or can look it up (`nudge+record`); the experiment measures whether either helps, and which works better |
| An engine that detects patterns and makes the workload fast | §3: deterministic detection inside the hook's wait, no model calls live, cost measured per call (§2's 2 s budget) |
| One row per step: prompt, trajectory, tool call, output, state delta | Each call's records (`.call.json`, `CallRecord`, process snapshot, diffs, flags) share the `tool_call_id`, which is ADR-0014's step row; the flags are designed to become columns there |
| Columns like `isLoop`, decided from the predicate (the reasoning); a DB filter on an LLM call | `loop` and the other patterns, each in two implementations: `state` (deterministic) and `text` (an LLM predicate over the reasoning, cached), compared against human labels (§8) |
| Find concrete use cases; decide what the language must express | The 7 patterns are the use cases. Validation records, per pattern, whether state alone, an LLM over text, or both are needed, which is the expressibility catalog for ADR-0014's query layer |
| Regulation Z: push the rule into the system as an operation, so only correct choices pass | The rules live in the system, not in the agent's prompt. Here the watcher checks them after each call, which is detection only. In the feedback arms the `Stop` block holds an unverified "done" once. Enforcement proper, a `PreToolUse` guard that refuses a rule-breaking call before it runs, is the `guard` arm under Later |

## Decision

### 1. Patterns

Each flag names the `tool_call_id` of the call that triggered it, with evidence (paths, commands, hashes, quoted text).

| Pattern | Flag when |
|---|---|
| `loop` | the same normalized command recurs at least `r` times with no file change between the repeats; or a call restores a path to a content hash it had after an earlier call. A third subtype, `stall` (`k` consecutive hooked calls that change nothing), is flagged but reported separately, because a run of inspection commands looks the same |
| `no_effect_write` | a `Write` or `Edit` call, or a Bash call whose command writes to a path (a redirect, `tee`, `sed -i`, `cp`, `mv`), changes nothing |
| `false_edit_claim` | an agent message says it created or changed a path, and no change to that path exists since the agent's previous message |
| `false_test_claim` | an agent message says tests ran or pass, and no test command ran after the last file-changing call, or the last test command failed (non-zero exit, or failures in its output) |
| `unverified_completion` | the agent's final message says the task is done, and the last test command it ran failed, or none ran after its last change. Live, this means "done, unverified". Offline, the reward says whether the claim was also false |
| `server_state` | a process or listening port the agent started disappears; or an agent message says a server is running and no matching process or port exists. Processes and ports of the harness are ignored: Claude Code itself, the hook, and a port already listening at the trial's first call (one was, in the `--extras` check) |
| `forbidden_action` | a call breaks a rule in the rule set: v1 has generic rules (edits a test file, deletes or empties a file the task provided, writes outside the task's directories) |

`r`, `k`, the command normalization, the list of commands that count as a test run, and the rule set are set on the labeling pilot (§8), then frozen and versioned.

### 2. Capture additions

These change what a trial records; the first corpus under them gets a new `corpus_id`.
- The hook change is always on.
- The watcher's additions are opt-in, with `trajlab watch --extras`, so corpora captured without them stay reproducible.

- **Full hook input:** the hook writes its whole stdin (`tool_input`, `tool_response`, ids) to `<tool_use_id>.call.json` beside the `.req`, before the `.req`. Today it keeps only the ids.
- **Process snapshot:** after each hooked call the watcher runs a POSIX `sh` probe as root in the container (`checkpoint/processes.py`).
  - It reads `/proc` directly, since task images may lack `ps` or `ss`, and records each process's pid, parent pid, and command line, cut to 500 characters, plus the listening TCP ports.
  - The probe's own processes are dropped.
  - Detection compares processes by command line, so a restarted server shows up as a new pid with the same command.
  - It is a second `docker exec`, beside the change gate's `find`: about 60 ms on a test container.
  - One `ProcessSnapshot` per call goes to `processes.jsonl`.
- **Per-call diffs and hashes** (`checkpoint/diffs.py`), for each file and symlink the change gate reports (directories are not recorded):
  - **Now:** the watcher reads the new version from the running container with `tar`.
  - **Before:**
    - from the versions it holds from earlier calls (a held version is dropped when a call changes the path without reading it);
    - otherwise from the last checkpoint's image, which under `--gate change` holds the state before the call;
    - before any checkpoint, from the trial's start image.
  - Image reads use `docker run --rm --network none`, so a restarted watcher still finds before-versions.
  - Every file read gets a sha256 of its new content, and the before-version's sha256 when known; text files also get a unified diff.
  - Removed paths and symlinks are recorded without content.
  - The trial's first measured call has no earlier listing, so it is recorded as `baseline` with no files.
  - One `CallChanges` per call goes to `changes.jsonl`.
- **Diff function:**
  - our own, built on `difflib`, behind one function (`file_change` over `unified_diff`);
  - it uses ADR-0014's text rule (at most 1 MiB, no NUL byte, valid UTF-8) and the change gate's exclusions (`checkpoint/changes.py`);
  - **limits**, from ADR-0014's measurements:
    - no diff when lines before × lines after exceed 1,000,000;
    - at most 20 files read per call, files outside package, cache, and system directories first;
    - no before-version reads once 2 s have passed.
    - at most 500 changes recorded per call (the files that were read first, then the agent's own paths before package and cache paths); `files_total` keeps the full count. A single `pip install` changed 13,838 paths in the `--extras` check.
  - Beyond any limit, a file keeps its path and size, plus its hash if it was read, and a `skipped` reason;
  - it may be replaced by ADR-0014's diff function once that is shared outside `store/`.
- **Flags:** the watcher runs the detector after each answered call and appends `FlagRecord`s (`tool_call_id`, pattern, evidence, detector version, timestamp). `FlagRecord` goes in `contracts/`, with the detector (§3).
- **Where these are stored:**
  - Process snapshots, diffs, hashes, and flags go in `<trial>/trajlab/` on the host, outside the bind-mounted `agent/` directory, so no arm's agent can read them. Each is keyed by `tool_call_id` and `call_seq`.
  - Only `.call.json` sits beside the existing hook files, where the agent can reach it. It holds nothing the agent has not already seen.
  - All of it is written before the call's `.ack`, while the agent waits. A failure is logged and never blocks the ack.
  - Everything `--extras` does for one call shares a 30 s budget (`EXTRAS_BUDGET_S`), so the commit keeps the rest of the hook's 240 s wait; a read the budget does not cover is skipped, and its files are recorded without content.
  - Each call gets at most one line in each file, also when the sweep retries it after a failed commit or a watcher restart.
  - **Fixed 2026-10-22, in review (PR #32), after the `off` corpus was captured.**
    Before the fix, a file that a call changed but did not read (a call reads at most 20) kept the version held from an earlier call, so its next diff and `before_sha256` were against that older version.
    A call retried after a failed commit also got a second, empty line in `changes.jsonl` (`trajlab trace` reads the first).
    Both are reproduced in tests, and end to end on a container.
    In the `off` corpus, any call that changed more than 20 files may be followed by such a diff of a file it changed; after-hashes are unaffected.
- **Contracts:**
  - The new models (`ProcessSnapshot`, `CallChanges`, `FileChange`; later `FlagRecord`) are in a new `contracts/patterns.py`.
  - `CallRecord` is unchanged, so this ADR does not edit `contracts/checkpoint.py`, which ADR-0014 also edits.
- **Code:**
  - `checkpoint/hook/post_tool_use.sh`, with `settings.hooks.json` regenerated;
  - `checkpoint/processes.py` and `checkpoint/diffs.py`;
  - optional backend methods (`processes`, `read_files`, `image_of`, `read_image_files`), which default to unsupported;
  - the watcher's `extras` option.
  - Each comes with offline tests; the probe and the `tar` reads were also checked by hand against real `ubuntu:24.04` and `busybox` containers.

### 3. Detector

- One function per call. It takes the call input, the call output, the call record, the process snapshot, the diffs and hashes, the agent's latest message, the trial's earlier records, and the rule set, and returns flags.
- It lives in `checkpoint/patterns.py`, so the watcher runs it live, and `trajlab patterns <job>` runs the same code offline on finished trials.
- The agent's messages are read live, through the bind mount, from Claude Code's stream-json output (`agent/claude-code.txt`), which holds each message before its call's hook fires. The native session JSONL lags one message behind at `PostToolUse` (open question 2).
- Live claim detection (`false_*`, `server_state` claims) is phrase-based in v1, so the watcher needs no model calls.
- An LLM-judged implementation of each pattern (Haiku 5.5, temperature 0, committed prompts, cached) runs offline only. It sees the transcript but not the state. It is compared with the state-based implementation in validation (§8).

### 4. Feedback and record access

The watcher's policy sets one arm per corpus, recorded in `policy.json` and the manifest:

| Arm | When a flag fires, the agent gets |
|---|---|
| `off` | nothing; flags are only recorded |
| `nudge` | a fixed message with no state information: "Check that your work so far does what you intend." |
| `state` | the flag's evidence, e.g. "You said tests pass, but no test ran after your edit to `parse.py` (`toolu_…`)." |
| `nudge+record` | the `nudge` message, and, for the whole trial, a read-only command `/logs/agent/bin/record [n]` that prints its last `n` calls with their changed paths, diffs, and process changes, but not the flags |

- **Same moments:** `nudge`, `state`, and `nudge+record` speak at the same calls, wherever the detector fires.
- **Where nudges live:**
  - The hook runs inside the container and can read only `/logs/agent`. So the watcher writes the message to `agent/checkpoints/<tool_use_id>.feedback` just before it writes that call's `.ack`, while the agent is still paused.
  - The hook prints the message in Claude Code's hook-output format, deletes the file, and exits.
  - **`PostToolUse`:** the format is `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "<message>"}}`, which the model receives as context next to the tool result.
    - `{"decision": "block", "reason": …}` also reaches the model, but as a hook error that quotes the hook's own command, so it is not used.
    - Checked 2026-10-19 on 2.1.278 (`feedbackcheck-A`, `-B`).
  - So a message exists in the agent's view only for the call it answers, and only at the moment it is delivered. No `.feedback` file is ever written in `off`.
  - Every message sent (call, arm, pattern, text) is also logged host-side in `<trial>/trajlab/feedback.jsonl`, which is the experiment's record of what each agent was told.
- **Stop:** for `unverified_completion` at `Stop`, the hook blocks the stop once, with the arm's message as the reason.
  - It prints `{"decision": "block", "reason": "<message>"}`, which the model receives as a user turn, "Stop hook feedback: <message>".
  - A second stop is not blocked: the hook exits 0 when its input has `"stop_hook_active": true`.
  - Checked on 2.1.278 in the same trials: the agent continued, did what the reason asked, and the trial then ended normally.
- **Record delivery:** `record` writes a request file under `/logs/agent/`, and a host process answers it from `<trial>/trajlab/`. It uses the same pattern as the hook, and the channel ADR-0014 plans for its query tool (its step 4). In the other arms the command does not exist.
- The hook-output fields above were checked on Claude Code 2.1.278 (ADR-0009); see open question 1.

### 5. Experiment

- **Questions:**
  - Does state-verified feedback mid-run raise the pass rate, or lower the cost to pass, compared with a nudge at the same moments?
  - Does access to its own state record do the same?
  - Is it better to tell the agent what is wrong, or to let it look?
- **Tasks:** 8 TB 4.0 tasks from `tb40-sonnet-v2`'s 23:
  - tasks with 1 or 2 of 3 first-attempt passes, or with passing repairs in ADR-0012;
  - at least 2 where agents run servers (`vba-userform-port`, `sound-change-cascade`).
  - **Candidates:** `layout-config-recreation2` (1/3), `sound-change-cascade` (2/3), `vba-userform-port` (2/3), `cad-model`, `mvcc-lsm-compaction`, `vf2-speedup-networkx`, `pretrain-shard-corruption` (0/3 with passing repairs), and `layout-config-recreation` (0/3; its ground-truth items blame 11 to 17 edits each, the closest case to a loop).
  - **Excluded:** `sglang-qwen-burst` (8.8 GB containers made commits time out), `freecad-platform-drawing` (a trial lost every checkpoint), `interleaved-vigenere` (6 safety refusals in ADR-0012 round 1).
  - **Fixed 2026-10-19 after phase 0: all 8 candidates kept** (`configs/harbor/tb40-detector-off-v1.json`).
    - The pilot (`tb40-detector-pilot-v1`): 8 trials, 2 passed (`mvcc-lsm-compaction`, `vba-userform-port`), and no checkpoint timeouts.
    - `pretrain-shard-corruption` hit the 4 h cap (`AgentTimeoutError`) after 274 hooked calls and 95 checkpoints. It is kept anyway: its long run of unchanged calls is the corpus's best case for `loop` and `stall`.
    - Its place in the feedback arms is decided after phase 2.
- **Setup:** Sonnet 5.5, medium effort, Claude Code 2.1.278, `--gate change`, pre-installed images (ADR-0008), 4 h agent cap. All as in `tb40-sonnet-v2`, so the arms differ only in §4.
- **Size:** 5 attempts per task per arm, 160 trials.
  - Five matches the Terminal-Bench leaderboard and Anthropic's TB 4.0 protocol, and resolves a task's pass rate in steps of 20%.
  - At 40 trials per arm, a rise from 30% to 50% is detected about half the time, so more tasks are preferred over more attempts if the budget grows.
  - If the budget is short, `nudge+record` moves to a second round and the first round is 120 trials.
- **Baseline:** no separate run without the detector. The detector adds no message in `off` and only milliseconds to the existing hook pause, so `off` is the baseline. The pause is measured and reported.
- **Primary comparison:** the pass rate of `state` vs `nudge`, paired by task (Wilcoxon signed-rank, task-cluster bootstrap interval).
- **Secondary comparisons:** `nudge+record` vs `nudge` (does looking help) and `state` vs `nudge+record` (telling vs looking); each arm vs `off`.
- **Other measures:**
  - tokens, cost, agent seconds, and tool calls, per trial and per pass;
  - flags per trial, by pattern;
  - the share of flags the agent acts on (its next call addresses the flagged path or command);
  - `record` calls per trial in `nudge+record`;
  - the time to the first flag in failing vs passing trials (class 5).

### 6. Phases

0. **Task pilot**, with the current capture and before §2 is built: the 8 candidates × 1 attempt, one at a time, as `tb40-detector-pilot-v1`.
   - It checks that each task runs on the capture machine, measures durations, and caches the derived images.
   - It gives transcripts to draft the labeling guide from, and a first count of pattern occurrences.
   - It has no process snapshots or diffs, and is not part of the scored corpus.
1. **Capture changes and the `off` corpus.** §2 with tests, merged to `main`, then 8 tasks × 5 attempts with arm `off`. The labeling guide (§8) is written meanwhile.
2. **Detector and validation.** §3 run offline on the `off` corpus, then labels, precision, and recall. Thresholds and rules are frozen after this phase.
3. **Feedback and record access.** §4, piloted on 2 tasks to check delivery and stability.
4. **Experiment.** The `nudge`, `state`, and `nudge+record` arms (120 trials), then the analysis.

### 7. Not part of this ADR's code

- ADR-0014's store: the flags file is data, which the store can ingest later without importing `checkpoint/`.
- ADR-0014's image-based deltas: once merged, they are an offline check on the live diffs of §2.

### 8. Validation

1. **Labeling guide:** one page per pattern (definition, positive and negative examples, edge cases such as polling a slow build). Committed before any detector output is seen.
2. **Labeling pilot:** 5 `off` trials labeled blind. The guide, `r`, `k`, the test-command list, and the rules are revised, then frozen. Pilot trials are excluded from scoring.
3. **Labels:** every remaining `off` trial gets a yes/no per pattern. A sample of 12 trials, stratified by task and reward, is labeled at every step. Labelers never see flags before finishing a trial.
4. **Second labeler:** 6 of the 12 step-level trials are labeled again by a second team member.
5. **Measures:**
   - per pattern and implementation, precision and recall at the step and trial level, with task-cluster bootstrap intervals;
   - Cohen's κ between labelers;
   - the accuracy of the live phrase-based claim detection, measured on its own.
6. A pattern with κ below 0.6, or fewer than 10 positive labels, is reported as a count, not scored. The feedback arms use only the patterns that pass.
7. **Labeling tool:** `trajlab trace <jobs dir>` serves a local site that shows each call's input and output beside its diffs and process changes, with a checklist per hooked call, per stop, and per trial.
   - Labels cover the 7 patterns plus `stall`, which §1 reports apart from `loop`, and optionally the trial's first wrong step: the call where it first went wrong.
   - The site draws this section's samples with fixed seeds (the 5 pilot trials, the 12 step-level trials, 6 of them for the second labeler) and computes Cohen's κ per pattern between two labelers.
   - One `TrialLabels` file (`contracts/labels.py`) per labeler per trial, under `corpus/labels/`, which git ignores. The site exports them as CSV or JSON, one row per trial and per judged step, keyed by trial and `tool_call_id`, the key the flags carry.
   - The site's hints (repeated commands, files back to an earlier version, claim cross-checks) resemble the detector's rules, so they are off by default and stay off while labeling trials that are scored.

## Consequences

- Classes 1 and 3 get a measured source, with a state-versus-text comparison and a labeled set.
- If `state` or `nudge+record` beats `nudge`, the claim that the state, not the interruption, is what helps is supported.
- **Limits:**
  - The state is the filesystem plus a process list. Memory and shell state are not captured (ADR-0004), so a server that is running but wedged looks healthy.
  - Diffs are skipped beyond the limits of §2, so a call that rewrites many files is flagged on paths and hashes only.
  - Phrase-based claim detection misses claims phrased unusually; its recall is measured.
- **Risks:**
  - A false flag in a feedback arm can mislead the agent, so §8's precision is reported next to the experiment.
  - If patterns are rare on these tasks, the feedback arms have little to act on. Phase 1's flag counts show this before phase 3, and tasks can be swapped then.
- **Costs:**
  - **Snapshot pauses grow with the container's changed layer**, because each `docker commit` saves the whole writable layer.
    - In the `--extras` check (`cad-model`, 2026-10-19), `pip install cadquery` grew the layer to 2.2 GB, and every later commit took about 80 s; 7 commits took about 8.5 of the trial's 12 minutes.
    - The pilot's `cad-model` trial had a median of 75 s per commit, without `--extras`; the other 7 tasks had medians of 2 to 7 s.
    - The pause is recorded per checkpoint (`capture_ms`) and reported apart from the agent's working time.
    - A layer large enough for a commit to outlast the hook's 240 s wait loses its checkpoints, as `sglang-qwen-burst` did in ADR-0012; no task of this experiment came near it in the pilot.
  - **The extras' own cost** in the same check: about 80 ms per call for the process probe, and 50 to 150 ms for the diffs.
  - Diffing and detection run inside the hook's wait. They are bounded by §2's 2 s budget, measured, and count against the agent's time cap, as checkpoints do.
  - 160 trials share one subscription's rate limits.

## Later, each with its own ADR

- **Waypoint integration:**
  - selective saves that keep running programs, taken when the process snapshot changes, when a flag fires, and at the final state before the tests (the pre-verifier save in `waypoint-exp/run_trial.py`; ADR-0013 on `ai/waypoint`);
  - then a `rollback` arm that reopens the save before a flagged call, programs running, instead of sending a message.
  - Per-call Waypoint capture needs, first:
    - a hook-to-host channel, since Waypoint sessions have no bind-mounted `/logs/agent`;
    - Claude Code launched in the background, because a running `waypoint exec` blocks saves;
    - CRIU settings for the folders Claude Code watches.
  - Its overhead, disk, restore fidelity, and effect on the agent can be evaluated as a systems result in their own right.
- **Store integration:** the flags ingested into ADR-0014's store as validated columns, filling `is_loop` and `is_revert`. The diff function shared with `store/`.
- **A `guard` arm (Regulation Z):** a `PreToolUse` hook refuses a call that breaks a rule before it runs, and says why. It turns `forbidden_action` from a report after the fact into a guarantee.
- **Task-specific rules:** rules taken from each task's instruction.

## Open questions

1. **Closed (2026-10-19):** the hook-output fields for feedback in Claude Code 2.1.278.
   - Two Harbor hello-world trials on 2.1.278 (`feedbackcheck-A`, `-B`). In each, a test hook asked the agent to create a marker file, through a different field.
   - The agent created the marker every time, and both trials kept reward 1.0.
   - `PostToolUse` uses `additionalContext`, and `Stop` uses `decision: block` with `stop_hook_active` checked (§4).
2. **The live source of the agent's messages.**
   - Checked 2026-10-18 with Claude Code 2.1.302, outside Harbor. When a `PostToolUse` hook fires, the native session JSONL does not yet hold the call's own `tool_use`, nor the assistant text before it; it runs one message behind.
   - So §3's "messages come from the native session JSONL" does not hold live.
   - **The stream-json output does hold them.** In the same check, the `--output-format stream-json` stream (which Harbor tees to `agent/claude-code.txt`) already held, when each hook fired:
     - at each `PostToolUse` hook, the call's own `tool_use` and the assistant text right before it;
     - at `Stop`, the final message.
   - So §3 reads messages from `agent/claude-code.txt`.
   - **Confirmed 2026-10-19 with 2.1.278** in a Harbor hello-world trial (`hookcheck-278`), with the output passing through Harbor's `tee`. Each hook copied `claude-code.txt` as it stood when the hook fired:
     - at `PostToolUse`, the copy already held the call's `tool_use`;
     - at `Stop`, the copy already held the final assistant text.
   - That trial's only call had no text before it. The stream is written in order, so a copy that holds a `tool_use` holds all text before it.
   - **Closed.**
3. **Closed (2026-10-19):** the 8 tasks. All 8 candidates kept after phase 0 (§5).
4. Who the second labeler is.
5. Whether ADR-0014 wants `is_loop` and `is_revert` to come from this detector, and the diff function shared.
