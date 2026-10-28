"""Exp 2's retry-loop models (`trajlab.contracts.exp2`): what each one refuses, and round trips."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from trajlab.contracts import (
    ArmConfig,
    AttemptLaunch,
    AttemptResult,
    Exp2Settings,
    IngestResult,
    QueryLogEntry,
    RootFailure,
)
from trajlab.contracts import exp2 as exp2_contracts
from trajlab.exp2.arms import arm_config

ROOT = UUID("e1eee115-a864-4c21-896b-5b60c8741d97")
T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
SHA = "a" * 64
COMMIT = "c" * 40


def attempt(attempt_idx: int = 1, *, arm: str = "record-files", **overrides: Any) -> AttemptResult:
    outcome = overrides.get("outcome", "failed")
    fields: dict[str, Any] = {
        "run": "tb40-exp2-dry1",
        "arm": arm,
        "root_trial": "hello-world__K3GBok3",
        "root_trial_id": ROOT,
        "task": "hello-world/hello-world",
        "attempt_idx": attempt_idx,
        "parent_trial_id": ROOT,
        "job_name": f"tb40-exp2-dry1-hello-world__K3GBok3-{arm}-a{attempt_idx}",
        "outcome": outcome,
        "trial_name": "hello-world__Ab12Cd3",
        "trial_id": UUID(int=attempt_idx) if outcome in ("passed", "failed", "infra") else None,
        "reward": {"passed": 1.0, "failed": 0.0}.get(outcome),
        "failure_kind": {"failed": "ended_turn", "infra": "infra_error"}.get(outcome),
        "prompt_sha256": SHA,
        "prompt_bytes": 1000,
        "store_trials": tuple(UUID(int=100 + i) for i in range(attempt_idx))
        if arm != "control"
        else (),
        "bundle": "/srv/b" if arm == "record-files" else None,
        "queries": 0 if arm == "record-engine" else None,
        "query_tool_ready": True if arm == "record-engine" else None,
        "preregistration": COMMIT if arm == "record-engine" else None,
        "started_at": T0,
        "finished_at": T0 + timedelta(minutes=5),
    }
    return AttemptResult(**(fields | overrides))


def ingest_ok(attempt_idx: int) -> IngestResult:
    return IngestResult(
        attempt_idx=attempt_idx,
        trial_id=UUID(int=attempt_idx),
        trial_name="hello-world__Ab12Cd3",
        ok=True,
        steps=3,
        deltas=1,
        chain_complete=True,
        bundle="/srv/b2",
        started_at=T0,
        seconds=12.5,
    )


@pytest.mark.parametrize("arm", ["control", "record-files", "record-engine"])
def test_arm_configs(arm: str) -> None:
    config = arm_config(arm)  # type: ignore[arg-type]
    assert config.ingests == (arm != "control")
    assert config.mounts_bundle == (arm == "record-files")
    assert config.serves_queries == (arm == "record-engine")
    assert (config.query_cap is not None) == (arm == "record-engine")
    assert ArmConfig.model_validate_json(config.model_dump_json()) == config


def test_an_arm_config_cannot_mix_arms() -> None:
    with pytest.raises(ValidationError, match="only record-files mounts"):
        ArmConfig(arm="control", ingests=False, mounts_bundle=True, serves_queries=False)
    with pytest.raises(ValidationError, match="query settings"):
        ArmConfig(
            arm="record-files", ingests=True, mounts_bundle=True, serves_queries=False, query_cap=5
        )


@pytest.mark.parametrize("arm", ["control", "record-files", "record-engine"])
def test_attempt_results_round_trip(arm: str) -> None:
    for outcome in ("passed", "failed", "infra", "lost"):
        extra = {"trial_name": None} if outcome == "lost" else {}
        result = attempt(2, arm=arm, outcome=outcome, **extra)
        assert AttemptResult.model_validate_json(result.model_dump_json()) == result
        assert result.counted == (outcome in ("passed", "failed"))


def test_a_record_arms_store_holds_exactly_the_attempts_before() -> None:
    with pytest.raises(ValidationError, match="holds exactly the attempts before it"):
        attempt(2, store_trials=(ROOT,))
    with pytest.raises(ValidationError, match="control has no store"):
        attempt(1, arm="control", store_trials=(ROOT,))


def test_attempt_result_refusals() -> None:
    with pytest.raises(ValidationError, match="committed preregistration"):
        attempt(1, arm="record-engine", preregistration=None)
    with pytest.raises(ValidationError, match="passed attempt has reward 1"):
        attempt(1, outcome="passed", reward=0.5)
    with pytest.raises(ValidationError, match="reward 1.0 passed"):
        attempt(1, outcome="failed", reward=1.0)
    with pytest.raises(ValidationError, match="lost attempt has no result"):
        attempt(1, outcome="lost", trial_id=ROOT)
    with pytest.raises(ValidationError, match="failure_kind"):
        attempt(1, outcome="failed", failure_kind=None)
    with pytest.raises(ValidationError, match="bundle"):
        attempt(1, arm="control", bundle="/srv/b")
    with pytest.raises(ValidationError, match="record-engine only"):
        attempt(1, arm="control", queries=3)


def test_ingest_results() -> None:
    assert IngestResult.model_validate_json(ingest_ok(1).model_dump_json()) == ingest_ok(1)
    failed = IngestResult(
        attempt_idx=1,
        trial_id=ROOT,
        trial_name="x__1",
        ok=False,
        error="OSError: disk full",
        started_at=T0,
        seconds=1.0,
    )
    assert not failed.ok
    with pytest.raises(ValidationError, match="error is set exactly"):
        failed.model_copy(update={"error": None}).model_validate(
            failed.model_dump() | {"error": None}
        )
    with pytest.raises(ValidationError, match="has its counts"):
        IngestResult(
            attempt_idx=1, trial_id=ROOT, trial_name="x", ok=True, started_at=T0, seconds=1
        )


def test_attempt_launch_round_trips() -> None:
    launch = AttemptLaunch(
        run="r",
        arm="record-engine",
        root_trial="x__1",
        attempt_idx=1,
        rerun=0,
        job_name="r-x__1-record-engine-a1",
        parent_trial_id=ROOT,
        prompt_sha256=SHA,
        prompt_bytes=10,
        store_trials=(ROOT,),
        preregistration=COMMIT,
        started_at=T0,
        pid=42,
    )
    assert AttemptLaunch.model_validate_json(launch.model_dump_json()) == launch


def settings(**overrides: Any) -> Exp2Settings:
    fields: dict[str, Any] = {
        "run": "tb40-exp2-dry1",
        "kind": "dry-run",
        "corpus_job": "corpus/jobs/x",
        "corpus_store": "corpus/stores/x",
        "corpus_config": "configs/harbor/x.json",
        "sample": (
            RootFailure(trial_name="x__1", trial_id=ROOT, task="t/x", instruction_sha256=SHA),
        ),
        "budget": 1,
        "report_at": (1,),
        "arms": ("control", "record-engine"),
        "primary": "p",
        "test": "none",
        "schema_version": 4,
        "derived": "3+abc",
        "derived_views": ("step_flags",),
        "protocol_sha256": SHA,
        "prefix_sha256": {"control/1": SHA, "record-engine/1": SHA},
        "hooks_sha256": SHA,
        "stop_hook": None,
        "query": arm_config("record-engine"),
        "model": "anthropic/claude-sonnet-5-5",
        "agent_kwargs": {"reasoning_effort": "medium"},
        "repo_sha": COMMIT,
    }
    return Exp2Settings(**(fields | overrides))


def test_settings_round_trip_and_refusals() -> None:
    found = settings()
    assert Exp2Settings.model_validate_json(found.model_dump_json()) == found
    with pytest.raises(ValidationError, match="one hash per arm and k"):
        settings(prefix_sha256={"control/1": SHA})
    with pytest.raises(ValidationError, match="query settings exactly"):
        settings(query=None)
    with pytest.raises(ValidationError, match="report_at"):
        settings(report_at=(2,))
    with pytest.raises(ValidationError, match="listed twice"):
        settings(sample=found.sample * 2)


def test_the_query_log_entry_is_the_stores() -> None:
    assert exp2_contracts.QueryLogEntry is QueryLogEntry
