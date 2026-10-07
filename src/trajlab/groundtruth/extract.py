"""Artifact states: the graded artifacts' bytes at each timeline point (ADR-0013, decision 2).

A trial's timeline is its `initial` point (the derived image it started from), one point per
checkpoint in `seq` order, and its `final` point (Harbor's recorded `artifacts/`). At every point
but `final`, the task's declared artifacts are collected from the image the way Harbor collects
them from the agent's container at the end of a trial (`harbor/trial/artifact_handler.py`,
`_download_artifact`): `test -d` as root decides file or directory; files and directories without
exclusions are copied with `docker cp`; directories with exclusions travel as the same
`tar czf --exclude=...` archive Harbor makes and are extracted with the `data` filter; an artifact
that cannot be collected is recorded as `failed`. The host layout (`artifact_host_path`) and the
`artifacts/manifest.json` entries are Harbor's, so Harbor's regrade can read a state dir as a
trial dir. Identical states are stored once, under their `state_id`.
"""

import hashlib
import json
import logging
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from harbor.models.task.artifacts import is_convention_entry, with_convention_entry
from harbor.models.task.config import ArtifactConfig
from harbor.models.task.task import Task
from harbor.models.trial.artifact_manifest import ArtifactManifest, ArtifactManifestEntry
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths
from harbor.trial.artifact_handler import artifact_host_path

from trajlab.contracts.checkpoint import (
    CHECKPOINT_RECORDS_FILENAME,
    CHECKPOINTS_DIRNAME,
    CheckpointRecord,
)
from trajlab.contracts.groundtruth import (
    GROUNDTRUTH_DIRNAME,
    POINTS_FILENAME,
    STATE_FILENAME,
    STATES_DIRNAME,
    ArtifactState,
    StateEntry,
    StateFile,
    TimelinePoint,
)
from trajlab.contracts.preinstall import PREINSTALL_RECORD_FILENAME, PreinstallRecord

log = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"  # Harbor's, at the top of artifacts/
# Label on every container this module starts, so a crashed run's leftovers can be found.
CONTAINER_LABEL = "trajlab.groundtruth"
# Harbor packs excluded-directory transfers here in the container (environments/base.py).
TRANSFER_TAR_DIR = PurePosixPath("/tmp")
DOCKER_TIMEOUT_S = 600


class ExtractionError(RuntimeError):
    pass


def groundtruth_dir(trial_dir: Path) -> Path:
    return trial_dir / GROUNDTRUTH_DIRNAME


def state_dir(trial_dir: Path, state_id: str) -> Path:
    return groundtruth_dir(trial_dir) / STATES_DIRNAME / state_id


def declared_artifacts(task: Task, trial_config: TrialConfig) -> tuple[list[ArtifactConfig], str]:
    """The artifacts Harbor collects for this trial, convention dir included, and that dir."""
    convention = EnvironmentPaths.for_os(task.config.environment.os).artifacts_dir.as_posix()
    entries = with_convention_entry(
        [*task.config.artifacts, *trial_config.artifacts], convention_source=convention
    )
    return entries, convention


# --- reading an image ----------------------------------------------------------------------


