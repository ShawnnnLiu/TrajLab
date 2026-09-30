import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import assemble_job_dir
from trajlab.capture.corpus import (
    ManifestError,
    RepoState,
    build_manifest,
    repo_state,
    write_manifest,
)
from trajlab.contracts import CorpusManifest, CorpusTask

REPO = RepoState(sha="a" * 40, dirty=False)
HELLO_DIGEST = "sha256:38d7a077f07fbee8efc78db5dec9a72f82e727510ad1dcfeac0b55fa845256b7"


def _edit_json(path: Path, mutate: Any) -> None:
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def test_build_manifest_from_fixture_job(job_dir: Path) -> None:
    created = datetime(2026, 9, 30, tzinfo=UTC)
    manifest = build_manifest(
        [job_dir],
        corpus_id="hello",
        repo=REPO,
        config_path="configs/harbor/hello.json",
        storage="local",
        created_at=created,
    )
    assert manifest.harbor_version == "0.23.0"
    assert manifest.agent_name == "claude-code"
    assert manifest.model_name == "anthropic/claude-sonnet-5"
    assert manifest.agent_kwargs == {}
    assert manifest.environment_type == "docker"
    assert manifest.n_attempts == 1
    assert manifest.timeout_multiplier == 1.0
    assert manifest.agent_timeout_multiplier is None
    assert manifest.tasks == [CorpusTask(name="hello-world/hello-world", digest=HELLO_DIGEST)]
    [job] = manifest.jobs
    assert job.job_name == "hello-world-smoke"
    assert job.job_config == json.loads((job_dir / "config.json").read_text())
    assert [t.trial_name for t in job.trials] == ["hello-world__K3GBok3"]
    assert CorpusManifest.model_validate_json(manifest.model_dump_json()) == manifest


def test_build_manifest_spans_agreeing_jobs(tmp_path: Path) -> None:
    jobs = [assemble_job_dir(tmp_path, name) for name in ("job-a", "job-b")]
    manifest = build_manifest(jobs, corpus_id="two", repo=REPO)
    assert [j.job_name for j in manifest.jobs] == ["job-a", "job-b"]
    assert len(manifest.tasks) == 1


def test_build_manifest_refuses_jobs_that_disagree(tmp_path: Path) -> None:
    job_a = assemble_job_dir(tmp_path, "job-a")
    job_b = assemble_job_dir(tmp_path, "job-b")
    _edit_json(
        job_b / "lock.json", lambda d: d["trials"][0]["agent"].update(model_name="anthropic/x")
    )
    with pytest.raises(ManifestError, match=r"disagree on model_name: .*job-b/lock.json"):
        build_manifest([job_a, job_b], corpus_id="bad", repo=REPO)


def test_build_manifest_refuses_one_task_with_two_digests(tmp_path: Path) -> None:
    job_a = assemble_job_dir(tmp_path, "job-a")
    job_b = assemble_job_dir(tmp_path, "job-b")
    _edit_json(
        job_b / "lock.json", lambda d: d["trials"][0]["task"].update(digest="sha256:" + "0" * 64)
    )
    with pytest.raises(ManifestError, match="more than one digest"):
        build_manifest([job_a, job_b], corpus_id="bad", repo=REPO)


def test_build_manifest_refuses_non_job_dir(job_dir: Path) -> None:
    (job_dir / "lock.json").unlink()
    with pytest.raises(ManifestError, match="missing lock.json"):
        build_manifest([job_dir], corpus_id="bad", repo=REPO)


def test_build_manifest_reports_malformed_harbor_file(job_dir: Path) -> None:
    _edit_json(job_dir / "lock.json", lambda d: d["trials"][0]["task"].update(digest="bad"))
    with pytest.raises(ManifestError, match=r"(?s)lock.json: .*Digest"):
        build_manifest([job_dir], corpus_id="bad", repo=REPO)


def test_build_manifest_refuses_unfinished_trial(job_dir: Path) -> None:
    (job_dir / "hello-world__K3GBok3" / "result.json").unlink()
    with pytest.raises(ManifestError, match="no result.json"):
        build_manifest([job_dir], corpus_id="bad", repo=REPO)


def test_build_manifest_refuses_trial_from_another_job(job_dir: Path) -> None:
    _edit_json(
        job_dir / "hello-world__K3GBok3" / "config.json",
        lambda d: d.update(job_id="00000000-0000-0000-0000-000000000000"),
    )
    with pytest.raises(ManifestError, match="job_id"):
        build_manifest([job_dir], corpus_id="bad", repo=REPO)


def test_write_manifest_refuses_to_replace_unless_asked(job_dir: Path, tmp_path: Path) -> None:
    manifest = build_manifest([job_dir], corpus_id="hello", repo=REPO)
    manifests = tmp_path / "manifests"
    path = write_manifest(manifest, manifests, overwrite=False)
    assert CorpusManifest.model_validate_json(path.read_text()) == manifest
    with pytest.raises(ManifestError, match="exists"):
        write_manifest(manifest, manifests, overwrite=False)
    assert write_manifest(manifest, manifests, overwrite=True) == path


def test_repo_state_reports_sha_and_dirtiness(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    (tmp_path / "f").write_text("x")
    git("add", "f")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    assert repo_state(tmp_path) == RepoState(sha=git("rev-parse", "HEAD"), dirty=False)
    (tmp_path / "f").write_text("y")
    assert repo_state(tmp_path).dirty
