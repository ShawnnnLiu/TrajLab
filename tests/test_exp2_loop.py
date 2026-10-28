"""The retry loop (technical spec §5.1, ADR-0022), offline: `trajlab run` is a fake that makes a
trial dir from the fixture, scored as each test scripts, and checkpoint images are in memory."""

import itertools
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.conftest import edit_trajectory
from tests.test_contracts_exp2 import ROOT, attempt, ingest_ok
from tests.test_exp2_chain import TRIAL_ID, as_attempt, copy_trial, memory_images
from tests.test_store_build import PROMPT, all_images
from trajlab.contracts import EXP2_ARMS, Exp2Settings
from trajlab.exp2 import chain as chains
from trajlab.exp2 import leaks, prereg
from trajlab.exp2.loop import ChainState, Exp2Runner, chain_state
from trajlab.store.build import build_trial
from trajlab.store.export import verify

COMMIT = "c" * 40
NEEDLES = leaks.Needles(frozenset({"test_hello_file_exists"}), frozenset())

# -- chain_state, from the logs alone ----------------------------------------------------------


def state(attempts: list[Any], ingests: list[Any] = (), **kw: Any) -> ChainState:  # type: ignore[assignment]
    options = {"root_trial_id": ROOT, "budget": 3, "ingests_rows": True, "max_reruns": 3} | kw
    return chain_state(attempts, ingests, **options)


def done_attempt(t: int, outcome: str = "failed") -> Any:
    return attempt(
        t, outcome=outcome, trial_id=UUID(int=t), parent_trial_id=UUID(int=t - 1) if t > 1 else ROOT
    )


def test_a_new_chain_launches_attempt_1_after_its_root() -> None:
    assert state([]) == ChainState("launch", 1, parent_trial_id=ROOT)


def test_a_failed_attempt_is_ingested_before_the_next() -> None:
    assert state([done_attempt(1)]).kind == "ingest"
    assert state([done_attempt(1)], [ingest_ok(1)]) == ChainState(
        "launch", 2, parent_trial_id=UUID(int=1)
    )
    assert state([done_attempt(1)], ingests_rows=False).kind == "launch"  # control


def test_a_pass_or_the_budget_ends_the_chain() -> None:
    assert state([done_attempt(1, "passed")]) == ChainState("done", 1, reason="passed")
    three = [done_attempt(t) for t in (1, 2, 3)]
    # The last attempt is never ingested: no attempt follows it.
    assert state(three, [ingest_ok(1), ingest_ok(2)]) == ChainState(
        "done", 3, reason="failed all 3"
    )


def test_a_failed_ingest_stops_the_chain_unless_retried() -> None:
    failed = ingest_ok(1).model_copy(update={"ok": False, "error": "OSError: disk"})
    assert state([done_attempt(1)], [failed]) == ChainState(
        "stopped", 1, reason="ingest failed: OSError: disk"
    )
    assert state([done_attempt(1)], [failed], retry_ingest=True).kind == "ingest"


def test_harness_failures_are_run_again_up_to_max_reruns() -> None:
    infra = attempt(1, outcome="infra")
    assert state([infra]) == ChainState("launch", 1, rerun=1, parent_trial_id=ROOT)
    assert state([infra, infra, infra]).kind == "stopped"
    unclassified = attempt(1, outcome="infra", failure_kind="unclassified", exception_type="X")
    assert state([unclassified]).kind == "stopped"
    assert state([unclassified], rerun_unclassified=True).kind == "launch"


def test_usage_limits_do_not_use_up_the_reruns() -> None:
    limited = attempt(1, outcome="infra", exception_type="ApiUsageLimitError")
    assert state([limited] * 5) == ChainState("launch", 1, rerun=5, parent_trial_id=ROOT)
    infra = attempt(1, outcome="infra")
    assert state([limited, infra, limited, infra, infra]).kind == "stopped"


# -- the runner, with a fake `trajlab run` -----------------------------------------------------


