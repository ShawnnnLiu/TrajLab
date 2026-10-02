from pathlib import Path

from typer.testing import CliRunner

from tests.conftest import assemble_job_dir, make_checkpoint, write_capture
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
