import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import FIXTURE_TRIAL_NAME, assemble_job_dir, make_checkpoint, write_capture
from trajlab.capture.repair_launcher import Launcher
from trajlab.checkpoint.watcher import hold_watcher_lock
from trajlab.cli import app, identify_trial
from trajlab.contracts import CorpusManifest

COMMANDS = {"run", "watch", "repair", "postprocess", "manifest", "validate"}


def test_help_lists_all_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in COMMANDS:
        assert name in result.output


def test_manifest_names_corpus_after_single_job(job_dir: Path, tmp_path: Path) -> None:
    manifests = tmp_path / "manifests"
    args = ["manifest", str(job_dir), "--manifests-dir", str(manifests)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    manifest = CorpusManifest.model_validate_json(
        (manifests / "hello-world-smoke.json").read_text()
    )
    assert manifest.config_path is None
    # A second write needs --force.
    assert CliRunner().invoke(app, args).exit_code == 1
    assert CliRunner().invoke(app, [*args, "--force"]).exit_code == 0


def test_manifest_records_repo_relative_config(job_dir: Path, tmp_path: Path) -> None:
    config = Path("tests/fixtures/hello-world-job/config.json")
    args = ["manifest", str(job_dir), "--config", str(config), "--manifests-dir", str(tmp_path)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    manifest = CorpusManifest.model_validate_json((tmp_path / "config.json").read_text())
    assert manifest.config_path == config.as_posix()


def test_manifest_needs_corpus_id_for_several_jobs(tmp_path: Path) -> None:
    jobs = [str(assemble_job_dir(tmp_path, name)) for name in ("job-a", "job-b")]
    manifests = ["--manifests-dir", str(tmp_path / "manifests")]
    result = CliRunner().invoke(app, ["manifest", *jobs, *manifests])
    assert result.exit_code == 1
    assert "--corpus-id" in result.output
    result = CliRunner().invoke(app, ["manifest", *jobs, *manifests, "--corpus-id", "both"])
    assert result.exit_code == 0, result.output


def test_manifest_reports_bad_job_dir(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["manifest", str(tmp_path), "--manifests-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "not a Harbor job dir" in result.output


def test_run_refuses_without_starting_harbor(tmp_path: Path) -> None:
    config = tmp_path / "x.json"
    config.write_text('{"job_name": "x", "jobs_dir": "' + str(tmp_path) + '"}')
    (tmp_path / "x").mkdir()
    result = CliRunner().invoke(app, ["run", str(config), "--manifests-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "new job_name" in result.output


def test_watch_rejects_unknown_backend(tmp_path: Path) -> None:
    args = ["watch", str(tmp_path), "--every", "1", "--gate", "change", "--backend", "statefork"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "unknown backend" in result.output


def test_watch_refuses_second_watcher(tmp_path: Path) -> None:
    with hold_watcher_lock(tmp_path):
        result = CliRunner().invoke(
            app, ["watch", str(tmp_path), "--every", "1", "--gate", "change"]
        )
    assert result.exit_code == 1
    assert "already holds" in result.output


def test_watch_requires_every_and_gate(tmp_path: Path) -> None:
    watch = ["watch", str(tmp_path)]
    assert CliRunner().invoke(app, [*watch, "--gate", "change"]).exit_code != 0
    assert CliRunner().invoke(app, [*watch, "--every", "1"]).exit_code != 0
    assert CliRunner().invoke(app, [*watch, "--every", "0", "--gate", "none"]).exit_code != 0
    assert CliRunner().invoke(app, [*watch, "--every", "1", "--gate", "maybe"]).exit_code != 0


def test_watch_refuses_gate_with_every_n(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["watch", str(tmp_path), "--every", "3", "--gate", "change"])
    assert result.exit_code == 1
    assert "requires --every 1" in result.output


def test_identify_trial_names_compose_project(fixture_trial: Path) -> None:
    identity = identify_trial(fixture_trial)
    assert identity.trial_name == "hello-world__K3GBok3"
    assert identity.compose_project == "hello-world__k3gbok3__env"


def test_postprocess_job_dir_writes_each_trial(job_dir: Path) -> None:
    result = CliRunner().invoke(app, ["postprocess", str(job_dir)])

    assert result.exit_code == 0, result.output
    assert "3 steps, 0 checkpoints, 0 compactions" in result.output
    assert (job_dir / "hello-world__K3GBok3/agent/trajectory.enriched.json").is_file()


def test_postprocess_trial_dir(trial_copy: Path) -> None:
    write_capture(trial_copy, [make_checkpoint(1)])

    result = CliRunner().invoke(app, ["postprocess", str(trial_copy)])

    assert result.exit_code == 0, result.output
    assert "4 steps, 1 checkpoints" in result.output


def test_postprocess_reports_failures_and_skips(job_dir: Path) -> None:
    trial = job_dir / "hello-world__K3GBok3"
    write_capture(trial, [make_checkpoint(1, "toolu_nowhere")])
    unfinished = job_dir / "hello-world__Unfinish"
    (unfinished / "agent").mkdir(parents=True)
    (unfinished / "config.json").write_text((trial / "config.json").read_text())

    result = CliRunner().invoke(app, ["postprocess", str(job_dir)])

    assert result.exit_code == 1
    assert "skipped" in result.output and "no agent/trajectory.json" in result.output
    assert "failed" in result.output and "toolu_nowhere" in result.output


def test_postprocess_rejects_empty_dir(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["postprocess", str(tmp_path)])

    assert result.exit_code == 1
    assert "no trial dirs" in result.output


TRIAL_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_FORCE_OAUTH",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)


def flat(output: str) -> str:
    """CLI output with rich's box drawing and line wrapping removed."""
    return " ".join(output.replace("│", " ").split())


@pytest.fixture
def bare_env(monkeypatch: pytest.MonkeyPatch, job_dir: Path) -> None:
    """No credential or cap variable in the environment, and no .env in the cwd."""
    for name in TRIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(job_dir.parent)


@pytest.mark.usefixtures("bare_env")
def test_repair_dry_run_plans_only_the_added_arm_on_the_recorded_draw(job_dir: Path) -> None:
    trial = job_dir / FIXTURE_TRIAL_NAME
    result_path = trial / "result.json"
    data = json.loads(result_path.read_text())
    result_path.write_text(json.dumps(data | {"verifier_result": {"rewards": {"reward": 0.0}}}))
    jobs = job_dir.parent
    args = ["repair", str(job_dir), "--prefix", "rep-v1", "--per-task", "1", "--arms"]
    args += ["traj-text", "--jobs-dir", str(jobs), "--manifests-dir", str(jobs / "m"), "--dry-run"]

    refused = CliRunner().invoke(app, args)
    assert refused.exit_code == 2
    assert "rep-v1.selection.json" in flat(refused.output)

    inputs = jobs / "_repair-inputs"
    inputs.mkdir()
    task = "hello-world/hello-world"
    drawn = {"task": task, "seed": f"rep-v1:{task}", "candidates": [FIXTURE_TRIAL_NAME]}
    drawn |= {"chosen": [FIXTURE_TRIAL_NAME], "not_candidates": {}}
    (inputs / "rep-v1.selection.json").write_text(json.dumps({f"{job_dir.name}:{task}": drawn}))

    # A dry run reports, and does not refuse, an environment trials could not run in.
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0].startswith("env file: none (none of CLAUDE_CODE_OAUTH_TOKEN")
    assert lines[2].split() == [f"rep-v1-{FIXTURE_TRIAL_NAME}-traj-text", "0.6", "1", "0", "no"]
    assert lines[-1] == "1 jobs, 3 trials"
    assert [p.name for p in inputs.iterdir()] == ["rep-v1.selection.json"]

    # Adding an arm to a round drawn per task needs --per-task: without it every failure is planned.
    without = [a for a in args if a not in ("--per-task", "1")]
    refused = CliRunner().invoke(app, without)
    assert refused.exit_code == 2 and "--per-task" in flat(refused.output)


@pytest.mark.usefixtures("bare_env")
@pytest.mark.parametrize(
    ("env_file", "refusal"),
    [
        (None, "none of CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN"),
        ("CLAUDE_CODE_OAUTH_TOKEN=t\n", "CLAUDE_CODE_MAX_OUTPUT_TOKENS is not 128000"),
        (
            "ANTHROPIC_API_KEY=k\nCLAUDE_FORCE_OAUTH=1\nCLAUDE_CODE_MAX_OUTPUT_TOKENS=128000\n",
            "CLAUDE_FORCE_OAUTH is set but CLAUDE_CODE_OAUTH_TOKEN is not",
        ),
        ("CLAUDE_CODE_OAUTH_TOKEN=t\nCLAUDE_CODE_MAX_OUTPUT_TOKENS=128000\n", None),
    ],
    ids=["no-credentials", "no-cap", "force-oauth-without-token", "as-round-1"],
)
def test_repair_refuses_to_launch_trials_unlike_round_1(
    job_dir: Path, monkeypatch: pytest.MonkeyPatch, env_file: str | None, refusal: str | None
) -> None:
    started: list[Launcher] = []
    monkeypatch.setattr(Launcher, "run", lambda self, poll_s: started.append(self))
    jobs = job_dir.parent
    args = ["repair", str(job_dir), "--prefix", "rep-v1", "--jobs-dir", str(jobs)]
    args += ["--manifests-dir", str(jobs / "m")]
    if env_file is not None:
        (jobs / "test.env").write_text(env_file)
        args += ["--env-file", str(jobs / "test.env")]

    result = CliRunner().invoke(app, args)

    if refusal is None:
        assert result.exit_code == 0, result.output
        assert [launcher.env_file for launcher in started] == [jobs / "test.env"]
    else:
        assert result.exit_code == 2 and refusal in flat(result.output)
        assert started == []


def test_repair_refuses_an_env_file_that_does_not_exist(job_dir: Path) -> None:
    jobs = job_dir.parent
    args = ["repair", str(job_dir), "--prefix", "rep-v1", "--jobs-dir", str(jobs)]
    result = CliRunner().invoke(app, [*args, "--env-file", str(jobs / "missing.env")])
    assert result.exit_code == 2 and "does not exist" in flat(result.output)