class FakeProcess:
    pids = itertools.count(1000)

    def __init__(self, job_dir: Path, config: dict[str, Any], outcome: str) -> None:
        self.pid = next(self.pids)
        self.job_dir, self.config, self.outcome = job_dir, config, outcome
        self.trial: Path | None = None
        self.code: int | None = None
        if outcome != "lost":
            # Harbor makes the trial dir, agent/ included, before the container starts.
            self.trial = as_attempt(
                copy_trial(job_dir),
                trial_id=uuid4(),
                reward=1.0 if outcome == "pass" else 0.0,
            )

    def poll(self) -> int | None:
        if self.code is not None:
            return self.code
        if self.trial is not None:
            if (
                "record-engine" in self.job_dir.name
                and not (self.trial / "agent" / "bin" / "query").is_file()
            ):
                return None  # the agent starts once its query tool is in place
            note = self.config["agents"][0]["kwargs"]["repair_note"]

            def first(data: dict[str, Any]) -> None:
                step = next(s for s in data["steps"] if s["source"] == "user")
                step["message"] = f"{note}\n\n{PROMPT}"
                step["timestamp"] = datetime.now(UTC).isoformat()

            edit_trajectory(self.trial, first)
            if self.outcome == "infra":
                path = self.trial / "result.json"
                result = json.loads(path.read_text())
                result["verifier_result"] = None
                result["exception_info"] = {
                    "exception_type": self.config.get("_exception", "EnvironmentStartTimeoutError"),
                    "exception_message": "",
                    "exception_traceback": "",
                    "occurred_at": datetime.now(UTC).isoformat(),
                }
                path.write_text(json.dumps(result))
        self.code = 0
        return 0


class FakeHarbor:
    def __init__(self, outcomes: dict[str, str] | None = None, exception: str | None = None):
        self.outcomes = outcomes or {}
        self.exception = exception
        self.launched: list[tuple[str, dict[str, Any]]] = []

    def spawn(self, command: list[str], log_path: Path) -> FakeProcess:
        assert command[1] == "run" and "--corpus-id" in command
        config = json.loads(Path(command[2]).read_text())
        name = config["job_name"]
        self.launched.append((name, config))
        if self.exception is not None:
            config["_exception"] = self.exception
        outcome = next((o for key, o in self.outcomes.items() if name.endswith(key)), "fail")
        return FakeProcess(Path(config["jobs_dir"]) / name, config, outcome)


@pytest.fixture
def corpus(tmp_path: Path) -> dict[str, Path]:
    return make_corpus(tmp_path)


def make_corpus(tmp_path: Path) -> dict[str, Path]:
    """A corpus of one failed first attempt: its job dir, its store, and its job config."""
    job = tmp_path / "corpus-job"
    as_attempt(copy_trial(job))
    store = tmp_path / "corpus-store"
    build_trial(job / "hello-world__K3GBok3", store, images=all_images(), work_dir=tmp_path / "w")
    config = tmp_path / "corpus.json"
    config.write_text(
        json.dumps(
            {
                "job_name": "hello-corpus",
                "jobs_dir": "corpus/jobs",
                "agent_timeout_multiplier": 0.5,
                "agents": [
                    {
                        "name": "claude-code",
                        "model_name": "anthropic/claude-sonnet-5-5",
                        "kwargs": {"reasoning_effort": "medium", "version": "2.1.278"},
                    }
                ],
                "tasks": [{"name": "hello-world/hello-world", "ref": "latest"}],
                "environment": {
                    "import_path": "trajlab.capture.preinstall:PreinstalledDockerEnvironment"
                },
            }
        )
    )
    return {"job": job, "store": store, "config": config}


def make_settings(
    corpus: dict[str, Path], arms: tuple[str, ...] = EXP2_ARMS, budget: int = 2
) -> Exp2Settings:
    return prereg.settings(
        run="t-exp2",
        kind="dry-run",
        corpus_job=corpus["job"],
        corpus_store=corpus["store"],
        corpus_config=corpus["config"],
        trials=None,
        budget=budget,
        arms=arms,  # type: ignore[arg-type]
        test="none",
    )


def make_runner(
    corpus: dict[str, Path], tmp_path: Path, harbor: FakeHarbor, **kw: Any
) -> Exp2Runner:
    options: dict[str, Any] = {
        "settings": make_settings(corpus),
        "preregistration": COMMIT,
        "root": tmp_path / "exp2",
        "jobs_dir": tmp_path / "jobs",
        "manifests_dir": tmp_path / "manifests",
        "work_dir": tmp_path / "work",
        "env_file": None,
        "spawn": harbor.spawn,
        "open_images": memory_images,
        "needles_of": lambda _: NEEDLES,
        "capture_problem": lambda _: None,
        "commit_manifests": lambda _: [],
        "disk_free_gb": lambda _: 1000.0,
        "alive": lambda pid, name: False,
    }
    return Exp2Runner(**(options | kw))


def run_until_done(runner: Exp2Runner, passes: int = 400) -> None:
    for _ in range(passes):
        runner.step()
        if runner.done():
            return
        time.sleep(0.01)
    raise AssertionError(f"not done: {runner.status()}")


def chain_of(runner: Exp2Runner, arm: str) -> Path:
    return chains.chain_dir(runner.root, "t-exp2", "hello-world__K3GBok3", arm)  # type: ignore[arg-type]


