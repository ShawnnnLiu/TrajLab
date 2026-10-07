"""A plain-text brief of one trial for whoever labels it (ADR-0013, decision 5).

`brief` names the task package, the graded artifacts, the timeline, each failing check with its
message and what the timeline says about it, and how to try a fix. `calls` lists the agent's
tool calls with the timeline point that first holds each call's effects.
"""

import json
from datetime import datetime

from harbor.models.trial.paths import TrialPaths

from trajlab.groundtruth.checks import read_checks
from trajlab.groundtruth.counterfactual import artifact_roots, read_fixes
from trajlab.groundtruth.extract import TrialInputs, state_dir
from trajlab.groundtruth.gates import recorded_reward, trial_gates
from trajlab.groundtruth.items import trial_timeline

ARGUMENT_CHARS = 240


def _point_label(index: int, kind: str, seq: int | None) -> str:
    return f"P{index}" + (f" (checkpoint {seq})" if seq is not None else f" ({kind})")


def brief(inputs: TrialInputs) -> str:
    trial_dir = inputs.trial_dir
    reward, exception = recorded_reward(trial_dir)
    timeline = trial_timeline(trial_dir)
    points = timeline.points
    result = json.loads(TrialPaths(trial_dir).result_path.read_text())
    phase = result.get("verifier") or {}
    seconds = (
        (
            datetime.fromisoformat(phase["finished_at"])
            - datetime.fromisoformat(phase["started_at"])
        ).total_seconds()
        if phase.get("started_at") and phase.get("finished_at")
        else None
    )
    out = [
        f"Trial {trial_dir.name} ({inputs.task_name}): recorded reward {reward}"
        + (f", exception {exception}" if exception else ""),
        f"Trial dir: {trial_dir}",
        f"Verifier time in the original run: {seconds:.0f} s (a fix's regrade takes about as long)"
        if seconds is not None
        else "Verifier time in the original run: unknown",
    ]
    if inputs.task_dir is not None:
        out += [
            f"Task package: {inputs.task_dir}",
            "  instruction.md, tests/ (the verifier), solution/ (a reference solution)",
        ]
    out += [
        f"Trajectory: {TrialPaths(trial_dir).agent_dir / 'trajectory.json'}",
        "Graded artifacts (container paths): "
        + ", ".join(f"/{root}" for root in artifact_roots(inputs)),
        "",
        "Timeline (state = the graded artifacts' bytes; same letter = same bytes):",
    ]
    letters: dict[str, str] = {}
    for point in points:
        letter = letters.setdefault(point.state_id, chr(ord("A") + len(letters) % 26))
        calls = ", ".join(point.covered_tool_call_ids)
        out.append(
            f"  {_point_label(point.index, point.kind, point.seq):24} state {letter}"
            + (f"  calls: {calls}" if calls else "")
        )
    final_dir = state_dir(trial_dir, points[-1].state_id) / "artifacts"
    out += [
        "",
        f"Final graded files: {final_dir}",
        f"Recorded verifier output (full assertion messages): {TrialPaths(trial_dir).verifier_dir}",
        "",
    ]
    messages = {
        f"{c.kind}:{c.check}": c.message for c in read_checks(TrialPaths(trial_dir).verifier_dir)
    }
    rows = {t.key: t for t in timeline.timelines}
    out.append(f"Failing checks at the end ({len(timeline.final_failing)}):")
    for key in timeline.final_failing:
        verdict = timeline.verdicts[key]
        detail = verdict.verdict
        if verdict.verdict == "regression" and verdict.point is not None:
            point = points[verdict.point]
            detail += f": passed at P{verdict.last_pass}, fails from P{verdict.point}"
            if point.covered_tool_call_ids:
                detail += f" (calls {', '.join(point.covered_tool_call_ids)})"
        statuses = " ".join((s or "?")[0].upper() for s in rows[key].statuses)
        out.append(f"  {key}")
        out.append(f"    timeline {statuses}   [{detail}]")
        if messages.get(key):
            out.append(f"    message: {messages[key]}")
    gates = trial_gates(trial_dir)
    excluded = sorted(set(gates.flaky_checks) | set(gates.irreproducible_checks))
    if gates.gate2 == "pending":
        out += ["", "Gate 2 is pending (final-state replays not done): exclusions not known yet."]
    elif excluded:
        out += ["", "Excluded by gate 2 (flaky or irreproducible; do not label):"]
        out += [f"  {key}" for key in excluded]
    fixes = read_fixes(trial_dir)
    if fixes:
        out += ["", "Fixes tried so far:"]
        for fix in fixes:
            outcome = fix.error or f"fixed {list(fix.fixed)}, broke {list(fix.broken)}"
            out.append(f"  {fix.fix_id} ({fix.mode}): {outcome}")
    return "\n".join(out) + "\n"


def calls(inputs: TrialInputs) -> str:
    """The agent's tool calls in order, with the point that first holds each call's effects."""
    trial_dir = inputs.trial_dir
    point_of: dict[str, str] = {}
    for point in trial_timeline(trial_dir).points:
        for call in point.covered_tool_call_ids:
            point_of[call] = f"P{point.index}"
    path = TrialPaths(trial_dir).agent_dir / "trajectory.json"
    out = []
    for step in json.loads(path.read_text()).get("steps", []):
        for call in step.get("tool_calls") or []:
            arguments = json.dumps(call.get("arguments"), ensure_ascii=False)
            if len(arguments) > ARGUMENT_CHARS:
                arguments = arguments[:ARGUMENT_CHARS] + "..."
            where = point_of.get(call["tool_call_id"], "-")
            out.append(
                f"step {step['step_id']:>4}  {where:>5}  {call['tool_call_id']}  "
                f"{call['function_name']}  {arguments}"
            )
    return "\n".join(out) + "\n"
