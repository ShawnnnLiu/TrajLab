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
