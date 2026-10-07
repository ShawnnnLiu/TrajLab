"""Fidelity gates and flakiness (ADR-0013, decisions 2 and 3).

Gate 1: the last checkpoint's state equals the recorded final state, so the timeline loses no
write. Gate 2, per check: replaying the final state reproduces the recorded status. With the
original run and its replays as samples of the final state, a check whose replays disagree with
each other is `flaky`, and one whose replays agree with each other but not with the original run
is `irreproducible`; either is left out of the ground truth for that trial. A trial fails gate 2
only if its replays do not reproduce the recorded reward. Only replays with a verdict count.

For tasks whose verifier installs packages at grading time, the packages a replay installed are
compared with the original run's (`dependency_drift`).
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from harbor.models.trial.paths import TrialPaths

from trajlab.groundtruth.checks import read_checks, statuses
from trajlab.groundtruth.extract import points_path, read_points
from trajlab.groundtruth.replay import FINAL_SAMPLES, MAX_FINAL_ATTEMPTS, read_records
from trajlab.groundtruth.traits import traits

Gate = Literal["pass", "fail", "pending", "n/a"]
# What a grading-time install resolved: pip's list, and npm's package count without its timing.
_INSTALLED = re.compile(r"^(Successfully installed .*|added \d+ packages)", re.MULTILINE)


@dataclass
class TrialGates:
    trial_name: str
    task_name: str
    recorded_reward: float | None
    recorded_exception: str | None
    points: int = 0
    states: int = 0
    checkpoints: int = 0
    gate1: Gate = "pending"
    gate2: Gate = "pending"
    final_samples: int = 0
    replay_rewards: list[float | None] = field(default_factory=list)
    flaky_checks: list[str] = field(default_factory=list)
    irreproducible_checks: list[str] = field(default_factory=list)
    dependency_drift: bool | None = None
    replays_without_verdict: int = 0


def recorded_reward(trial_dir: Path) -> tuple[float | None, str | None]:
    result = json.loads(TrialPaths(trial_dir).result_path.read_text())
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward = rewards.get("reward")
    exception = (result.get("exception_info") or {}).get("exception_type")
    return (float(reward) if reward is not None else None), exception


def installed(stdout: Path) -> list[str]:
    if not stdout.is_file():
        return []
    return sorted(_INSTALLED.findall(stdout.read_text(errors="replace")))


def excluded_checks(trial_dir: Path) -> set[str]:
    """Checks gate 2 leaves out of this trial's ground truth (flaky or irreproducible)."""
    gates = trial_gates(trial_dir)
    return set(gates.flaky_checks) | set(gates.irreproducible_checks)


def trial_gates(trial_dir: Path) -> TrialGates:
    reward, exception = recorded_reward(trial_dir)
    task_name = json.loads(TrialPaths(trial_dir).config_path.read_text())["task"]["name"]
    gates = TrialGates(trial_dir.name, task_name, reward, exception)
    if not points_path(trial_dir).is_file():
        return gates
    points = read_points(trial_dir)
    gates.points = len(points)
    gates.states = len({p.state_id for p in points})
    checkpoints = [p for p in points if p.kind == "checkpoint"]
    gates.checkpoints = len(checkpoints)
    if not checkpoints:
        gates.gate1 = "n/a"
    else:
        gates.gate1 = "pass" if checkpoints[-1].state_id == points[-1].state_id else "fail"

    final_id = points[-1].state_id
    records = [r for r in read_records(trial_dir) if r.purpose != "counterfactual"]
    gates.replays_without_verdict = sum(1 for r in records if r.outcome != "verdict")
    finals = [
        r
        for r in records
        if r.state_id == final_id and r.purpose in ("final", "repeat") and r.outcome == "verdict"
    ]
    gates.final_samples = len(finals)
    if len(finals) < FINAL_SAMPLES:
        attempts = sum(
            1
            for r in records
            if r.state_id == final_id and r.purpose == "final" and r.outcome != "infra"
        )
        if attempts >= MAX_FINAL_ATTEMPTS:  # the recorded verdict cannot be reproduced
            gates.gate2 = "fail"
        return gates
    recorded = statuses(read_checks(TrialPaths(trial_dir).verifier_dir))
    samples = [statuses(r.checks) for r in finals]
    gates.replay_rewards = [r.reward for r in finals]
    for key in sorted(set(recorded).union(*samples)):
        replayed = {s.get(key) for s in samples}
        if len(replayed) > 1:
            gates.flaky_checks.append(key)
        elif recorded.get(key) not in replayed:
            gates.irreproducible_checks.append(key)
    gates.gate2 = "pass" if all(r == reward for r in gates.replay_rewards) else "fail"
    if traits(task_name).network:
        original = installed(TrialPaths(trial_dir).verifier_dir / "test-stdout.txt")
        replays_dir = trial_dir / "groundtruth" / "replays"
        drift = [
            installed(TrialPaths(replays_dir / r.replay_id).verifier_dir / "test-stdout.txt")
            != original
            for r in finals
        ]
        gates.dependency_drift = any(drift)
    return gates


def job_report(trial_dirs: list[Path]) -> dict[str, object]:
    rows = [trial_gates(d) for d in trial_dirs]
    totals: dict[str, object] = {
        "trials": len(rows),
        "points": sum(r.points for r in rows),
        "states": sum(r.states for r in rows),
        "gate1": {g: sum(r.gate1 == g for r in rows) for g in ("pass", "fail", "n/a", "pending")},
        "gate2": {g: sum(r.gate2 == g for r in rows) for g in ("pass", "fail", "pending")},
        "flaky_checks": sum(len(r.flaky_checks) for r in rows),
        "irreproducible_checks": sum(len(r.irreproducible_checks) for r in rows),
        "trials_with_dependency_drift": sum(bool(r.dependency_drift) for r in rows),
        "replays_without_verdict": sum(r.replays_without_verdict for r in rows),
    }
    return {"totals": totals, "trials": [asdict(r) for r in rows]}
