import json
import os
from collections.abc import Sequence
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FIXTURE_TRIAL_NAME, assemble_job_dir, make_checkpoint, write_capture
from tests.test_store_build import all_images, capture
from trajlab.atif.postprocess import postprocess_trial
from trajlab.capture.repair_launcher import Launcher
from trajlab.checkpoint.watcher import hold_watcher_lock
from trajlab.cli import app, identify_trial
from trajlab.contracts import PREINSTALL_RECORD_FILENAME, CheckpointRecord, CorpusManifest
from trajlab.store import schema
from trajlab.store.build import store_meta
from trajlab.store.reconcile import ImageChain

COMMANDS = {"run", "watch", "repair", "postprocess", "manifest", "validate", "store", "trace"}


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


def test_record_writes_needs_root(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("run as root")
    result = CliRunner().invoke(app, ["record-writes", str(tmp_path)])
    assert result.exit_code == 1
    assert "needs root" in result.output


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


def test_watch_says_extras_without_a_gate_record_no_diffs(tmp_path: Path) -> None:
    watch = ["watch", str(tmp_path), "--every", "1", "--gate", "none", "--extras"]
    with hold_watcher_lock(tmp_path):  # stops the command right after its checks
        result = CliRunner().invoke(app, watch)
    assert "records processes only" in flat(result.output)


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


def test_postprocess_reports_bad_paths_and_still_writes_the_good(
    trial_copy: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    (tmp_path / "empty").mkdir()
    args = ["postprocess", str(missing), str(trial_copy), str(tmp_path / "empty")]

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert f"trajlab postprocess: {missing}: not a directory" in result.output
    assert f"trajlab postprocess: {tmp_path / 'empty'}: no trial dirs" in result.output
    assert (trial_copy / "agent/trajectory.enriched.json").is_file()


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


def test_trace_needs_a_directory(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["trace", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "not a directory" in result.output


# -- store build -------------------------------------------------------------------------------


def _trials_in(store: Path) -> int:
    with schema.connect(schema.record_path(store), read_only=True) as con:
        return con.execute("SELECT count(*) FROM trials").fetchone()[0]


def test_store_build_without_images_builds_steps_and_skips_a_second_time(
    trial_copy: Path, tmp_path: Path
) -> None:
    postprocess_trial(trial_copy)
    store = tmp_path / "store"
    args = ["store", "build", str(trial_copy), "--store", str(store), "--no-images"]

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"built {trial_copy}: ")
    # The stock fixture has no call records, so its chain has two reasons.
    assert "chain incomplete (" in result.output
    assert "images were not checked" in result.output
    assert result.output.rstrip().endswith(" s")
    assert _trials_in(store) == 1

    again = CliRunner().invoke(app, args)

    assert again.exit_code == 0, again.output
    assert again.output.startswith(f"skipped {trial_copy}: already in {store}")
    assert _trials_in(store) == 1


def test_store_build_takes_a_job_dir(job_dir: Path, tmp_path: Path) -> None:
    trial_dir = job_dir / FIXTURE_TRIAL_NAME
    postprocess_trial(trial_dir)
    store = tmp_path / "store"

    result = CliRunner().invoke(
        app, ["store", "build", str(job_dir), "--store", str(store), "--no-images"]
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"built {trial_dir}: ")
    assert _trials_in(store) == 1


def test_store_build_reports_an_unpostprocessed_trial(trial_copy: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    args = ["store", "build", str(trial_copy), "--store", str(store), "--no-images"]

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert f"failed {trial_copy}: " in result.output
    assert "trajlab postprocess" in result.output


def test_store_build_reports_bad_paths(tmp_path: Path) -> None:
    store = tmp_path / "store"
    missing = tmp_path / "missing"
    (tmp_path / "empty").mkdir()
    args = ["store", "build", str(missing), str(tmp_path / "empty"), "--store", str(store)]

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert f"{missing}: not a directory" in result.output
    assert f"{tmp_path / 'empty'}: no trial dirs" in result.output


def test_store_build_leaves_no_scratch_behind(trial_copy: Path, tmp_path: Path) -> None:
    postprocess_trial(trial_copy)
    work = tmp_path / "work" / "scratch"  # made by the build
    args = ["store", "build", str(trial_copy), "--store", str(tmp_path / "store")]
    args += ["--no-images", "--work-dir", str(work)]

    assert CliRunner().invoke(app, args).exit_code == 0
    assert list(work.iterdir()) == []


def test_store_build_reports_a_work_dir_it_cannot_make(trial_copy: Path, tmp_path: Path) -> None:
    postprocess_trial(trial_copy)
    (tmp_path / "file").write_text("")
    args = ["store", "build", str(trial_copy), "--store", str(tmp_path / "store")]
    args += ["--no-images", "--work-dir", str(tmp_path / "file" / "work")]

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert "trajlab store build: cannot make scratch space: " in result.output
    assert isinstance(result.exception, SystemExit)


def test_store_build_reports_a_database_error_and_goes_on(
    trial_copy: Path, job_dir: Path, tmp_path: Path
) -> None:
    postprocess_trial(trial_copy)
    postprocess_trial(job_dir / FIXTURE_TRIAL_NAME)
    store = tmp_path / "store"
    schema.create_store(store, store_meta())
    args = ["store", "build", str(trial_copy), str(job_dir), "--store", str(store), "--no-images"]

    # An open connection under another configuration makes each write fail with a duckdb.Error,
    # as a lock held by another process does; neither is a ValueError or an OSError.
    with duckdb.connect(str(schema.record_path(store)), read_only=True):
        result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    for trial_dir in (trial_copy, job_dir / FIXTURE_TRIAL_NAME):
        assert f"failed {trial_dir}: Connection Error: " in result.output
    assert _trials_in(store) == 0


# -- store query ----------------------------------------------------------------------------


def test_store_query_prints_rows_and_a_count(trial_copy: Path, tmp_path: Path) -> None:
    postprocess_trial(trial_copy)
    store = tmp_path / "store"
    assert (
        CliRunner()
        .invoke(app, ["store", "build", str(trial_copy), "--store", str(store), "--no-images"])
        .exit_code
        == 0
    )
    args = ["store", "query", str(store), "SELECT step_idx, tool FROM steps ORDER BY step_idx"]
    result = CliRunner().invoke(app, [*args, "--timeout", "30"])
    assert result.exit_code == 0, result.output
    assert result.output == '{"step_idx":1,"tool":"Write"}\n{"step_idx":2,"tool":null}\n-- 2 rows\n'
    cut = CliRunner().invoke(app, [*args, "--cap", "1", "--timeout", "30"])
    assert cut.output.endswith("-- 1 rows shown; the query has more. Add a LIMIT, or narrow it.\n")
    refused = CliRunner().invoke(app, ["store", "query", str(store), "DROP TABLE steps"])
    assert refused.exit_code == 1
    assert refused.output == "trajlab store query: refused: only a SELECT or WITH query\n"


def test_store_serve_refuses_a_missing_store(tmp_path: Path) -> None:
    (tmp_path / "job").mkdir()
    result = CliRunner().invoke(
        app, ["store", "serve", str(tmp_path / "none"), str(tmp_path / "job")]
    )
    assert result.exit_code == 1
    assert "trajlab store serve: " in result.output and "does not exist" in result.output


# -- store build with images, from tests/test_store_build.py's in-memory layers -----------------


@pytest.fixture
def start_images(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stands the in-memory images in for Docker's; lists the start image each provider got."""
    given: list[str] = []

    class Provider:
        def __init__(self, work_dir: Path, *, start_image: str) -> None:
            given.append(start_image)

        def __enter__(self) -> "Provider":
            return self

        def __exit__(self, *exc_info: object) -> None:
            pass

        def chain(self, checkpoints: Sequence[CheckpointRecord]) -> ImageChain:
            return all_images().chain(checkpoints)

    monkeypatch.setattr("trajlab.cli.DockerImageProvider", Provider)
    return given


def write_preinstall_record(trial_dir: Path, image: str) -> None:
    record = {
        "task_image": "hello-world:task",
        "task_image_id": "sha256:" + "1" * 64,
        "task_content_key": "k",
        "image": image,
        "image_id": "sha256:" + "2" * 64,
        "agent_name": "claude-code",
        "agent_version": "2.1.278",
        "agent_sha256": "3" * 64,
        "harbor_version": "0.23.0",
        "recipe_version": 2,
        "cache_hit": True,
        "recorded_at": "2026-10-02T00:00:00Z",
    }
    (trial_dir / PREINSTALL_RECORD_FILENAME).write_text(json.dumps(record))


def test_store_build_reads_checkpoints_against_the_trials_start_image(
    trial_copy: Path, tmp_path: Path, start_images: list[str]
) -> None:
    capture(trial_copy)
    write_preinstall_record(trial_copy, "trajlab-preinstalled:abc")
    store = tmp_path / "store"

    # A trial's own record wins over --start-image, which is for trials without one.
    result = CliRunner().invoke(
        app, ["store", "build", str(trial_copy), "--store", str(store), "--start-image", "x:y"]
    )

    assert result.exit_code == 0, result.output
    assert "3 steps, 1 deltas, 2 checkpoints, chain complete" in result.output
    assert start_images == ["trajlab-preinstalled:abc"]


def test_store_build_skips_a_trial_built_with_start_image_without_it(
    trial_copy: Path, tmp_path: Path, start_images: list[str]
) -> None:
    capture(trial_copy)  # no trajlab-preinstall.json
    store = tmp_path / "store"
    args = ["store", "build", str(trial_copy), "--store", str(store)]

    first = CliRunner().invoke(app, [*args, "--start-image", "hello-world:task"])

    assert first.exit_code == 0, first.output
    assert "1 deltas, 2 checkpoints, chain complete" in first.output
    assert start_images == ["hello-world:task"]

    again = CliRunner().invoke(app, args)

    assert again.exit_code == 0, again.output
    assert again.output.startswith(f"skipped {trial_copy}: already in {store}")


def test_store_build_needs_a_start_image_to_read_checkpoints(
    trial_copy: Path, tmp_path: Path, start_images: list[str]
) -> None:
    capture(trial_copy)
    store = tmp_path / "store"
    args = ["store", "build", str(trial_copy), "--store", str(store)]
    assert CliRunner().invoke(app, [*args, "--no-images"]).exit_code == 0

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert result.output.startswith(
        f"failed {trial_copy}: neither trajlab-preinstall.json nor trajlab-resume.json names its "
        "start image; pass --start-image, or --no-images for steps only"
    )
    assert start_images == []
    # The steps built without images are kept.
    assert _trials_in(store) == 1


def test_store_build_needs_no_start_image_without_checkpoints(
    trial_copy: Path, tmp_path: Path, start_images: list[str]
) -> None:
    postprocess_trial(trial_copy)

    result = CliRunner().invoke(
        app, ["store", "build", str(trial_copy), "--store", str(tmp_path / "store")]
    )

    assert result.exit_code == 0, result.output
    assert "0 deltas, 0 checkpoints" in result.output
    assert "images were not checked" not in result.output


def test_store_build_refuses_a_start_image_without_images(trial_copy: Path, tmp_path: Path) -> None:
    postprocess_trial(trial_copy)
    store = tmp_path / "store"
    args = ["store", "build", str(trial_copy), "--store", str(store), "--no-images"]

    result = CliRunner().invoke(app, [*args, "--start-image", "hello-world:task"])

    assert result.exit_code == 1
    assert "trajlab store build: --start-image " in result.output
    assert "pass one or the other" in result.output
    assert not store.exists()
