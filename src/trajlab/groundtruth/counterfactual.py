"""Counterfactual fixes and file histories (ADR-0013, decision 5).

`try_fix` applies a unified diff (paths relative to the container root, `a/` and `b/` prefixes,
as `git diff` writes them) to a trial's final state and regrades the result:

- `artifacts` mode patches the final state's graded files directly;
- `environment` mode starts a container from the last checkpoint's image (which gate 1 shows
  holds the final graded files), patches any file in it, optionally runs a command to regenerate
  outputs, and collects the artifacts again exactly as extraction does.

Every try is recorded as a `FixRecord` in `groundtruth/fixes.jsonl`, its diff is kept under
`groundtruth/patches/`, and its regrade is a `counterfactual` replay. A fix only becomes ground
truth through `trajlab.groundtruth.items`, which checks its replay.

`file_history` reads one file at every timeline point, for blame.
"""

import asyncio
import fcntl
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from harbor.models.task.config import TaskConfig
from harbor.models.trial.artifact_manifest import ArtifactManifest
from harbor.models.trial.paths import TrialPaths
from harbor.trial.artifact_handler import artifact_host_path

from trajlab.contracts.groundtruth import (
    FIX_RECORDS_FILENAME,
    ORACLE_FILENAME,
    PATCHES_DIRNAME,
    FixFile,
    FixMode,
    FixRecord,
    OracleRecord,
    TimelinePoint,
)
from trajlab.groundtruth.admission import Claim, held
from trajlab.groundtruth.blame import fix_hunks, split_lines
from trajlab.groundtruth.checks import PASSED, read_checks, statuses
from trajlab.groundtruth.extract import (
    MANIFEST_FILENAME,
    ExtractionError,
    ImageReader,
    TrialInputs,
    collect_artifact,
    groundtruth_dir,
    read_manifest,
    read_points,
    staging_dir,
    state_dir,
    store_state,
)
from trajlab.groundtruth.replay import read_records, replay
from trajlab.groundtruth.timeline import FLAKY, check_timelines

log = logging.getLogger(__name__)

OUTPUT_CHARS = 4000
DEFAULT_COMMAND_TIMEOUT_S = 900.0


class FixError(ValueError):
    pass


def fixes_path(trial_dir: Path) -> Path:
    return groundtruth_dir(trial_dir) / FIX_RECORDS_FILENAME


def read_fixes(trial_dir: Path) -> list[FixRecord]:
    path = fixes_path(trial_dir)
    if not path.is_file():
        return []
    return [FixRecord.model_validate_json(line) for line in path.read_text().splitlines()]


