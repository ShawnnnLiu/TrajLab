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
