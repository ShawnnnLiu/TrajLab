# Motivation for the Exp 2 build: why the store, the engine, and the three arms

Written 2026-10-18. Companion to `exp2_technical_spec_trajlab_2026-10-18.md` (what to build) and `exp2_design_spec_2026-10-18.md` (the experiment). This is the why. Read it before either, so local decisions go the right way.

## The premise

Agent trajectories live in token space: prompt, reasoning, tool calls, tool output. That record is not enough to debug an agent whose job is to change an environment, because the consequence of each action is in the environment, not in the text. The information is in the state deltas: what changed, when, and because of which action. (Eugene Wu, 10-15 meeting. His Sep 2026 keynote makes the stronger version: filesystem-only checkpoints lose process state.)

What #16 is building is a store that records each action with its state delta and its check outcome, and an engine that makes querying that record fast, so that an agent or an analyst finds where a run broke by querying instead of reading.

## What exists and what it lacks

TrajectoryDB (arXiv 2609.07782, Harvard, Sep 2026, still v1) argues trajectories are a data type that needs its own store. It gives a data model (session, turn, step, message, plus config and telemetry), six use cases, requirements (sequence retrieval over ordered steps, LLM judgments as UDFs inside relational plans, lineage for every semantic judgment) and seven system challenges. It has no system, no query language, no evaluation, and no environment state: its environment is tool-result text.

Trajlab already captures what it lacks: a filesystem checkpoint after every mutating tool call, joined to the trajectory step on `tool_call_id`. The store in the technical spec is the join of TrajectoryDB's token side and the state side, on the step. TrajectoryDB is the spec and the related-work anchor; #16 adds environment state and a working query layer.

## Why Exp 2 and not Exp 1

Exp 1 handed the repair agent raw state (the failed filesystem) or raw history (the transcript as text). Neither is the premise, and neither helped: fresh start 11.5%, failed filesystem alone 7.9%, flattened transcript 4.8%, resuming the agent's own session 27.0% (21 failures x 5 arms x 3 repairs; arms differ at p ~ 0.006, no pair survives correction). The form of the record was the variable. The arm that hands the agent state joined to actions, queryable, was never built.

Exp 2 builds it, with two controls: no record, and the same record as files without the engine. The second control is what lets the paper say anything about the engine rather than about the data. Exp 1 stays in the paper as motivation only.

## What the course brief measures

An analysis system that answers questions about agent histories with less effort than ad hoc workflows. In Exp 2 the analyst is the repair agent, arm 2 is the ad hoc workflow, and effort is tokens, tool calls and wall-clock to the fix.

## Design rules that follow

1. **The operator behind a derived column "does not have to be semantic, whatever is best"** (Eugene). A derived column is an interface. Exp 2's implementations are deterministic; an LLM implementation is a later registration, not a schema change.
2. **The record is an artifact the agent inspects, never text pasted into its context.** Pasted history anchors the agent on the prior attempt's reasoning (Exp 1 v2 spec, §Motivation: Recovery-Bench, Restart-Smart).
3. **Use case first.** Exp 2's use case is repair after failure on Terminal-Bench, because the ground truth already exists: verifier replay on `sl/groundtruth-replay`, 76 fault items covering 202 failing checks. That ground truth scores the experiment and is never shown to the agent.
4. **Information equality between arms 2 and 3 is the experiment.** Anything that gives arm 3 more information than arm 2 invalidates the comparison.
5. **Binary failure signal.** The agent learns only that the previous attempt FAILED. No check names, no verifier output. This is Terminal-Bench's native setting, needs no discretion about which tests to show, and is the strongest test of the premise.
6. **Filesystem-only state is a stated limitation, not a blocker.** Process and shell state (Waypoint) is the follow-up, and the schema must not make it harder to add.

## What a positive and a negative result each mean

- Arm 3 beats arm 2 on repair rate, or matches it at lower cost: the engine matters, not only the data.
- Arm 3 equals arm 2 on both: an agent that can write code builds its own engine, and the contribution is the data model, not the query layer. Still a result.
- Arms 2 and 3 both equal control: the record as deltas does not help repair, and the analysis story must be carried by the analyst-side use case instead. Report it.

## Sources

- `direction_trajectorydb_vs_wu_2026-10-16.md`
- `meeting_notes_eugene_wu_2026-10-15.md`
- `prep_trajectorydb_trail_team_explainer.md` (TrajectoryDB and TRAIL, full-text read 09-20)
- `exp1_round1_full_results_2026-10-06.md` (Exp 1 numbers)
- `exp1_repair_protocol_v2_spec_2026-10-02.md` §Motivation (literature behind the protocol prompt and the anchoring rule)
- [TrajectoryDB, arXiv 2609.07782](https://arxiv.org/abs/2609.07782)
- [Early Thoughts on Agentic Data Environments, Wu, Sep 2026](https://eugenewu.net/talks/2026-09-agentic-data-envs.html)
