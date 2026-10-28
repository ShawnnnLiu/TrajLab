# ADR-0020: Attribute a late delta to the call that launched the background job

Status: proposed (2026-10-26).

## Context

The store attributes every delta row to the step whose checkpoint first contains it (ADR-0014; `reconcile._row` sets `step_idx` from the checkpoint's owning call).
That is correct as a record of state: the checkpoint proves the file was present after that call.
It is wrong as a record of cause whenever the write was made by a process the agent left running.

Harbor runs Claude Code with `FORCE_AUTO_BACKGROUND_TASKS=1` and `ENABLE_BACKGROUND_TASKS=1` (`ClaudeCode.run`, `docs/harbor-facts.md`).
A Bash command that reaches its foreground timeout (`BASH_DEFAULT_TIMEOUT_MS`, 120 s by default) is moved to the background, and the tool returns at once with a task id.
The PostToolUse hook fires on that return, so the call's checkpoint and its `t_end` record the moment the command was backgrounded, not the moment it finished.
Whatever the job writes afterwards lands in the top layer of a later call's checkpoint and is attributed to that call.
ADR-0011 handled the end of the turn and left the between-calls case as a threat ("Background writes" in `docs/paper-notes.md`), unmeasured.

On 2026-10-26 Amina Isayeva measured it on the 40-trial `tb40-detector-off-v1` corpus while labeling for ADR-0015 §8.
She found 104 tool calls where the agent said it had written a file and the call's own delta did not contain it.
Every one of the 104 writes appeared in a later checkpoint, between 1 and 18 hooked calls after the claim.
None was a retried commit, a duplicate change line, a subagent call, or an instance of the stale before-version bug fixed in PR #32.
The capture lost nothing; the attribution lagged.

The lag reaches three consumers:

- ADR-0016 item 4: `t_err` is the step row of ADR-0013's decisive call, found as the owner of the checkpoint where the blamed content first appears, so it names the landing step; `t_lock` is the last step whose delta touches a blamed file, so it moves later by the lag.
- ADR-0018 item 3: a *mutating* step, a *tail probe*, `version`, and `variant_of` all read `deltas.step_idx` as the step that made the change.
- ADR-0015: `no_effect_write` and `false_edit_claim` flag exactly this case, so the 104 are false positives for them.

The owner asked whether the lag can be prevented at capture time, whether the launching call can be recovered deterministically, and whether that needs an LLM judge.

What the store and the trial directory already hold:

- `steps.t_start` is the ATIF step's timestamp (the call was issued) and `steps.t_end` is the hook request's mtime (`CallRecord.requested_at`, the call returned), both aware UTC (`store/steps.py`).
- `steps.output` is the tool result as the agent saw it, so a backgrounded call's task-id text is in the store.
- The completion of a background job arrives as a later turn of the native session JSONL, which `atif/compaction.py` already reads; its exact shape is undocumented and has to be read from real trials.
- Every entry of a checkpoint's top layer carries the file's mtime in its `TarInfo`; `store/layers.py` (`file_entry`) builds `Entry` without it, so the store drops the one timestamp that says when the write happened.
- The `--extras` process snapshots (ADR-0015, `ProcessSnapshot`) list pid, ppid, and command per hooked call, when the corpus was captured with them.

What Claude Code offers, from its documentation read on 2026-10-26: `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` turns off the move of timed-out commands to the background and removes `run_in_background`; `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` (600 s) set when the move happens; none of its 33 hook events fires when a background command finishes.
Harbor applies the job's declarative env (`--ae`) after it sets its own two variables (`env.update(self._resolved_env_vars)` in `ClaudeCode.run`), so either variable can be overridden from outside without patching Harbor.

## Decision

0. **Scope.** The Exp 2 store's attribution of a delta row to the tool call that caused it: `trajlab.store.layers`, `trajlab.store.reconcile`, `trajlab.store.derive`, store schema version 3, and the consumers named in Context.
   Capture is unchanged; no `corpus_id` changes.

1. **A delta row keeps its landing step and gains the write time.**
   `Entry` gains `mtime` (the tar entry's mtime, seconds since the epoch, as `TarInfo` gives it), and `file_entry` fills it.
   `deltas` gains `mtime_before` and `mtime_after`, aware UTC, null when that side of the row does not exist.
   `deltas.step_idx` stays the *landing step*: the step whose checkpoint first contains the change.
   This is `SCHEMA_VERSION` 3; a version-2 store is rebuilt from its images, because the value is not in its rows.

2. **A step knows whether Claude Code backgrounded it, and when the job ended.**
   The view `step_jobs` in `trajlab.store.derive` gives each tool-call step `backgrounded`, `task_id`, and `background_done_step`.
   A *backgrounded call* is a Bash step whose `output` matches the task-id pattern.
   Its *job window* runs from its `t_start` to the `t_start` of `background_done_step`, the step at which the completion notice for its `task_id` arrived, or to the trial's stop record when no notice arrived.
   The two text inputs, the task-id text in `steps.output` and the completion notice in the session JSONL, are fixed, versioned patterns like ADR-0018's error markers.
   They are fixed after reading them in the 40-trial corpus and recorded here by amendment; until then they are candidates.

3. **Attribution is a derived view, computed by one ordered rule with no judgment call.**
   The view `delta_attribution` gives every delta row an `attribution` value and a nullable `launched_by_step`.
   Let k be the row's landing step, p its path, and m its `mtime_after` (`mtime_before` for a delete).
   The first case that holds wins:
   1. `foreground`: m lies in [`t_start`, `t_end`] of step k itself. `launched_by_step` is k.
   2. `background`: exactly one backgrounded call j before k has a job window containing m. `launched_by_step` is j.
   3. `background_by_path`: several job windows contain m, and exactly one of those calls' `args_json` contains p's basename. `launched_by_step` is that call.
   4. `ambiguous`: several job windows contain m and case 3 picks none. `launched_by_step` is null.
   5. `background_untracked`: no job window contains m, m lies in no tool call's [`t_start`, `t_end`], and exactly one earlier Bash call launches a job by hand (`&` as a separator, `nohup`, `setsid`, `disown`), still listed in the process snapshot at step k minus 1 when the trial has snapshots. `launched_by_step` is that call.
   6. `untrusted_mtime`: m is earlier than the trial's first `t_start`, so the write preserved an older timestamp (archive extraction, `cp -p`, `rsync -a`). `launched_by_step` is k when no job window covers the gap from step k minus 1 to k, else null.
   7. `unknown`: anything else. `launched_by_step` is null.
   A row is a *late delta* when `launched_by_step` is set and earlier than `step_idx`.

4. **Consumers use the causal step when there is one.**
   The *causal step* of a delta is `coalesce(launched_by_step, step_idx)`.
   ADR-0018's *mutating* step, *tail probe*, `version`, and `variant_of` read the causal step; the landing step stays available as `step_idx`.
   ADR-0016's `t_err` is the causal step of the earliest-introduced blamed delta and `t_lock` the last causal step whose delta touches a blamed file.
   The localization score of ADR-0013 is reported twice: strict, on the causal step, and windowed, accepting either the causal or the landing step.
   A trial whose blamed delta is `ambiguous` or `unknown` is counted and left out of the localization score, not out of the repair-rate comparison.
   ADR-0015's `no_effect_write` and `false_edit_claim` do not fire on a backgrounded call, nor on a hand-launched job's call, for a path that changes inside the job window.

5. **The record arms see the attribution.**
   `step_jobs` and `delta_attribution` are relations of the `record-files` bundle and of the `record-engine` connection (ADR-0018 item 5, ADR-0019) like every other view, in both arms.
   An agent that reads "no change at step 5, file appeared at step 10" also reads "step 5 was backgrounded and its job finished before step 10".
   `control` has no store, so nothing changes there.

6. **Capture is not changed for Exp 2; the capture-side option is recorded for a later corpus.**
   Setting `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, or raising `BASH_DEFAULT_TIMEOUT_MS`, through `--ae` would remove most of the lag at the source.
   It is not done for Exp 2 for three reasons: the rerun of first attempts (`tb40-sonnet-v3`) is already running under Harbor's defaults; it changes the agent, since a command over the timeout becomes a failure instead of a job and `run_in_background` disappears, which weakens the comparison with Exp 1 and with Harbor's stock scaffold; and hand-launched jobs would still lag.
   A checkpoint at job exit is not available through hooks; it would be the watcher polling the container's processes and committing with a new `Trigger` value tied to the launching call, a capture change with its own ADR and `corpus_id`.
   Either option, if taken for a later corpus, is an amendment to ADR-0011 with this ADR's lag distribution as its evidence.

7. **No LLM judge.**
   Every value above is a comparison of timestamps and two fixed text patterns.
   Whether the agent believed the write had happened is not needed for attribution; it remains the business of ADR-0015's text proxy for `false_edit_claim`.

## Consequences

- `SCHEMA_VERSION` 2 to 3; the `tb40-sonnet-v3` store and the Mac example stores are rebuilt from images, since ADR-0018's in-place `trajlab store derive` cannot add a column to `deltas`.
- `DERIVE_VERSION` changes with the two new views and with every consumer view that moves to the causal step; the preregistration records both ids.
- Tests stay offline: a synthetic layer entry with a set mtime for `layers` and `reconcile`, and one hand-made row per attribution case in the derive tests, including the two-window tie and the preserved-timestamp case.
- Validation, to be recorded below when run: the rule over the 40-trial corpus against the 104 hand cases, on the machine that holds that corpus's images, reporting how many the rule sends to the call the labeler identified, how many fall in `ambiguous` or `unknown`, how many disagree, and the lag distribution (median, p90; the maximum of 18 is known).
- What the paper must say: a checkpoint proves presence after a call, and cause is inferred by this rule with a stated agreement; the "Background writes" threat becomes a measured number; the record arms' timeline carries both steps.
- Clock: the container shares the host kernel clock on Linux, so mtimes compare with the JSONL timestamps there; on the Mac's Docker VM the offset is measured once before the rule is trusted on Mac-built stores.
- Open: the two text patterns; whether `background_by_path` should match the full path rather than the basename; whether a job window with no completion notice should end at the stop record's `t_start` or at the stop checkpoint's `captured_at`; how `background_untracked` behaves on a corpus without process snapshots (it still attributes when exactly one hand-launched job is open).

## Amendment (2026-10-27): validation on `tb40-sonnet-v3`, and the rule as implemented

### What was measured

The rule above was replayed before implementing it, on the 10 trials of `tb40-sonnet-v3` (69 first attempts, captured 2026-10-26 on the Linux server) whose steps hold a backgrounded call, with each delta row's write time read from the checkpoint images' top layers (`TarInfo.mtime`, whole seconds) and joined to the schema-2 store.
Corpus-wide, 15 of the 69 trials (11 of the 57 failures) ran a job past a call's return: 10 through Claude Code's timeout move (32 calls) and 14 by hand (`&`, `nohup`); the problem is concentrated in `layout-config-recreation`, `layout-config-recreation2`, `pretrain-shard-corruption`, and `sound-change-cascade`.
In the other 54 trials no process outlived its call, so the landing step is the causal step.

The two text patterns, fixed here:

- A backgrounded call's `steps.output` begins `Command did not complete within its 120s timeout and was moved to the background (ID: <task id>)` (`run_in_background` gives `Command running in background with ID: <task id>`; none in this corpus).
- The completion notice is a `queue-operation` event in the native session JSONL, `operation` `enqueue`, whose `content` is `<task-notification>` text carrying `<task-id>`, `<tool-use-id>` (the call's `tool_call_id`), and `<status>` (`completed` or `failed`).
  No user step ever carries it: every notice in the corpus was removed again as `absorbed_mid_turn` or dequeued, so `background_done_step` as worded in item 2 does not exist in the ATIF trajectory.
  The enqueue event's own timestamp is the job's end, within Claude Code's polling, and is what the job window ends at.

Replay of the seven cases over 23,064 delta rows of the 10 trials:

| Attribution | Rows |
|---|---|
| `foreground` | 20,728 |
| `background` | 826 |
| `background_untracked` | 52 |
| `unknown` | 1,458 |
| `ambiguous` | 0 |

878 late deltas, with lags of 1 to 37 calls (the 40-trial corpus's maximum of 18 does not bound this one), most of them Chromium profile files written by headless browsers that background scans spawned.

Where the rule is right: `layout-config-recreation2__bBNCaCw`'s deliverable `/app/output/config.json` landed at step 54 and the rule returns step 53, the backgrounded refine job; `sound-change-cascade__AjkhsMS`'s `/app/rules_greedy.json` landed at step 12 and the rule returns the backgrounded step 6.

Where it is wrong, three ways:

1. **Case 1 fires on a background writer.** `pretrain-shard-corruption__XK8w3KT` step 97 runs `nohup python climb2.py ... > log_$s.txt &` eight times; the logs get a line every few seconds; steps 98 and 100 are `sleep 115; tail -qn1 /tmp/w/log_1[1-8].txt`. At each commit the logs' times lie inside the sleeping call's own window, so case 1 says `foreground` of step 98 and 100: 16 rows confidently wrong, and 3 more at the stop record. The same shape is in `pretrain-shard-corruption__2pVLuf4` (steps 104, 107), `pretrain-shard-corruption__q34Z4ZX` (54, 66, 67), `layout-config-recreation__dzr9cbd` (64, 100), and `layout-config-recreation__VUc3qgg` (74): about 60 rows whose landing call is `sleep` and `cat` or `tail`, 1,324 non-cache rows over 11 trials by the looser count of rows landing on such calls.
2. **Case 5 gives up on two hand-launched jobs.** 1,458 `unknown` rows, 720 in `dzr9cbd` and 730 in `VUc3qgg`, where several scans ran at once. Taking the latest hand-launched call before the write resolves 1,452 of them, and was right in every case checked by hand (`VUc3qgg` 73 for the rows landing at 74; `q34Z4ZX` 57 for 59 and 82 for 90; `AjkhsMS` 17 for 19).
3. **Case 2's window absorbs other jobs' writes.** Windows run to 1,404 s. In `dzr9cbd`, `/tmp/g8.log` is attributed to the backgrounded step 160 although step 188 launched `g8.py` by hand into it, because case 2 is tested before case 5.

A detail the replay also settled: layer mtimes are whole seconds, and a commit lands 3 to 4 s after `t_end`, so a call's own last write can carry a time past `t_end` and fall out of case 1 as worded.

### Decision, amended

Items 1 to 3 are replaced by what landed on `main` with this amendment; items 0, 4 to 7 stand, with item 4's consumer change deferred as noted.

1. **Schema version 3.** `deltas` gains `mtime_before` and `mtime_after` (aware UTC, whole seconds, from the tar entries; `Entry.mtime` in `trajlab.store.layers`, never compared). `steps` gains `checkpoint_at`, when the image named by `checkpoint_seq` was committed, and `command_shell`, a Bash command with heredoc bodies and inline `-c` programs cut out (`trajlab.store.steps.shell_text`), since an `&` inside either is the program's, not a launch, and RE2 cannot strip them. A new agent-readable table `job_notices` (`trial_id`, `task_id`, `tool_call_id`, `status`, `noticed_at`) holds the completion notices, read from the session JSONL by `trajlab.atif.jobs` at build time. No store of another version is upgraded in place: the values are in the rows, so a schema-1 or schema-2 store is built again from its trial dirs and images, and the schema-1 upgrade path of ADR-0018 is removed.
2. **`step_jobs`.** Per tool call: `backgrounded` and `task_id` from the output pattern; `job_status`, `job_end` (the notice's time, else the attempt's last recorded instant), and `background_done_step` (the first step that began after `job_end`, kept as information) from `job_notices`; `launches_job` from `command_shell` matching an `&` separator, `nohup`, `setsid`, or `disown`.
3. **`delta_attribution`**, one ordered rule, `DERIVE_VERSION` 2. For a row landing at step k with write time m = `mtime_after` (null for a delete):
   - the landing window is [k.`t_start`, k.`checkpoint_at`] (else `t_end`), so the commit gap no longer drops a call's own write;
   - the candidate launchers are the earlier calls whose job was open at m: a backgrounded call whose window [`t_start`, `job_end`] holds m, or a hand-launched job started before m, which has no end on record;
   - among candidates the best is the one whose command redirects into the file (a target's basename after shell expansion, `$s` and `${s}` standing for any text, so `> log_$s.txt` redirects into `log_11.txt`), else one that names the file, else the latest launched; `launcher_step` names it;
   - `foreground`: m in the landing window and no candidate;
   - `background`, `background_by_path`, `background_untracked`: a candidate, and either m is outside the landing window, or the best launcher redirects into the file and the landing call writes nothing it names (no write tool, no `>` in its shell text, no mention of the file); `by_path` when the naming tie-break overrode the latest launcher, `untracked` for a hand-launched job;
   - `contested`: a candidate and m inside the landing window, otherwise; `launched_by_step` is null, `causal_step` the landing step, and `launcher_step` the alternative, for the windowed score;
   - `untrusted_mtime`: no candidate and m before the attempt's first step; `launched_by_step` is k when no job was launched before k, else null;
   - `unknown`: anything else, a delete included (its time is the parent directory's, which the store does not keep).
   `ambiguous` is gone: the ordering decides between launchers. `is_late` is `launched_by_step` < `step_idx`; `causal_step` is `coalesce(launched_by_step, step_idx)`.
4. **Consumers.** `causal_step` is in the view, but ADR-0018's `is_mutating`, `version`, `variant_of`, and `is_tail_probe`, and ADR-0016's timeline, still read the landing step; moving them is the next PR, with its own derived id.

The open questions of this ADR are answered: the basename, with shell variables as wildcards; the notice's own time; and, without process snapshots, the latest open launcher.

### What the rule still cannot do

A file that a background process keeps writing while a read-only call runs cannot be separated from a write by that call with timestamps and command text: the rule says `contested` when the landing call names the file, and `background` only when the job redirects into it. The exact signal is the identity of the writing process, recorded at capture time; ADR-0021 proposes it.

Validation of the rule as implemented, on the schema-3 rebuild of the corpus store, is recorded below when `scripts/2026-10-27_adr0020_attribution_report.py` has run on it.
