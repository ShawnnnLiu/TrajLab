# Terminal-Bench version for the trajlab corpus: current recommendation

Date: 2026-09-24.
Status: recommendation only, not a decision.
Per the repo rules, the actual choice needs an ADR in `docs/decisions/` and a `corpus_id` before the first corpus run.
All factual claims below are sourced in `2026-09-24_tb-version-survey.md`; this document only arranges them around the decision.

## The decision

Which Terminal-Bench version supplies the task set for the trajectory-capture corpus.
Planned setup as of this date: Claude Code harness on Harbor, Claude subscriptions/tokens, frontier Claude models, papers targeted for end of 2026.

## What the options offer

The survey's headline: of 56 version calls across 52 primary sources, TB 2.0 has 29, TB 2.1 has 9, TB 4.0 has 3, TB 3.0 has 1, TB 1.0 has 11.
Zero academic papers evaluate on 3.0 or 4.0.

**TB 2.0 (89 tasks, Nov 2025).**
Largest comparable corpus by far (16 model reports, 13 papers), and the only version described by a peer-reviewed paper (ICLR 2026).
Concerns: 28 of its 89 tasks are documented as broken (dependency drift, resource mismatches, instruction/test misalignment), which for a trajectory corpus means some "failures" would be benchmark bugs recorded as agent errors; recent papers are moving off it for exactly that reason (T1: 2.0's "instabilities hindered reproducible evaluation").

**TB 2.1 (89 tasks, May 2026).**
Same task set as 2.0 with the 28 broken tasks repaired; released as Harbor Hub dataset `terminal-bench/terminal-bench-2-1` with its own frozen leaderboard.
Chosen by the most recent RL/agent papers (T1, StateM, Tmax, 2607.12227) and by mid-2026 lab reports (Fable 5 card 84.3%, Grok 4.5, GLM-5.3, Tencent Hy4).
GLM-5.3 sets precedent for running TB 2.1 under the Claude Code harness specifically.
Task timeouts are the per-task 2.0-era ones (900 s / 1800 s tiers), which keeps trajectory length and checkpoint storage bounded for the docker_commit design.
Concerns: (a) frontier saturation - Fable 5 is at 84.3% and the 2.1 board's top entries are 78-83%, so a corpus run with a frontier Claude yields roughly 15-20% failure trajectories, concentrated in the 30-task hard tier; (b) 2.1 scores are not comparable to the much larger body of 2.0 numbers (the repairs inflate scores ~10+ points); (c) by end of 2026 it will be two-plus versions behind the live leaderboard, and the fast release cadence (2.0 Nov 2025, 2.1 May, 3.0 Jul, 4.0 Aug, 4.1/5.0 on the roadmap) suggests further drift; (d) confirmed from the card PDF (2026-09-24): Anthropic's 84.3% was run on the mini-SWE-agent harness at high effort (they abandoned Terminus-2 for its 2.7x timeout rate), so a Claude Code run is comparable to it only across a harness gap worth several points; the nearest Claude-Code-harness comparables on 2.1 are the frozen leaderboard entries (Claude Code / Opus 4.8 at 78.9%), and the only Fable number produced under Claude Code is on TB 4.0, not 2.1.

**TB 3.0 (74 tasks, Jul 2026).**
Effectively skipped by the field: one lab report (GLM-5.3, 28.3 avg@3), zero papers, superseded after five weeks.
The September audit paper additionally certified 78 tasks of the TB-3 production record as unsolved and questioned item validity.
No comparison set on either axis; not a contender.

**TB 4.0 (66 tasks, Aug 2026, current main).**
The live leaderboard version, used by the three newest lab reports (Fable 5.1 card, Opus 5.5, Grok 4.7); SOTA ~58%, so a frontier run yields a roughly balanced success/failure mix and improvement claims have headroom.
Concerns: (a) zero academic comparables, so paper-to-paper comparison is impossible today; (b) flat 8-hour agent timeout and GPU-requiring tasks, which multiplies run cost and checkpoint storage (per-tool-call docker_commit on multi-hour trajectories) and complicates the capture stack; (c) 5 of 66 tasks are arm64-broken on main (matters only for local Mac runs; `DOCKER_DEFAULT_PLATFORM=linux/amd64` works); (d) leaderboard numbers since 2026-04-19 are judge-filtered (ATIF required, reward-hacking zeroed), so self-run numbers are not exactly leaderboard-comparable either.

## Cross-cutting facts that apply regardless of version

- Harness effects (~6-10 points) and attempt-count differences dominate small score deltas; any comparison we publish must state harness, attempt count, and aggregation explicitly.
- The leaderboard protocol is k=5 mean resolution rate; Harbor's default is `n_attempts=1`, so the job config must set attempts deliberately.
- The Hub dataset ref must be pinned exactly in `configs/harbor/` (dataset name + version), since 2.1 demonstrates that task content can change without a git tag.
- The tbench.ai leaderboard itself mandates ATIF trajectories for passing trials, which is the format trajlab captures; useful as motivation, whichever version is chosen.

## Current recommendation (held lightly)

**TB 2.1 as the primary corpus.**
Motivation, in order of weight:

1. Corpus quality: the 28 repaired tasks remove a known source of spurious failure trajectories, which matters more for a capture corpus than for a score claim.
2. Comparability where it counts for an end-of-2026 paper: the recent-paper cohort (T1, StateM, Tmax) and mid-2026 frontier claims (Fable 5 at 84.3%) are on 2.1, and the frozen 2.1 leaderboard (top entries 78-83%, including Claude Code entries) persists as an anchor even after the live board moves on.
3. Operational fit: per-task timeouts keep docker_commit checkpoint chains tractable, unlike 4.0's flat 8-hour timeout and GPU tasks; GLM-5.3 provides precedent for the exact Claude Code + TB 2.1 pairing.

Honest counterweights, stated so the ADR can rebut or accept them:

- If any paper claim becomes "our method improves the agent", 2.1's saturation (frontier models at 80%+) leaves little headroom and TB 4.0's ~58% SOTA would serve that claim better.
- The version will look dated by review time; mitigate by pinning the dataset ref, citing the ICLR 2026 paper plus the 2.1 release note, and saying explicitly that 4.0 has no academic comparables.
- Failure-trajectory count at ~85% solve rate may be thin; mitigate by weighting attempts toward the official 30-task hard tier, and keep a small TB 4.0 hard-subset secondary corpus (own `corpus_id`) as an option if failures come up short.

Open items before writing the ADR:

1. RESOLVED 2026-09-24: the Fable 5 card (§8.3) was read directly; the 84.3% TB 2.1 number was produced with the mini-SWE-agent harness at high effort (5 attempts x 89 tasks), not Terminus-2 and not Claude Code.
   Consequence for the ADR: with a Claude Code harness, the direct comparables on 2.1 are the frozen leaderboard's Claude Code entries; the Fable 5 card number is comparable only with a stated harness caveat.
2. Confirm the pinned `terminal-bench/terminal-bench-2-1` dataset version resolvable through our pinned Harbor, and record it in the job config.
3. Decide the attempt protocol (k and aggregation) against the comparison target: k=5 mean matches the leaderboard; avg@3 matches GLM-5.3's 3.0 protocol; several 2.1 papers use 5 trials/task.
4. Estimate expected failure-trajectory count from a small pilot (hard-tier tasks first) before fixing the task weighting.