def test_every_arm_retries_until_the_budget(corpus: dict[str, Path], tmp_path: Path) -> None:
    harbor = FakeHarbor()
    runner = make_runner(corpus, tmp_path, harbor)
    run_until_done(runner)
    assert sorted(name for name, _ in harbor.launched) == sorted(
        f"t-exp2-hello-world__K3GBok3-{arm}-a{t}" for arm in EXP2_ARMS for t in (1, 2)
    )
    for arm in EXP2_ARMS:
        attempts = chains.read_attempts(chain_of(runner, arm))
        assert [(a.attempt_idx, a.outcome) for a in attempts] == [(1, "failed"), (2, "failed")]
        first, second = attempts
        assert first.parent_trial_id == TRIAL_ID and second.parent_trial_id == first.trial_id
        assert all(a.prompt_delivered for a in attempts)
        ingests = chains.read_ingests(chain_of(runner, arm))
        if arm == "control":
            assert ingests == [] and first.store_trials == second.store_trials == ()
            continue
        # At attempt t the store held exactly attempts 0 to t-1; the last is never ingested.
        assert first.store_trials == (TRIAL_ID,)
        assert second.store_trials == (TRIAL_ID, first.trial_id)
        assert [(i.attempt_idx, i.ok) for i in ingests] == [(1, True)]
        store = chains.store_dir(chain_of(runner, arm))
        assert chains.store_trials(store) == (TRIAL_ID, first.trial_id)
        if arm == "record-files":
            assert second.bundle == str(chains.bundle_dir(chain_of(runner, arm), 2))
            assert verify(store, Path(second.bundle)) == []
        else:
            assert [(a.queries, a.query_tool_ready) for a in attempts] == [(0, True), (0, True)]
            assert all(a.preregistration == COMMIT for a in attempts)
    assert runner.status()["chains"]["hello-world__K3GBok3/control"]["reason"] == "failed all 2"


def test_each_attempts_config_carries_its_arms_prompt_and_mount(
    corpus: dict[str, Path], tmp_path: Path
) -> None:
    harbor = FakeHarbor()
    run_until_done(make_runner(corpus, tmp_path, harbor))
    configs = dict(harbor.launched)
    note = {name: c["agents"][0]["kwargs"]["repair_note"] for name, c in configs.items()}
    stem = "t-exp2-hello-world__K3GBok3"
    assert note[f"{stem}-control-a1"].startswith("1 previous attempt(s) at this task FAILED")
    assert note[f"{stem}-control-a2"].startswith("2 previous attempt(s) at this task FAILED")
    assert "/logs/agent/record/" in note[f"{stem}-record-files-a2"]
    assert "/logs/agent/bin/query" in note[f"{stem}-record-engine-a2"]
    mounts = configs[f"{stem}-record-files-a2"]["environment"]["mounts"]
    assert mounts[0]["source"].endswith("record-files/bundles/attempt-2") and mounts[0]["read_only"]
    assert "mounts" not in configs[f"{stem}-record-engine-a2"]["environment"]


def test_a_pass_ends_its_chain(corpus: dict[str, Path], tmp_path: Path) -> None:
    harbor = FakeHarbor({"record-files-a1": "pass"})
    runner = make_runner(corpus, tmp_path, harbor)
    run_until_done(runner)
    passed = chains.read_attempts(chain_of(runner, "record-files"))
    assert [(a.attempt_idx, a.outcome, a.reward) for a in passed] == [(1, "passed", 1.0)]
    assert chains.read_ingests(chain_of(runner, "record-files")) == []
    assert len(chains.read_attempts(chain_of(runner, "control"))) == 2


def test_an_attempt_the_harness_failed_runs_again_under_a_new_name(
    corpus: dict[str, Path], tmp_path: Path
) -> None:
    harbor = FakeHarbor({"control-a1": "infra", "record-files-a1": "lost"})
    runner = make_runner(
        corpus, tmp_path, harbor, settings=make_settings(corpus, ("control", "record-files"), 1)
    )
    run_until_done(runner)
    control = chains.read_attempts(chain_of(runner, "control"))
    assert [(a.job_name.rsplit("-", 2)[-2:], a.outcome) for a in control] == [
        (["control", "a1"], "infra"),
        (["a1", "r1"], "failed"),
    ]
    assert control[0].failure_kind == "infra_error" and not control[0].counted
    lost = chains.read_attempts(chain_of(runner, "record-files"))
    assert [a.outcome for a in lost] == ["lost", "failed"]
    assert lost[0].trial_id is None and lost[1].job_name.endswith("-a1-r1")