def _docker(*args: str, timeout: float = DOCKER_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


STALE_EXTRACT_S = 3600  # an extraction container this old was left by a killed process


def remove_stale_containers() -> int:
    """Remove extraction containers older than `STALE_EXTRACT_S` (left by a killed extract)."""
    listed = _docker(
        "ps",
        "--all",
        "--filter",
        f"label={CONTAINER_LABEL}=extract",
        "--format",
        "{{.ID}} {{.CreatedAt}}",
    )
    removed = 0
    now = datetime.now(UTC)
    for line in listed.stdout.splitlines():
        container, created = line.split(" ", 1)
        stamp = datetime.strptime(" ".join(created.split()[:2]), "%Y-%m-%d %H:%M:%S")
        if (now - stamp.replace(tzinfo=UTC)).total_seconds() > STALE_EXTRACT_S:
            _docker("rm", "--force", container, timeout=120)
            removed += 1
    return removed


class ImageReader:
    """A throwaway container of an image, with no network, removed on exit.

    Extraction only reads from it; a counterfactual fix also writes files into it and runs a
    command in it (`trajlab.groundtruth.counterfactual`). The image itself never changes.
    """

    def __init__(
        self,
        image: str,
        *,
        purpose: str = "extract",
        cpus: float | None = None,
        memory_mb: int | None = None,
        network: str = "none",
    ) -> None:
        self.image = image
        self.purpose = purpose
        self.network = network
        self.limits = (["--cpus", str(cpus)] if cpus else []) + (
            ["--memory", f"{memory_mb}m"] if memory_mb else []
        )
        self.name = f"trajlab-gt-{uuid.uuid4().hex[:12]}"

    def __enter__(self) -> "ImageReader":
        started = _docker(
            "run",
            "--detach",
            "--name",
            self.name,
            "--network",
            self.network,
            "--label",
            f"{CONTAINER_LABEL}={self.purpose}",
            *self.limits,
            "--entrypoint",
            "/bin/sh",
            self.image,
            "-c",
            "while :; do sleep 3600; done",
        )
        if started.returncode != 0:
            raise ExtractionError(f"cannot start {self.image}: {started.stderr.strip()}")
        return self

    def __exit__(self, *exc: object) -> None:
        _docker("rm", "--force", self.name, timeout=120)

    def exec_root(self, command: str) -> subprocess.CompletedProcess[str]:
        return _docker("exec", "--user", "0", self.name, "/bin/sh", "-c", command)

    def is_dir(self, path: str) -> bool:
        return self.exec_root(f"test -d {shlex.quote(path)}").returncode == 0

    def copy_out(self, source: str, target: Path) -> None:
        copied = _docker("cp", f"{self.name}:{source}", str(target))
        if copied.returncode != 0:
            raise ExtractionError(f"docker cp {source}: {copied.stderr.strip()}")

    def copy_in(self, source: Path, target: str) -> None:
        parent = PurePosixPath(target).parent.as_posix()
        self.exec_root(f"mkdir -p {shlex.quote(parent)}")
        copied = _docker("cp", str(source), f"{self.name}:{target}")
        if copied.returncode != 0:
            raise ExtractionError(f"docker cp into {target}: {copied.stderr.strip()}")

    def copy_dir_in(self, source: Path, target: str) -> None:
        self.exec_root(f"mkdir -p {shlex.quote(target)}")
        copied = _docker("cp", f"{source}/.", f"{self.name}:{target}")
        if copied.returncode != 0:
            raise ExtractionError(f"docker cp into {target}: {copied.stderr.strip()}")

    def run(
        self, command: str, *, user: str, workdir: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Run a shell command in the container; a timeout returns exit code 124."""
        args = ["exec", "--user", user]
        if workdir:
            args += ["--workdir", workdir]
        try:
            return _docker(*args, self.name, "/bin/sh", "-c", command, timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            # Every process but PID 1 (the keep-alive loop); the container is discarded anyway.
            self.exec_root("kill -KILL -1 2>/dev/null; true")
            output = expired.stdout or b""  # bytes even with text=True
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            return subprocess.CompletedProcess(args, 124, output, "timed out")

    def copy_dir_with_exclusions(self, source: str, target: Path, exclude: list[str]) -> None:
        """Harbor's `_download_dir_with_exclusions_impl`, against this container."""
        flags = " ".join(f"--exclude={shlex.quote(pattern)}" for pattern in exclude)
        name = f".hb-transfer-{uuid.uuid4()}.tar.gz"
        env_tar = str(TRANSFER_TAR_DIR / name)
        packed = self.exec_root(
            f"tar czf {shlex.quote(env_tar)} {flags} -C {shlex.quote(source)} ."
        )
        if packed.returncode != 0:
            raise ExtractionError(f"tar {source}: {(packed.stderr or packed.stdout).strip()}")
        with tempfile.TemporaryDirectory() as tmp:
            host_tar = Path(tmp) / name
            self.copy_out(env_tar, host_tar)
            with tarfile.open(host_tar, "r:gz") as archive:
                archive.extractall(path=target, filter="data")
        self.exec_root(f"rm -f {shlex.quote(env_tar)}")


def _manifest_destination(artifacts_dir: Path, target: Path) -> str:
    if target == artifacts_dir:
        return "artifacts"
    return f"artifacts/{target.relative_to(artifacts_dir).as_posix()}"


def collect_artifact(
    reader: ImageReader, artifacts_dir: Path, artifact: ArtifactConfig, convention: str
) -> ArtifactManifestEntry:
    """One artifact, as `ArtifactHandler._download_artifact` collects it from a live container."""
    source = artifact.source
    target = artifact_host_path(artifacts_dir, artifact)
    destination = _manifest_destination(artifacts_dir, target)
    exclude = list(artifact.exclude)
    if is_convention_entry(artifact, convention) and not artifact.destination and not exclude:
        # In a trial this dir is a bind mount, which Harbor records from the host side and a
        # commit never contains; the agent's writes there are not recoverable per checkpoint.
        target.mkdir(parents=True, exist_ok=True)
        status = "ok" if any(target.iterdir()) else "empty"
        return ArtifactManifestEntry(
            source=source, destination=destination, type="directory", status=status, exclude=[]
        )
    try:
        is_dir = reader.is_dir(source)
    except Exception:
        is_dir = not PurePosixPath(source).suffix
    try:
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            if exclude:
                reader.copy_dir_with_exclusions(source, target, exclude)
            else:
                reader.copy_out(f"{source}/.", target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            reader.copy_out(source, target)
        status = "ok"
    except Exception as error:
        log.debug("collecting %s from %s failed: %s", source, reader.image, error)
        status = "failed"
    return ArtifactManifestEntry(
        source=source,
        destination=destination,
        type="directory" if is_dir else "file",
        status=status,
        exclude=exclude if is_dir else [],
    )


def collect_image(image: str, artifacts: list[ArtifactConfig], convention: str, into: Path) -> None:
    """Collect every declared artifact of `image` into `into` (a state's artifacts/ dir)."""
    into.mkdir(parents=True, exist_ok=True)
    with ImageReader(image) as reader:
        entries = [collect_artifact(reader, into, a, convention) for a in artifacts]
    (into / MANIFEST_FILENAME).write_text(
        json.dumps(ArtifactManifest(entries=entries).to_json_data(), indent=2)
    )


# --- states --------------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def list_files(artifacts_dir: Path) -> list[StateFile]:
    """Every path under a state's artifacts/ dir but Harbor's manifest; links are not followed."""
    found: list[StateFile] = []
    for root, dirs, names in os.walk(artifacts_dir):
        here = Path(root)
        for name in [*dirs, *names]:
            path = here / name
            relative = path.relative_to(artifacts_dir).as_posix()
            if relative == MANIFEST_FILENAME:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                found.append(StateFile(path=relative, kind="symlink", target=os.readlink(path)))
            elif stat.S_ISDIR(info.st_mode):
                found.append(StateFile(path=relative, kind="directory"))
            elif stat.S_ISREG(info.st_mode):
                found.append(
                    StateFile(
                        path=relative,
                        kind="file",
                        size=info.st_size,
                        sha256=_sha256_file(path),
                        executable=bool(info.st_mode & 0o111),
                    )
                )
            else:
                raise ExtractionError(f"{path}: not a file, directory, or symlink")
    return sorted(found, key=lambda f: f.path)


def read_manifest(artifacts_dir: Path) -> list[ArtifactManifestEntry]:
    data = json.loads((artifacts_dir / MANIFEST_FILENAME).read_text())
    return [ArtifactManifestEntry.model_validate(item) for item in data]


def describe_state(artifacts_dir: Path, task_name: str) -> ArtifactState:
    """The `ArtifactState` of an artifacts/ dir; `state_id` hashes entries and files."""
    entries = sorted(
        (
            StateEntry(source=e.source, type=e.type, status=e.status)
            for e in read_manifest(artifacts_dir)
        ),
        key=lambda e: e.source,
    )
    files = list_files(artifacts_dir)
    canonical = json.dumps(
        {
            "entries": [e.model_dump(mode="json") for e in entries],
            "files": [f.model_dump(mode="json") for f in files],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    state_id = hashlib.sha256(canonical.encode()).hexdigest()
    return ArtifactState(state_id=state_id, task_name=task_name, entries=entries, files=files)


def store_state(trial_dir: Path, staged: Path, task_name: str) -> ArtifactState:
    """Move a staged state dir (holding `artifacts/`) into place under its id; dedupe."""
    state = describe_state(staged / "artifacts", task_name)
    final_dir = state_dir(trial_dir, state.state_id)
    if final_dir.is_dir():
        shutil.rmtree(staged)
        return state
    (staged / STATE_FILENAME).write_text(state.model_dump_json(indent=2) + "\n")
    # Harbor's regrade reads the source trial's result.json (task name, agent info).
    shutil.copy2(TrialPaths(trial_dir).result_path, staged / "result.json")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        staged.rename(final_dir)
    except OSError:
        if not final_dir.is_dir():
            raise
        shutil.rmtree(staged)  # another process stored the same state meanwhile
    return state


def read_state(trial_dir: Path, state_id: str) -> ArtifactState:
    path = state_dir(trial_dir, state_id) / STATE_FILENAME
    return ArtifactState.model_validate_json(path.read_text())


def staging_dir(trial_dir: Path) -> Path:
    staging = groundtruth_dir(trial_dir) / STATES_DIRNAME / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=staging))


def state_from_image(
    trial_dir: Path, image: str, artifacts: list[ArtifactConfig], convention: str, task_name: str
) -> ArtifactState:
    staged = staging_dir(trial_dir)
    try:
        collect_image(image, artifacts, convention, staged / "artifacts")
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return store_state(trial_dir, staged, task_name)


def state_from_recorded(trial_dir: Path, task_name: str) -> ArtifactState:
    """The `final` state: Harbor's own `artifacts/`, copied as recorded."""
    staged = staging_dir(trial_dir)
    shutil.copytree(TrialPaths(trial_dir).artifacts_dir, staged / "artifacts", symlinks=True)
    return store_state(trial_dir, staged, task_name)


# --- timelines -----------------------------------------------------------------------------


def checkpoint_records(trial_dir: Path) -> list[CheckpointRecord]:
    path = TrialPaths(trial_dir).agent_dir / CHECKPOINTS_DIRNAME / CHECKPOINT_RECORDS_FILENAME
    if not path.is_file():
        return []
    records = [CheckpointRecord.model_validate_json(line) for line in path.read_text().splitlines()]
    return sorted(records, key=lambda r: r.seq)


def initial_image(trial_dir: Path) -> str:
    record = PreinstallRecord.model_validate_json(
        (trial_dir / PREINSTALL_RECORD_FILENAME).read_text()
    )
    return record.image_id


@dataclass(frozen=True)
class TrialInputs:
    """What extraction needs to know about one trial."""

    trial_dir: Path
    task_name: str
    artifacts: list[ArtifactConfig]
    convention: str
    task_dir: Path | None = None  # the task package (instruction, tests, solution)


def points_path(trial_dir: Path) -> Path:
    return groundtruth_dir(trial_dir) / POINTS_FILENAME


def read_points(trial_dir: Path) -> list[TimelinePoint]:
    path = points_path(trial_dir)
    return [TimelinePoint.model_validate_json(line) for line in path.read_text().splitlines()]


def extract_timeline(inputs: TrialInputs, *, force: bool = False) -> list[TimelinePoint]:
    """Extract every point's state and write the trial's `points.jsonl`; reuse it if present."""
    trial_dir = inputs.trial_dir
    path = points_path(trial_dir)
    if path.is_file() and not force:
        return read_points(trial_dir)
    trial_name = trial_dir.name

    def from_image(image: str) -> str:
        state = state_from_image(
            trial_dir, image, inputs.artifacts, inputs.convention, inputs.task_name
        )
        return state.state_id

    image = initial_image(trial_dir)
    points = [
        TimelinePoint(
            trial_name=trial_name, index=0, kind="initial", image=image, state_id=from_image(image)
        )
    ]
    for record in checkpoint_records(trial_dir):
        points.append(
            TimelinePoint(
                trial_name=trial_name,
                index=len(points),
                kind="checkpoint",
                seq=record.seq,
                tool_call_id=record.tool_call_id,
                covered_tool_call_ids=record.covered_tool_call_ids,
                trigger=record.trigger,
                image=record.checkpoint_id,
                state_id=from_image(record.checkpoint_id),
                captured_at=record.captured_at,
            )
        )
    final = state_from_recorded(trial_dir, inputs.task_name)
    points.append(
        TimelinePoint(
            trial_name=trial_name, index=len(points), kind="final", state_id=final.state_id
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".tmp")
    staged.write_text("".join(p.model_dump_json() + "\n" for p in points))
    staged.rename(path)
    try:  # left only if empty; a concurrent try-fix may still be staging a state
        (groundtruth_dir(trial_dir) / STATES_DIRNAME / ".staging").rmdir()
    except OSError:
        pass
    return points
