"""The ground-truth dataset manifest (ADR-0013): sources, settings, and counts."""

import importlib.metadata
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from trajlab.contracts.groundtruth import GroundTruthManifest
from trajlab.groundtruth.admission import CPU_CAPACITY, MEMORY_CAPACITY_MB, QUIET_COMPANY_CPUS
from trajlab.groundtruth.counterfactual import read_fixes
from trajlab.groundtruth.extract import TrialInputs, points_path, read_points
from trajlab.groundtruth.gates import trial_gates
from trajlab.groundtruth.items import labels_path, read_items
from trajlab.groundtruth.replay import read_records


def build_manifest(
    dataset_id: str,
    source_corpus_id: str,
    job_dir: Path,
    inputs: list[TrialInputs],
    *,
    repo_sha: str,
    repo_dirty: bool,
    labelers: tuple[str, ...],
    storage: str | None,
) -> GroundTruthManifest:
    outcomes: Counter[str] = Counter()
    purposes: Counter[str] = Counter()
    gate1: Counter[str] = Counter()
    gate2: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    images: dict[str, str] = {}
    points = states = excluded = fixes = failed = 0
    for one in inputs:
        trial_dir = one.trial_dir
        if one.task_dir is not None and one.task_name not in images:
            config = tomllib.loads((one.task_dir / "task.toml").read_text())
            image = ((config.get("verifier") or {}).get("environment") or {}).get("docker_image")
            images[one.task_name] = image or ""
        gates = trial_gates(trial_dir)
        failed += gates.recorded_reward != 1.0
        gate1[gates.gate1] += 1
        gate2[gates.gate2] += 1
        excluded += len(gates.flaky_checks) + len(gates.irreproducible_checks)
        if points_path(trial_dir).is_file():
            found = read_points(trial_dir)
            points += len(found)
            states += len({p.state_id for p in found})
        for record in read_records(trial_dir):
            outcomes[record.outcome] += 1
            purposes[record.purpose] += 1
        fixes += len(read_fixes(trial_dir))
        if labels_path(trial_dir).is_file() or read_items(trial_dir):
            for item in read_items(trial_dir):
                kinds[item.kind] += 1
                methods[item.method] += 1
    return GroundTruthManifest(
        dataset_id=dataset_id,
        created_at=datetime.now(UTC),
        source_corpus_id=source_corpus_id,
        source_job=job_dir.resolve().name,
        storage=storage,
        harbor_version=importlib.metadata.version("harbor"),
        repo_sha=repo_sha,
        repo_dirty=repo_dirty,
        labelers=labelers,
        verifier_images=dict(sorted(images.items())),
        admission={
            "cpu_capacity": CPU_CAPACITY,
            "memory_capacity_mb": float(MEMORY_CAPACITY_MB),
            "quiet_company_cpus": QUIET_COMPANY_CPUS,
        },
        trials=len(inputs),
        failed_trials=failed,
        points=points,
        states=states,
        replays_by_outcome=dict(sorted(outcomes.items())),
        replays_by_purpose=dict(sorted(purposes.items())),
        gate1=dict(sorted(gate1.items())),
        gate2=dict(sorted(gate2.items())),
        excluded_checks=excluded,
        fixes=fixes,
        items_by_kind=dict(sorted(kinds.items())),
        items_by_method=dict(sorted(methods.items())),
    )