def test_a_usage_limit_pauses_launches(corpus: dict[str, Path], tmp_path: Path) -> None:
    clock = [0.0]
    harbor = FakeHarbor({"control-a1": "infra"}, exception="ApiUsageLimitError")
    runner = make_runner(
        corpus,
        tmp_path,
        harbor,
        settings=make_settings(corpus, ("control",), 1),
        clock=lambda: clock[0],
        usage_backoff_s=600.0,
    )
    runner.step()  # launch
    runner.step()  # reap: usage limit
    runner.step()
    assert len(harbor.launched) == 1 and runner.hold == "usage-limit pause"
    clock[0] = 601.0
    runner.step()
    assert len(harbor.launched) == 2


def test_launches_hold_while_capture_is_down(corpus: dict[str, Path], tmp_path: Path) -> None:
    harbor = FakeHarbor()
    runner = make_runner(corpus, tmp_path, harbor, capture_problem=lambda _: "no watcher")
    runner.step()
    assert harbor.launched == [] and runner.status()["hold"] == "no watcher"


def test_a_failed_ingest_stops_only_its_chain(corpus: dict[str, Path], tmp_path: Path) -> None:
    def broken(trial_dir: Path, scratch: Path) -> Any:
        raise OSError("docker image save failed")

    harbor = FakeHarbor()
    runner = make_runner(corpus, tmp_path, harbor, open_images=broken)
    run_until_done(runner)
    states = {n: c["state"] for n, c in runner.status()["chains"].items()}
    assert states == {
        "hello-world__K3GBok3/control": "done",
        "hello-world__K3GBok3/record-files": "stopped",
        "hello-world__K3GBok3/record-engine": "stopped",
    }
    assert len(chains.read_attempts(chain_of(runner, "record-files"))) == 1


def test_record_engine_needs_a_committed_preregistration(
    corpus: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(chains.ChainError, match="committed preregistration"):
        make_runner(corpus, tmp_path, FakeHarbor(), preregistration=None)
    make_runner(
        corpus,
        tmp_path,
        FakeHarbor(),
        preregistration=None,
        settings=make_settings(corpus, ("control", "record-files")),
    )


def test_a_leaking_prompt_is_never_launched(corpus: dict[str, Path], tmp_path: Path) -> None:
    harbor = FakeHarbor()
    needles = leaks.Needles(frozenset({"previous"}), frozenset())
    runner = make_runner(corpus, tmp_path, harbor, needles_of=lambda _: needles)
    runner.step()
    assert harbor.launched == []
    reasons = {c["reason"] for c in runner.status()["chains"].values()}
    assert reasons == {"the prompt leaks check name previous"}


def test_a_prefix_other_than_the_preregistered_is_never_launched(
    corpus: dict[str, Path], tmp_path: Path
) -> None:
    found = make_settings(corpus)
    hashes = dict(found.prefix_sha256) | {"control/1": "0" * 64}
    harbor = FakeHarbor()
    runner = make_runner(
        corpus, tmp_path, harbor, settings=found.model_copy(update={"prefix_sha256": hashes})
    )
    runner.step()
    assert all("control" not in name for name, _ in harbor.launched)
    assert (
        "not the preregistered"
        in runner.status()["chains"]["hello-world__K3GBok3/control"]["reason"]
    )


def test_a_restarted_loop_resumes_from_the_logs(corpus: dict[str, Path], tmp_path: Path) -> None:
    harbor = FakeHarbor()
    first = make_runner(corpus, tmp_path, harbor)
    first.step()  # launches attempt 1 of each arm
    assert len(harbor.launched) == 3
    # The loop dies with its attempts running; their `trajlab run`s finish without it.
    for entry in first.running.values():
        if entry.server is not None:
            entry.server.stop()
    second = make_runner(corpus, tmp_path, harbor)
    run_until_done(second)
    names = [name for name, _ in harbor.launched]
    assert len(names) == len(set(names)) == 6  # nothing ran twice
    for arm in EXP2_ARMS:
        attempts = chains.read_attempts(chain_of(second, arm))
        assert [(a.attempt_idx, a.outcome) for a in attempts] == [(1, "failed"), (2, "failed")]
        # Adopted from its job dir: the first message was not written when the loop read it.
        assert attempts[0].job_name.endswith("-a1")


def test_the_status_file_is_written_each_pass(corpus: dict[str, Path], tmp_path: Path) -> None:
    runner = make_runner(corpus, tmp_path, FakeHarbor())
    runner.step()
    status = json.loads((runner.root / "t-exp2" / "status.json").read_text())
    assert status["run"] == "t-exp2" and len(status["running"]) == 3