def _append_fix(trial_dir: Path, record: FixRecord) -> None:
    path = fixes_path(trial_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(record.model_dump_json() + "\n")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_lines(path: Path) -> list[str] | None:
    """A file's lines as blame and diffs see them (one decoding everywhere)."""
    if not path.is_file():
        return None
    return split_lines(path.read_bytes().decode("utf-8", errors="surrogateescape"))


def patch_paths(patch: Path) -> list[str]:
    """The container paths a diff touches, without the leading slash; renames are refused."""
    listed = subprocess.run(
        ["git", "apply", "--numstat", "-p1", str(patch)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tempfile.gettempdir(),
    )
    if listed.returncode != 0:
        raise FixError(f"not a diff git can apply: {listed.stderr.strip()}")
    paths = []
    for line in listed.stdout.splitlines():
        added, removed, path = line.split("\t", 2)
        if added == "-" or removed == "-":
            raise FixError(f"{path}: binary diffs are not supported")
        if " => " in path:
            raise FixError(f"{path}: renames are not supported")
        if path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise FixError(f"{path}: paths are relative to the container root, without '..'")
        paths.append(path)
    if not paths:
        raise FixError("the diff changes nothing")
    return paths


def _apply(patch: Path, root: Path) -> None:
    applied = subprocess.run(
        ["git", "apply", "-p1", "--whitespace=nowarn", str(patch.resolve())],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    if applied.returncode != 0:
        raise FixError(f"the diff does not apply: {applied.stderr.strip()}")


def artifact_roots(inputs: TrialInputs) -> list[str]:
    """Each declared artifact's path relative to the container root (convention dir excluded)."""
    roots = []
    for artifact in inputs.artifacts:
        if artifact.source.rstrip("/") == inputs.convention.rstrip("/"):
            continue
        roots.append(artifact_host_path(Path("."), artifact).as_posix().rstrip("/"))
    return roots


def in_artifacts(path: str, inputs: TrialInputs) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in artifact_roots(inputs))


def _file_changes(before: dict[str, list[str] | None], after: dict[str, list[str] | None]):
    changes = []
    for path, old in before.items():
        hunks = fix_hunks(old or [], after[path] or [])
        changes.append(
            FixFile(
                path=f"/{path}",
                existed=old is not None,
                removed=tuple(line for h in hunks for line in h.removed),
                inserted_before=tuple(h.inserted_before for h in hunks if h.inserted_before),
                added=sum(h.added for h in hunks),
            )
        )
    return tuple(changes)


def _resync_manifest(artifacts_dir: Path) -> None:
    """After a diff created or deleted an artifact, record it as Harbor's collection would."""
    entries = read_manifest(artifacts_dir)
    updated = []
    for entry in entries:
        target = artifacts_dir.parent / entry.destination
        if entry.status == "failed" and target.exists():
            kind = "directory" if target.is_dir() else "file"
            entry = entry.model_copy(update={"status": "ok", "type": kind})
        elif entry.status == "ok" and not target.exists():
            entry = entry.model_copy(update={"status": "failed"})
        updated.append(entry)
    (artifacts_dir / MANIFEST_FILENAME).write_text(
        json.dumps(ArtifactManifest(entries=updated).to_json_data(), indent=2)
    )


def last_checkpoint(points: list[TimelinePoint]) -> TimelinePoint:
    checkpoints = [p for p in points if p.kind == "checkpoint"]
    if not checkpoints:
        raise FixError("the trial has no checkpoint to start a container from")
    last = checkpoints[-1]
    if last.state_id != points[-1].state_id:
        raise FixError("the last checkpoint does not hold the final state (gate 1 failed)")
    return last


def base_statuses(trial_dir: Path, points: list[TimelinePoint]) -> dict[str, str]:
    """Each check's status at the final point; flaky checks are left out."""
    recorded = statuses(read_checks(TrialPaths(trial_dir).verifier_dir))
    final = {t.key: t.final for t in check_timelines(points, read_records(trial_dir), recorded)}
    return {key: status for key, status in final.items() if status and status != FLAKY}


def agent_resources(inputs: TrialInputs) -> tuple[float, int]:
    """The task's declared agent-environment CPUs and memory, for a fix's container."""
    if inputs.task_dir is None:
        return 2.0, 4096
    config = TaskConfig.model_validate_toml((inputs.task_dir / "task.toml").read_text())
    return float(config.environment.cpus or 2), int(config.environment.memory_mb or 4096)


def try_fix(
    inputs: TrialInputs,
    patch: Path,
    *,
    mode: FixMode,
    command: str | None = None,
    user: str = "root",
    workdir: str | None = None,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
) -> FixRecord:
    """Apply a diff to the trial's final state, regrade it, and record the try."""
    trial_dir = inputs.trial_dir
    points = read_points(trial_dir)
    final = points[-1]
    text = patch.read_bytes()
    sha = hashlib.sha256(text).hexdigest()
    kept = groundtruth_dir(trial_dir) / PATCHES_DIRNAME / f"{sha}.diff"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(text)
    fix_id = f"fix-{uuid.uuid4().hex[:10]}"
    common = {
        "fix_id": fix_id,
        "trial_name": trial_dir.name,
        "mode": mode,
        "base_state_id": final.state_id,
        "patch_sha256": sha,
        "command": command,
        "command_user": user if command else None,
        "command_workdir": workdir if command else None,
        "command_timeout_s": timeout if command else None,
    }

    def failure(error: str, **extra: object) -> FixRecord:
        record = FixRecord(**common, **extra, error=error, created_at=datetime.now(UTC))
        _append_fix(trial_dir, record)
        return record

    if mode == "environment" and command is not None and not command.strip():
        command = None
    try:
        paths = patch_paths(kept)
        if mode == "artifacts":
            if command is not None:
                raise FixError("a command runs only in environment mode")
            outside = [p for p in paths if not in_artifacts(p, inputs)]
            if outside:
                raise FixError(f"not graded artifacts (use environment mode): {outside}")
        else:
            common["base_image"] = last_checkpoint(points).image
    except FixError as error:
        return failure(str(error))

    staged = staging_dir(trial_dir)
    exit_code: int | None = None
    output: str | None = None
    try:
        if mode == "artifacts":
            root = staged / "artifacts"
            shutil.copytree(state_dir(trial_dir, final.state_id) / "artifacts", root, symlinks=True)
            before = {p: read_lines(root / p) for p in paths}
            _apply(kept, root)
            after = {p: read_lines(root / p) for p in paths}
            _resync_manifest(root)
        else:
            root = staged / "root"
            cpus, memory_mb = agent_resources(inputs)
            reader = ImageReader(
                str(common["base_image"]), purpose="fix", cpus=cpus, memory_mb=memory_mb
            )
            claim = Claim(claim_id=fix_id, cpus=cpus, memory_mb=memory_mb, container=reader.name)
            with held(claim), reader as box:
                for path in paths:
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    try:
                        box.copy_out(f"/{path}", root / path)
                    except ExtractionError:
                        pass  # a file the diff creates
                before = {p: read_lines(root / p) for p in paths}
                _apply(kept, root)
                after = {p: read_lines(root / p) for p in paths}
                for path in paths:
                    if (root / path).exists():
                        box.copy_in(root / path, f"/{path}")
                    else:
                        box.exec_root(f"rm -f '/{path}'")
                if command:
                    ran = box.run(command, user=user, workdir=workdir, timeout=timeout)
                    exit_code = ran.returncode
                    output = ((ran.stdout or "") + (ran.stderr or ""))[-OUTPUT_CHARS:]
                into = staged / "artifacts"
                into.mkdir()
                entries = [
                    collect_artifact(box, into, a, inputs.convention) for a in inputs.artifacts
                ]
                (into / MANIFEST_FILENAME).write_text(
                    json.dumps(ArtifactManifest(entries=entries).to_json_data(), indent=2)
                )
            shutil.rmtree(root)
        state = store_state(trial_dir, staged, inputs.task_name)
    except (FixError, ExtractionError) as error:
        shutil.rmtree(staged, ignore_errors=True)
        return failure(str(error), command_exit_code=exit_code, command_output=output)

    base = base_statuses(trial_dir, points)
    record = asyncio.run(
        replay(
            trial_dir,
            state.state_id,
            "counterfactual",
            base_state_id=final.state_id,
            patch_sha256=sha,
        )
    )
    now = statuses(record.checks)
    failing = sorted(k for k, s in base.items() if s != PASSED)
    if record.outcome != "verdict":
        result = FixRecord(
            **common,
            command_exit_code=exit_code,
            command_output=output,
            files=_file_changes(before, after),
            state_id=state.state_id,
            replay_id=record.replay_id,
            error=f"the fix's replay ended without a verdict ({record.outcome})",
            still_failing=tuple(failing),
            created_at=datetime.now(UTC),
        )
        _append_fix(trial_dir, result)
        return result
    fixed = tuple(k for k in failing if now.get(k) == PASSED)
    result = FixRecord(
        **common,
        command_exit_code=exit_code,
        command_output=output,
        files=_file_changes(before, after),
        state_id=state.state_id,
        replay_id=record.replay_id,
        fixed=fixed,
        broken=tuple(sorted(k for k, s in base.items() if s == PASSED and now.get(k) != PASSED)),
        still_failing=tuple(k for k in failing if k not in fixed),
        created_at=datetime.now(UTC),
    )
    _append_fix(trial_dir, result)
    return result


# --- file histories ------------------------------------------------------------------------


def read_from_image(image: str, path: str) -> list[str] | None:
    """One file of an image, without starting a container; None if it is not a file there."""
    created = subprocess.run(
        ["docker", "create", "--entrypoint", "/bin/sh", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        raise ExtractionError(f"cannot create a container of {image}: {created.stderr.strip()}")
    container = created.stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file"
            copied = subprocess.run(
                ["docker", "cp", f"{container}:{path}", str(target)],
                capture_output=True,
                check=False,
            )
            if copied.returncode != 0 or not target.is_file():
                return None
            return read_lines(target)
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)


def file_history(inputs: TrialInputs, path: str) -> list[tuple[TimelinePoint, list[str] | None]]:
    """A container path's lines at every timeline point (None where absent).

    Graded artifacts are read from the extracted states, the final point included; any other
    file is read from each point's image, and the final point is left out, since only graded
    artifacts were recorded after the agent ended.
    """
    trial_dir = inputs.trial_dir
    relative = path.lstrip("/")
    graded = in_artifacts(relative, inputs)
    history = []
    by_image: dict[str, list[str] | None] = {}
    for point in read_points(trial_dir):
        if graded:
            lines = read_lines(state_dir(trial_dir, point.state_id) / "artifacts" / relative)
        elif point.image is None:
            continue
        else:
            if point.image not in by_image:
                by_image[point.image] = read_from_image(point.image, f"/{relative}")
            lines = by_image[point.image]
        history.append((point, lines))
    return history


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def text_from_image(image: str, path: str) -> str | None:
    """One file of an image as text, without starting a container."""
    created = subprocess.run(
        ["docker", "create", "--entrypoint", "/bin/sh", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        raise ExtractionError(f"cannot create a container of {image}: {created.stderr.strip()}")
    container = created.stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file"
            copied = subprocess.run(
                ["docker", "cp", f"{container}:{path}", str(target)],
                capture_output=True,
                check=False,
            )
            return read_text(target) if copied.returncode == 0 else None
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)


def fix_contents(inputs: TrialInputs, fix: FixRecord) -> dict[str, tuple[str | None, str | None]]:
    """For every file a fix's diff touches: its text in the fix's base, and after the diff."""
    trial_dir = inputs.trial_dir
    patch = groundtruth_dir(trial_dir) / PATCHES_DIRNAME / f"{fix.patch_sha256}.diff"
    paths = patch_paths(patch)
    contents: dict[str, tuple[str | None, str | None]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for path in paths:
            if fix.mode == "artifacts":
                before = read_text(state_dir(trial_dir, fix.base_state_id) / "artifacts" / path)
            else:
                assert fix.base_image is not None
                before = text_from_image(fix.base_image, f"/{path}")
            if before is not None:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_bytes(before.encode("utf-8", errors="surrogateescape"))
            contents[path] = (before, None)
        _apply(patch, root)
        for path in paths:
            contents[path] = (contents[path][0], read_text(root / path))
    return contents


ORACLE_TIMEOUT_S = 1800.0


def run_oracle(inputs: TrialInputs, *, timeout: float = ORACLE_TIMEOUT_S) -> OracleRecord:
    """Run the task's reference solution on the trial's initial image and regrade the result.

    A check of the replay harness (ADR-0013): if the reference solution does not pass here, a
    failing replay of this task says little. The container has network access, since reference
    solutions may install packages; nothing it makes becomes ground truth.
    """
    trial_dir = inputs.trial_dir
    points = read_points(trial_dir)
    image = points[0].image or ""
    common = {"trial_name": trial_dir.name, "image": image}

    def record(**fields: object) -> OracleRecord:
        result = OracleRecord(**common, **fields, created_at=datetime.now(UTC))
        path = groundtruth_dir(trial_dir) / ORACLE_FILENAME
        path.write_text(result.model_dump_json(indent=2) + "\n")
        return result

    solution = inputs.task_dir / "solution" if inputs.task_dir else None
    if solution is None or not (solution / "solve.sh").is_file():
        return record(error="the task has no solution/solve.sh")
    cpus, memory_mb = agent_resources(inputs)
    reader = ImageReader(image, purpose="oracle", cpus=cpus, memory_mb=memory_mb, network="bridge")
    claim = Claim(
        claim_id=f"oracle-{uuid.uuid4().hex[:10]}",
        cpus=cpus,
        memory_mb=memory_mb,
        container=reader.name,
    )
    staged = staging_dir(trial_dir)
    try:
        with held(claim), reader as box:
            box.copy_dir_in(solution, "/solution")
            ran = box.run("bash /solution/solve.sh", user="root", workdir=None, timeout=timeout)
            exit_code = ran.returncode
            output = ((ran.stdout or "") + (ran.stderr or ""))[-OUTPUT_CHARS:]
            into = staged / "artifacts"
            into.mkdir()
            entries = [collect_artifact(box, into, a, inputs.convention) for a in inputs.artifacts]
            (into / MANIFEST_FILENAME).write_text(
                json.dumps(ArtifactManifest(entries=entries).to_json_data(), indent=2)
            )
        state = store_state(trial_dir, staged, inputs.task_name)
    except ExtractionError as error:
        shutil.rmtree(staged, ignore_errors=True)
        return record(error=str(error))
    replayed = asyncio.run(replay(trial_dir, state.state_id, "oracle"))
    failed = tuple(sorted(k for k, s in statuses(replayed.checks).items() if s != PASSED))
    return record(
        command_exit_code=exit_code,
        command_output=output,
        state_id=state.state_id,
        replay_id=replayed.replay_id,
        reward=replayed.reward,
        failed_checks=failed,
    )
