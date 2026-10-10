# ADR-0012: The repair experiment on TB 4.0

Status: proposed (2026-10-02).

## Context

The overnight pilot (`docs/research/2026-10-02_tb40-repair-pilot.md`) repaired 8 failed Sonnet 5.5 trials once under four arms and found the design's problems: too few repairable failures, n = 1 per cell, no measured task pass rate, and a launcher that lived in a dated script.
The run now moves to a Linux server (8 cores, 30 GB, 479 GB disk, native amd64, Docker 29.8.2 with the containerd image store), with a 48-hour budget for the whole run.
The question is unchanged: does the failed trial's environment, its trajectory, or both help a repair agent of the same model?

## Decision

0. **This is Experiment 1, and its checkpoints are `docker commit` only.** Every checkpoint is a filesystem snapshot of the trial container (ADR-0004); no CRIU, so no process, memory, or shell state is captured or restored, and neither StateFork nor Waypoint is used.
   A `state` or `state-traj` repair therefore starts from the failed trial's files, not its running processes: anything the failed trial left running (a server, a background job) is gone.
   A later experiment with CRIU-based state needs its own ADR superseding ADR-0004 and new corpus ids.
1. **Benchmark:** Terminal-Bench 4.0 (`terminal-bench/terminal-bench@4.0.0`). This settles the version for this experiment; the TB 2.1 recommendation of 2026-09-24 stays a recommendation for any other corpus.
2. **Tasks:** the 23 TB 4.0 tasks with `expert_time_estimate_hours` from 1.5 to 4, no GPU, a single container, at most 4 CPUs and 8 GB declared (`configs/harbor/tb40-sonnet-v2.json`). Compressed environment images are 0.04 to 3.6 GB on amd64.
3. **First attempts:** 3 per task (`n_attempts: 3`), Claude Sonnet 5.5 at medium effort, Claude Code 2.1.278 (ADR-0009), agent cap 4 h (`agent_timeout_multiplier: 0.5` of TB 4.0's 8 h), captured with `trajlab watch --every 1 --gate change` on the pre-installed image (ADR-0008, ADR-0010, ADR-0011).
   A task's first-attempt pass rate (0, 1, 2, or 3 of 3) is recorded and used to stratify results.
4. **Failure types:** every finished trial that did not pass is classified from its `result.json` alone (`trajlab.capture.repair_launcher.classify_failure`, `FailureKind` in `contracts`):
   `ended_turn` (no exception, reward below 1), `timeout` (`AgentTimeoutError`), `agent_error` (output or context limits, refusal), `infra_error` (API, usage limit, environment, verifier, cancellation), or `unclassified` (anything else, including `NonZeroAgentExitCodeError`, which Claude Code raises for both its own and harness failures).
   Only `ended_turn`, `timeout`, and `agent_error` are repaired; `infra_error` trials are not attempts and are rerun with `harbor jobs resume`.
5. **Which failures are repaired:** every repairable failed trial, not one chosen per task, so no selection favors an arm.
   A failure is repaired only if all four arms can start (a final checkpoint whose image exists, exactly one native session); otherwise no arm runs, so arms stay paired.
6. **Arms:** `fresh`, `state`, `state-traj`, `traj`, as in the pilot; every arm runs `RepairClaudeCode` with the same note; same model, effort, Claude Code version, and 4 h cap as the first attempt.
   The final checkpoint is the failed trial's highest-seq checkpoint (its stop checkpoint when the final state was new, ADR-0011).
7. **Repairs:** 3 repair trials per failure per arm (`n_attempts: 3` in each repair job, `<prefix>-<trial>-<arm>`), run by `trajlab repair` as source trials finish, at most 6 trials at once across all jobs. Each repair job's provenance is a `RepairSource` record in `corpus/jobs/_repair-inputs/<job>/repair-source.json`.
8. **Metrics, fixed before the run:**
   - Primary: per failure, the repair resolution rate of each arm, the mean reward over its 3 repairs; compared with `fresh` on the same failures.
   - Primary test: per arm against `fresh`, a paired comparison of per-failure rates (Wilcoxon signed-rank), with failures clustered by task (cluster bootstrap for the interval); secondary, exact McNemar on pass@3 per failure.
   - Strata: task first-attempt pass rate (0/3, 1/3, 2/3) and failure type.
   - Secondary: pass@3 per failure; change in tests failing against the source trial, including the regression rate; agent and wall seconds; the repair's own tokens by kind and their cost at list prices (messages whose `uuid` is not in the source session, as in the pilot); cost per resolved failure; API and tool calls; the share of repairs that end their turn within 3 minutes.
   - Cost of the state arms: the source trial's capture overhead (hook hold time) and checkpoint storage.
   - Reported, not compared: infra-excluded and unclassified trial counts.

## Consequences

- At about half the first attempts failing, the run is about 69 first-attempt and 400 repair trials; at 6 at once it fits 48 hours only if the mean trial takes under about 37 minutes. Timeouts at the 4 h cap are the risk; if they dominate the first attempts, cut tasks rather than shorten the cap for some arms.
- `fresh` is the control for "another attempt": on a task the model passes 2 of 3 times, `fresh` alone should pass about two-thirds of repairs.
- Claims are per stratum; a pooled rate mixes tasks the model can and cannot solve.
- Subscription usage limits may pause the run; `trajlab repair` pauses launches for 30 minutes after an `ApiUsageLimitError` and reruns the affected trials, so a limit costs time, not data.
- The pilot's confounds remain and are reported: the note's meaning differs across arms, and resumed arms see Harbor's re-sent instruction and Claude Code's notices.
- `scripts/2026-10-02_repair_arms.py` is superseded by `trajlab repair`; the pilot's corpora keep pointing at it.
- New corpus ids: `tb40-sonnet-v2` for first attempts, `tb40-repair-v2-<trial>-<arm>` for repairs.
- Job dirs live in the shared folder `/srv/trajlab/jobs` on the Linux server (`corpus/jobs` is a symlink to it); every manifest of this experiment records it as `storage`.

## Amendment (2026-10-02, 21:50): one failure per task, pruned repair checkpoints

After 33 of 69 first attempts, 27 had failed (6 passed), against the planned half. Repairing every failure would have been about 720 repair trials, beyond the 48-hour budget, and at about 1.6 GB of checkpoint images per trial, beyond the disk.

1. **One failure per task** (`trajlab repair --per-task 1`), replacing decision 5's "every repairable failed trial". Once all 3 of a task's attempts have ended, one of its repairable failures is drawn at random, seeded by `<prefix>:<task>`; the candidates, the draw, and why other trials were not candidates are in `_repair-inputs/<prefix>.selection.json`. Drawing only after every attempt ends keeps the choice from favoring fast failures. Tasks with no failure contribute no repair. About 20 failures x 4 arms x 3 repairs, about 240 repair trials.
2. **Repair checkpoints are pruned** to each trial's final checkpoint image once its job finishes (`--prune`, the default); every `CheckpointRecord` stays, and `agent/checkpoints/pruned.json` lists the removed images. First-attempt checkpoints are all kept: they are the corpus.
3. The unit of analysis becomes the task (one failure each); per-task 3-repair means, compared with `fresh`, are unchanged as the primary metric.
4. The launcher started on 2026-10-02 at 19:47 queued every failure; it had launched no repair before it was restarted with these options, so no repair ran under the old rule.

## Amendment (2026-10-03): three additions after round 1; to be built once the round 1 runs finish

Nothing below is implemented yet. The round 1 runs (`tb40-repair-v2`, decisions 0-8 and the 2026-10-02 amendment) continue unchanged; these are built after they finish.

### Context: round 1 so far

Interim counts as of 2026-10-03 00:43, runs still going (repair trials passed / repair trials finished):

| `fresh` | `state` | `state-traj` | `traj` |
|---|---|---|---|
| 1/33 | 4/35 | 7/33 | 10/33 |

The ordering so far: the prior conversation helps, and starting from the failed state hurts once the conversation is there (`traj` 10 vs `state-traj` 7); without a conversation, the failed state beats a restart (`state` 4 vs `fresh` 1), on tasks with partial work worth keeping. Small n: a direction, not a significant result.

This is the opposite of Recovery-Bench (letta-ai/recovery-bench), where giving the full history made recovery worst. Two differences could explain it, and round 1 cannot separate them:
1. **Delivery.** Recovery-Bench puts a transcript in the prompt; `traj` resumes the native conversation (`claude --resume`).
2. **Hidden reasoning.** A resumed conversation passes the model's own earlier thinking blocks back to the API (they are encrypted in the session file, but sent), which no transcript can contain.

Six tasks are 0/3 in every arm so far, mostly from first attempts that ended their turn in under a minute.

### Decision

1. **New arm `traj-text`: the history as a plain transcript.**
   - Task image, new conversation, `RepairClaudeCode` with the same note; after the note and before the instruction, the failed trial rendered as text: the instruction, then per agent step the agent's message, its tool calls, and their outputs.
   - Rendered from Harbor's `agent/trajectory.json` (`harbor.models.trajectories`), never from thinking blocks, which are encrypted.
   - Each tool output is truncated by one fixed rule: its first and last 2,000 characters, with a marker giving the number of characters cut. The rule, and per failure the rendered size and the number of outputs cut, go in the `RepairSource` record and the corpus manifest.
   - The transcript is in the prompt, not a file in the environment, so the treatment is "the agent has read it", as in Recovery-Bench.
   - Same failures as round 1 (the draw in `_repair-inputs/tb40-repair-v2.selection.json`), 3 repairs each, so `traj-text` pairs with the existing arms. About 66 repair trials.
   - No `state-traj-text` arm: round 1 already answers fresh versus state.
   - **What `traj` has that `traj-text` lacks.** Both carry the same failed attempt; they differ in how it is delivered, and the difference is not only format. `traj` resumes the native conversation, so the repair agent gets (a) its own earlier reasoning, as thinking blocks sent back to the API, which a transcript cannot contain; (b) every tool output in full, where `traj-text` truncates; and (c) the history as its own multi-turn conversation, its assistant turns and tool results, where `traj-text` gets one user message describing someone's attempt. These three together are the treatment "conversation versus document"; the arms cannot separate them, and results are reported as that bundle, not as format alone.
   - Reading: if `traj-text` matches `traj`, the Recovery-Bench disagreement is about tasks, model, or harness; if it falls toward `fresh`, delivery as a conversation matters, through one or more of (a) to (c).
2. **New arm `traj-ckpt`: the checkpoints as a tool, on a fresh environment.** This is the headline checkpoint arm, compared with `traj`.
   - Task image, the failed trial's native session resumed (as `traj`), plus a read-only tool to inspect the failed trial's checkpoints (all of them, which ADR-0012's amendment keeps for first attempts).
   - The tool inspects; it does not set the starting state. Round 1 says starting from the failed state costs repairs, so the planned "resume-state + conversation + checkpoint tool" arm is demoted to secondary: `state-traj-ckpt`, run only if budget allows.
   - The note is unchanged; the tool is announced only by its own description, so `traj-ckpt` differs from `traj` only by the tool being available. Whether and how often the agent calls it is recorded as a secondary outcome.
3. **Round 2: a repair protocol, after the two arms above.**
   - Every arm gets the same protocol in place of round 1's one-line note: diagnose why the previous attempt failed and write the diagnosis down before editing; then a fixed gather, fix, verify loop; do not end the turn without running a check. (Self-Debugging, RepairAgent, FailForge's diagnosis pass.)
   - Round 1 stays as the naive-note baseline; round 2 is a new corpus, `tb40-repair-v3-<trial>-<arm>`, on the same failures.
   - The written diagnosis is recorded as a second outcome, so arms can be told apart even where repairs stay at zero. Capture records it; how it is scored is analysis and is decided before round 2 starts, not here.

### Before building

- Read three short trials from the all-zero tasks and write down why each stopped (input to decision 3).
- Read `recovery_mixin.py` in letta-ai/recovery-bench to confirm the transcript goes in the prompt and how they truncate (input to decision 1).
- Design the checkpoint tool's interface. Checkpoint images live in the host's Docker; the trial container cannot reach them, so the tool needs a channel (a host-side server, or data exported into the container). Its interface and what it exposes need their own section here before `traj-ckpt` runs.

### Consequences

- `RepairArm` in `contracts/repair.py` gains `traj-text`, `traj-ckpt`, and, if run, `state-traj-ckpt`; the arm sets that decide inputs (`CHECKPOINT_ARMS`, `SESSION_ARMS`) gain a transcript set and a tool set. New arm names give new corpus ids under the existing `tb40-repair-v2-<trial>-<arm>` pattern.
- **Prompt size limit.** Harbor passes the instruction as one `docker exec -e` argument (`harbor/environments/docker/docker.py`, `exec`), which Linux caps at 128 KiB per argument (`MAX_ARG_STRLEN`). A rough render of the 21 chosen round 1 failures with the 2,000-character rule (2026-10-03, not the final format) gives 8 to 148 KB: 2 over the cap (vba-userform-port, pretrain-shard-corruption) and 2 more within a few KB of it (sound-change-cascade, layout-config-recreation2), before the note and instruction are added. The longest failures are the ones the arm most needs, so dropping them or tightening the rule for them only is not allowed. The prompt is delivered by our `RepairClaudeCode` subclass instead of through Harbor's argument, and the limit is recorded in `docs/upstream-notes.md`.
- **`traj` versus `traj-text` is a bundle, stated in the paper as such:** reasoning (cannot be removed), truncation (the per-failure count of cut outputs allows a check on failures with none cut), and conversation form (decision 1). The resumed arms also keep ADR-0012's confounds (Harbor's re-sent instruction, Claude Code's notices).
- If the source session was compacted, `traj` resumes the compacted context while `traj-text` renders the full trajectory; such failures are flagged in the record.
- Budget: `traj-text` about 66 trials of short duration; `traj-ckpt` about 66 trials at round 1 durations; round 2 repeats every arm it includes on the same failures, so its arm list is fixed when it is launched, against the remaining budget.
- Oct 9 exhibit: repair rate by arm, round 1's four arms plus `traj-text` and `traj-ckpt` as they land; caveats on the slide: n of about 33 per arm, and the Recovery-Bench contrast is confounded by hidden reasoning even after `traj-text`.

## Amendment (2026-10-08): repair checkpoint images deleted

On 2026-10-08 every remaining checkpoint image of the repair jobs was deleted from the capture server to free disk: the round 1 finals of `tb40-repair-v2-*` (`fresh`, `state`, `state-traj`, `traj`, `traj-text`) and the withdrawn round 2 (`tb40-repair-v3-*`), 593 images in all.
Every `CheckpointRecord` stays; each affected trial's `agent/checkpoints/pruned.json` lists its removed images, with `kept_seq: null` and the reason, in the same shape the launcher's prune writes.
The 917 first-attempt images of `tb40-sonnet-v2` are untouched: they are the corpus, and ground truth (ADR-0013) reads them for `try-fix` in environment mode and for blame.
