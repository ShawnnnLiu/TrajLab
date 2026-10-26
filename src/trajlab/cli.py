"""trajlab command line entry point."""

import asyncio
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn, get_args

import duckdb
import typer
from harbor.utils.env import parse_bool_env_value

from trajlab.atif.load import trajectory_path
from trajlab.atif.postprocess import enriched_trajectory_path, postprocess_trial
from trajlab.atif.validate import validate_trajectory
from trajlab.capture.corpus import (
    ManifestError,
    build_manifest,
    repo_root,
    repo_state,
    write_manifest,
)
from trajlab.capture.discover import compose_project_name, iter_trial_dirs, load_trial_config
from trajlab.capture.harbor_runner import RunRefusedError, execute, plan_run, repo_relative
from trajlab.capture.labels import DEFAULT_LABELS_DIR, LabelStore
from trajlab.capture.pins import CLAUDE_CODE_MAX_OUTPUT_TOKENS
from trajlab.capture.repair_launcher import (
    INPUTS_DIRNAME,
    Launcher,
    RepairJob,
    check_sources,
    default_arms,
    load_result,
    resume_env,
    save_keep_reason,
    trial_reward,
    waypoint_record,
)
from trajlab.capture.trace_server import LOCAL_HOSTS, TraceSite, serve
from trajlab.capture.waypoint import DEFAULT_STATE_DIR, Waypoint
from trajlab.checkpoint.backends.docker_commit import DockerCommitBackend
from trajlab.checkpoint.watcher import (
    DEFAULT_SWEEP_INTERVAL_S,
    TrialIdentity,
    Watcher,
    WatcherLockedError,
    hold_watcher_lock,
    watcher_running,
)
from trajlab.contracts import CheckpointRecord, RepairArm
from trajlab.contracts.groundtruth import (
    GROUNDTRUTH_DIRNAME,
    POINTS_FILENAME,
    REFUTATIONS_FILENAME,
    RefutationRecord,
    TrialCauses,
)
from trajlab.groundtruth.blame import blame
from trajlab.groundtruth.counterfactual import file_history, read_fixes, run_oracle, try_fix
from trajlab.groundtruth.extract import TrialInputs, remove_stale_containers
from trajlab.groundtruth.gates import excluded_checks, job_report
from trajlab.groundtruth.items import (
    build_items,
    build_progress,
    fix_confirms,
    labels_path,
    read_items,
    regressions,
    trial_timeline,
)
from trajlab.groundtruth.manifest import build_manifest as build_gt_manifest
from trajlab.groundtruth.minimize import (
    minimize_fix,
    read_minimized,
    read_reverts,
    revert_regression,
)
from trajlab.groundtruth.replay import FIX_SAMPLES, QUIET_FIX_SAMPLES, reparse_records
from trajlab.groundtruth.run import (
    confirm_fixes,
    extract_job,
    finished_trial_dirs,
    planned_counts,
    replay_job,
    trial_inputs,
)
from trajlab.groundtruth.show import brief, calls
from trajlab.groundtruth.summary import markdown as summary_markdown
from trajlab.groundtruth.summary import write_review_sheet
from trajlab.groundtruth.traits import traits
from trajlab.store.build import BuildError, BuildResult, build_trial, start_image, store_meta
from trajlab.store.docker_images import DockerImageProvider
from trajlab.store.export import export as export_bundle
from trajlab.store.export import verify as verify_bundle
from trajlab.store.schema import UPGRADED_FROM_1, replace_views

app = typer.Typer(help="Capture Claude Code trajectories on Harbor with environment checkpoints.")
store_app = typer.Typer(help="The Exp 2 store: steps joined to filesystem deltas (ADR-0014).")
app.add_typer(store_app, name="store")

MANIFESTS_DIR = Path("corpus/manifests")
DEFAULT_ENV_FILE = Path(".env")
# Any one authenticates Claude Code in a trial (Harbor's ClaudeCode._resolve_auth_env and
# MODEL_CONNECTION.api_key_envs); Bedrock is not used here.
CREDENTIAL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

CorpusIdOption = Annotated[
    str | None,
    typer.Option(help="Manifest name under corpus/manifests/. Default: the config file's stem."),
]
StorageOption = Annotated[
    str | None, typer.Option(help="Where the job dirs are kept outside git (corpus/README.md).")
]
ManifestsDirOption = Annotated[Path, typer.Option(help="Directory the manifest is written to.")]


@app.callback()
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@app.command()
def run(
    config: Annotated[Path, typer.Argument(help="Harbor job config, e.g. configs/harbor/x.json.")],
    corpus_id: CorpusIdOption = None,
    env_file: Annotated[
        Path | None,
        typer.Option(help="Credentials file passed to Harbor. Default: .env if it exists."),
    ] = None,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Run with uncommitted changes (throwaway runs).")
    ] = False,
    storage: StorageOption = None,
    manifests_dir: ManifestsDirOption = MANIFESTS_DIR,
) -> None:
    """Run a Harbor job from a config file and record a corpus manifest."""
    if env_file is None and DEFAULT_ENV_FILE.is_file():
        env_file = DEFAULT_ENV_FILE
    try:
        plan = plan_run(
            config,
            repo_dir=Path.cwd(),
            corpus_id=corpus_id,
            manifests_dir=manifests_dir,
            env_file=env_file,
            allow_dirty=allow_dirty,
            watcher_running=watcher_running,
        )
    except RunRefusedError as error:
        typer.echo(f"trajlab run: {error}", err=True)
        raise typer.Exit(code=1) from error
    raise typer.Exit(code=execute(plan, storage=storage))


class Arm(StrEnum):
    fresh = "fresh"
    state = "state"
    state_traj = "state-traj"
    traj = "traj"
    traj_text = "traj-text"
    state_files = "state-files"
    state_live = "state-live"
    state_live_traj = "state-live-traj"


def trial_env_problem(env_file: Path | None) -> str | None:
    """Why trials started with this env file would run unauthenticated, or without ADR-0005's
    output-token cap; None if neither. Reads the environment Harbor would get: this process's,
    with the env file's values over it."""
    env = resume_env(env_file) or dict(os.environ)
    where = env_file or "the environment"
    if not any((env.get(name) or "").strip() for name in CREDENTIAL_VARS):
        return f"none of {', '.join(CREDENTIAL_VARS)} is set in {where}"
    try:
        force_oauth = parse_bool_env_value(
            env.get("CLAUDE_FORCE_OAUTH"), name="CLAUDE_FORCE_OAUTH", default=False
        )
    except ValueError as error:
        return str(error)
    if force_oauth and not (env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        return f"CLAUDE_FORCE_OAUTH is set but CLAUDE_CODE_OAUTH_TOKEN is not, in {where}"
    cap = CLAUDE_CODE_MAX_OUTPUT_TOKENS
    if env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") != cap:
        return f"CLAUDE_CODE_MAX_OUTPUT_TOKENS is not {cap} in {where} (ADR-0005)"
    return None


def print_plan(jobs: list[RepairJob], jobs_dir: Path) -> None:
    """The dry run's table: one line per planned repair job."""
    typer.echo(f"{'job':66} {'transcript KiB':>14} {'outputs':>7} {'cut':>4}  exists")
    for job in jobs:
        source = job.source
        size = "-" if source.transcript_bytes is None else f"{source.transcript_bytes / 1024:.1f}"
        total = "-" if source.transcript_outputs_total is None else source.transcript_outputs_total
        cut = "-" if source.transcript_outputs_cut is None else source.transcript_outputs_cut
        exists = "yes" if (jobs_dir / job.name).exists() else "no"
        typer.echo(f"{job.name:66} {size:>14} {total:>7} {cut:>4}  {exists}")
    trials = sum(job.source.attempts for job in jobs)
    typer.echo(f"{len(jobs)} jobs, {trials} trials")


@app.command()
def repair(
    source_jobs: Annotated[
        list[Path], typer.Argument(help="Source job dirs whose failed trials are repaired.")
    ],
    prefix: Annotated[str, typer.Option(help="Repair job names: <prefix>-<trial>-<arm>.")],
    attempts: Annotated[int, typer.Option(min=1, help="Repair trials per failure per arm.")] = 3,
    max_running: Annotated[
        int,
        typer.Option(min=1, help="Trials at once, source jobs' remaining concurrency included."),
    ] = 6,
    jobs_dir: Annotated[Path, typer.Option(help="Where repair jobs are written.")] = Path(
        "corpus/jobs"
    ),
    env_file: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Credentials file passed to Harbor. Default: .env if it exists.",
        ),
    ] = None,
    hold_below_gb: Annotated[
        float, typer.Option(help="Start nothing while the jobs dir's disk has less free.")
    ] = 50.0,
    usage_backoff: Annotated[
        float, typer.Option(help="Seconds to pause launches after a usage-limit error.")
    ] = 1800.0,
    max_resumes: Annotated[
        int, typer.Option(min=0, help="Resumes per repair job for unfinished or infra trials.")
    ] = 3,
    poll: Annotated[float, typer.Option(help="Seconds between passes.")] = 30.0,
    per_task: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Repair this many failures per task, drawn at random once all its attempts "
            "end (ADR-0012 amendment). Default: every failure.",
        ),
    ] = None,
    prune: Annotated[
        bool,
        typer.Option(
            help="Remove a finished repair trial's checkpoint images except its final one; "
            "records stay."
        ),
    ] = True,
    arms: Annotated[
        list[Arm] | None,
        typer.Option(
            help="Arms to run; repeat for several. Other than the round's default, with "
            "--per-task, the prefix's recorded draw is reused and its other jobs are left alone. "
            "Default: fresh, state, state-traj, traj; for Waypoint source jobs, fresh, "
            "state-files, state-live, state-live-traj, traj (ADR-0017)."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Plan and render, print the jobs, and start or write nothing."
        ),
    ] = False,
    storage: StorageOption = None,
    manifests_dir: ManifestsDirOption = MANIFESTS_DIR,
) -> None:
    """Repair every failed trial of the source jobs under four arms (ADR-0012). Restartable."""
    # The source job may have been started a moment ago; Harbor writes its config.json first.
    while problems := check_sources(source_jobs):
        logging.getLogger(__name__).warning("waiting: %s", "; ".join(problems))
        time.sleep(poll)
    try:
        round_arms = default_arms(source_jobs)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    chosen = {arm.value for arm in arms or []}
    repair_arms = tuple(a for a in get_args(RepairArm) if a in chosen) or round_arms
    selection = jobs_dir / INPUTS_DIRNAME / f"{prefix}.selection.json"
    if repair_arms != round_arms:
        if per_task is None and selection.is_file():
            raise typer.BadParameter(
                f"{prefix} was drawn per task ({selection}); pass --per-task to add arms to it"
            )
        if per_task is not None and not selection.is_file():
            raise typer.BadParameter(
                f"--arms other than the default repair the failures already drawn for {prefix}, "
                f"and {selection} does not exist"
            )
    if env_file is None and DEFAULT_ENV_FILE.is_file():
        env_file = DEFAULT_ENV_FILE
    problem = trial_env_problem(env_file)
    if problem and not dry_run:
        raise typer.BadParameter(f"{problem}; trials would not run as round 1's did")
    launcher = Launcher(
        source_jobs=source_jobs,
        prefix=prefix,
        attempts=attempts,
        max_running=max_running,
        jobs_dir=jobs_dir,
        manifests_dir=manifests_dir,
        env_file=env_file,
        hold_below_gb=hold_below_gb,
        usage_backoff_s=usage_backoff,
        max_resumes=max_resumes,
        storage=storage,
        per_task=per_task,
        prune=prune,
        arms=repair_arms,
        round_arms=round_arms,
        dry_run=dry_run,
        watcher_running=watcher_running,
    )
    if dry_run:
        typer.echo(f"env file: {env_file or 'none'} ({problem or 'credentials and cap set'})")
        launcher.discover()
        print_plan(launcher.pending, jobs_dir)
        return
    launcher.run(poll_s=poll)


BACKENDS = {"docker_commit": DockerCommitBackend}


class Gate(StrEnum):
    change = "change"
    audit = "audit"
    none = "none"


def identify_trial(trial_dir: Path) -> TrialIdentity:
    """Name a running trial and its compose project (capture's job, handed to the watcher)."""
    return TrialIdentity(
        trial_name=load_trial_config(trial_dir).trial_name,
        compose_project=compose_project_name(trial_dir),
    )


@app.command()
def watch(
    jobs_dir: Annotated[Path, typer.Argument(help="Harbor jobs dir, e.g. corpus/jobs.")],
    every: Annotated[
        int,
        typer.Option(
            min=1, help="Checkpoint every Nth state-mutating call (ADR-0007); 1 means every call."
        ),
    ],
    gate: Annotated[
        Gate,
        typer.Option(
            help="change: checkpoint only calls that changed the filesystem (ADR-0010); "
            "audit: measure every call but checkpoint all of them; none: every Nth call."
        ),
    ],
    backend: Annotated[str, typer.Option(help="Snapshot backend.")] = "docker_commit",
    sweep_interval: Annotated[
        float, typer.Option(help="Seconds between rescans for missed requests.")
    ] = DEFAULT_SWEEP_INTERVAL_S,
    extras: Annotated[
        bool,
        typer.Option(
            "--extras/--no-extras",
            help="Also record each call's processes and listening ports, and under --gate change "
            "or audit its file diffs, in <trial>/trajlab/ (ADR-0015). Changes what a trial "
            "records: use a new corpus_id.",
        ),
    ] = False,
) -> None:
    """Watch running trials and checkpoint their state-mutating calls. Stop with Ctrl-C."""
    if gate is not Gate.none and every != 1:
        typer.echo(f"trajlab watch: --gate {gate.value} requires --every 1 (ADR-0010)", err=True)
        raise typer.Exit(code=1)
    if backend not in BACKENDS:
        typer.echo(
            f"trajlab watch: unknown backend {backend!r}; one of {sorted(BACKENDS)}", err=True
        )
        raise typer.Exit(code=1)
    if extras and gate is Gate.none:
        typer.echo(
            "trajlab watch: --extras under --gate none records processes only; "
            "file diffs need --gate change or audit",
            err=True,
        )
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        with hold_watcher_lock(jobs_dir):
            Watcher(
                jobs_dir,
                BACKENDS[backend](),
                identify_trial,
                every=every,
                gate=gate.value,
                sweep_interval=sweep_interval,
                extras=extras,
            ).run(stop)
    except WatcherLockedError as error:
        typer.echo(f"trajlab watch: {error}", err=True)
        raise typer.Exit(code=1) from error


def _trial_dirs(path: Path) -> list[Path]:
    """A trial dir as given, or every trial dir of a job dir."""
    if (path / "agent").is_dir():
        return [path]
    return list(iter_trial_dirs(path))


def _trial_dirs_of(command: str, paths: list[Path]) -> tuple[list[Path], int]:
    """The trial dirs `paths` name (trial dirs, or job dirs of them), and how many paths named
    none, each reported on stderr."""
    trial_dirs: list[Path] = []
    bad = 0
    for path in paths:
        if not path.is_dir():
            typer.echo(f"trajlab {command}: {path}: not a directory", err=True)
            bad += 1
            continue
        found = _trial_dirs(path)
        if not found:
            typer.echo(f"trajlab {command}: {path}: no trial dirs", err=True)
            bad += 1
        trial_dirs.extend(found)
    return trial_dirs, bad


@app.command()
def postprocess(
    paths: Annotated[list[Path], typer.Argument(help="Job dirs or trial dirs.")],
) -> None:
    """Join checkpoints and compactions into each trial's agent/trajectory.enriched.json."""
    trial_dirs, failed = _trial_dirs_of("postprocess", paths)
    for trial_dir in trial_dirs:
        if not trajectory_path(trial_dir).is_file():
            typer.echo(f"skipped {trial_dir}: no agent/trajectory.json", err=True)
            continue
        try:
            result = postprocess_trial(trial_dir)
        # Every error postprocess reports is a ValueError: unjoinable records, invalid ATIF,
        # malformed capture files.
        except ValueError as error:
            typer.echo(f"failed {trial_dir}: {error}", err=True)
            failed += 1
            continue
        typer.echo(
            f"wrote {result.path}: {result.steps} steps, "
            f"{result.checkpoints} checkpoints, {result.compactions} compactions"
        )
    if failed:
        raise typer.Exit(code=1)


@store_app.command("build")
def store_build(
    paths: Annotated[
        list[Path], typer.Argument(help="Job dirs or trial dirs; every trial postprocessed.")
    ],
    store_dir: Annotated[
        Path, typer.Option("--store", help="Store dir: record.duckdb, heldout.duckdb, blobs/.")
    ],
    no_images: Annotated[
        bool,
        typer.Option(
            "--no-images", help="Build steps only: no deltas, and the chain is reported unchecked."
        ),
    ] = False,
    start_image_ref: Annotated[
        str | None,
        typer.Option(
            "--start-image",
            help="The start image of a trial whose dir names none (no trajlab-preinstall.json "
            "or trajlab-resume.json), such as one run without pre-install, whose start image "
            "is its task image; a trial's own record always wins (ADR-0014, item 5). Not with "
            "--no-images.",
        ),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            help="Where each trial's `docker image save` and scratch files go, with room for "
            "its start image plus every top layer, compressed; created if missing. Default: "
            "the system temp dir."
        ),
    ] = None,
) -> None:
    """Build finished, postprocessed trials into the store as first attempts, one line each."""
    if no_images and start_image_ref is not None:
        typer.echo(
            "trajlab store build: --start-image names the image checkpoints are read against, "
            "and --no-images reads none; pass one or the other",
            err=True,
        )
        raise typer.Exit(code=1)
    trial_dirs, failed = _trial_dirs_of("store build", paths)
    try:
        if work_dir is not None:
            work_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="trajlab-store-build-", dir=work_dir))
    except OSError as error:
        typer.echo(f"trajlab store build: cannot make scratch space: {error}", err=True)
        raise typer.Exit(code=1) from error
    try:
        for trial_dir in trial_dirs:
            started = time.perf_counter()
            try:
                result = _build_one(
                    trial_dir,
                    store_dir,
                    no_images=no_images,
                    start_image_ref=start_image_ref,
                    scratch=scratch,
                )
            # Every error the store reports is a ValueError (BuildError, StepsError,
            # ReconcileError, StoreError, DockerImagesError); OSError is docker itself or the
            # disk, and duckdb.Error the database (a lock another process holds, a constraint).
            except (ValueError, OSError, duckdb.Error) as error:
                typer.echo(f"failed {trial_dir}: {error}", err=True)
                failed += 1
                continue
            seconds = time.perf_counter() - started
            if result.skipped_existing:
                typer.echo(f"skipped {trial_dir}: already in {store_dir}")
                continue
            chain = result.chain
            state = "complete" if chain.complete else f"incomplete ({'; '.join(chain.reasons)})"
            typer.echo(
                f"built {trial_dir}: {len(result.steps)} steps, {len(result.deltas)} deltas, "
                f"{chain.checkpoints} checkpoints, chain {state}, {seconds:.1f} s"
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if failed:
        raise typer.Exit(code=1)


StoreArgument = Annotated[
    Path, typer.Argument(help="Store dir: record.duckdb, heldout.duckdb, blobs/.")
]


@store_app.command("derive")
def store_derive(store_dir: StoreArgument) -> None:
    """Replace the store's derived views with this code's, upgrading a schema-1 store first
    (ADR-0018); rows are untouched."""
    meta = store_meta()
    try:
        old = replace_views(store_dir, meta)
    except (ValueError, duckdb.Error) as error:
        typer.echo(f"trajlab store derive: {error}", err=True)
        raise typer.Exit(code=1) from error
    if old == meta.derived:
        typer.echo(f"{store_dir}: derived views {meta.derived}, already current")
    elif old == UPGRADED_FROM_1:
        typer.echo(
            f"{store_dir}: upgraded from schema 1 to {meta.schema_version}, "
            f"derived views {meta.derived}"
        )
    else:
        typer.echo(f"{store_dir}: derived views {old} replaced by {meta.derived}")


@store_app.command("export")
def store_export(
    store_dir: StoreArgument,
    out_dir: Annotated[Path, typer.Argument(help="The bundle dir; must not exist.")],
    verify_only: Annotated[
        bool,
        typer.Option("--verify", help="Check an existing bundle against the store; write none."),
    ] = False,
) -> None:
    """Export the store's record as the record-files bundle (ADR-0018), and verify it."""
    try:
        if verify_only:
            problems = verify_bundle(store_dir, out_dir)
            for problem in problems:
                typer.echo(f"trajlab store export: {problem}", err=True)
            if problems:
                raise typer.Exit(code=1)
            typer.echo(f"{out_dir}: matches {store_dir}")
            return
        result = export_bundle(store_dir, out_dir)
    except (ValueError, OSError, duckdb.Error) as error:
        typer.echo(f"trajlab store export: {error}", err=True)
        raise typer.Exit(code=1) from error
    rows = ", ".join(f"{name} {n}" for name, n in result.rows.items())
    typer.echo(f"wrote {out_dir}: {rows}; {result.hunk_files} hunk files; verified")


def _build_one(
    trial_dir: Path,
    store_dir: Path,
    *,
    no_images: bool,
    start_image_ref: str | None,
    scratch: Path,
) -> BuildResult:
    if no_images:
        return build_trial(trial_dir, store_dir, images=None, work_dir=scratch)
    image = start_image(trial_dir) or start_image_ref
    if image is None:
        # build_trial asks for images only when it has checkpoints to diff, so a trial already
        # built with them is still skipped.
        return build_trial(trial_dir, store_dir, images=_UnknownStartImage(), work_dir=scratch)
    with DockerImageProvider(scratch, start_image=image) as images:
        return build_trial(trial_dir, store_dir, images=images, work_dir=scratch)


class _UnknownStartImage:
    """The images of a trial whose start image is not known: they cannot be diffed."""

    def chain(self, checkpoints: Sequence[CheckpointRecord]) -> NoReturn:
        raise BuildError(
            "neither trajlab-preinstall.json nor trajlab-resume.json names its start image; "
            "pass --start-image, or --no-images for steps only"
        )


@app.command()
def manifest(
    job_dirs: Annotated[list[Path], typer.Argument(help="Finished job dirs forming one corpus.")],
    corpus_id: Annotated[
        str | None,
        typer.Option(help="Manifest name. Default: the config's stem, else the one job's name."),
    ] = None,
    config: Annotated[
        Path | None, typer.Option(help="The job config under configs/harbor/ that produced them.")
    ] = None,
    storage: StorageOption = None,
    manifests_dir: ManifestsDirOption = MANIFESTS_DIR,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing manifest.")] = False,
) -> None:
    """Write a corpus manifest for finished job directories."""
    if corpus_id is None:
        if config is not None:
            corpus_id = config.stem
        elif len(job_dirs) == 1:
            corpus_id = job_dirs[0].resolve().name
        else:
            typer.echo("trajlab manifest: pass --corpus-id for more than one job dir", err=True)
            raise typer.Exit(code=1)
    try:
        root = repo_root(Path.cwd())
        config_repo_path = None
        if config is not None:
            config_repo_path = repo_relative(config, root)
            if config_repo_path is None:
                raise ManifestError(f"{config} is not inside the repo at {root}")
        built = build_manifest(
            job_dirs,
            corpus_id=corpus_id,
            repo=repo_state(root),
            config_path=config_repo_path,
            storage=storage,
        )
        path = write_manifest(built, manifests_dir, overwrite=force)
    except ManifestError as error:
        typer.echo(f"trajlab manifest: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"wrote {path}")


@app.command("waypoint-report")
def waypoint_report(
    job_dir: Annotated[Path, typer.Argument(help="A job dir whose trials ran on Waypoint.")],
) -> None:
    """One line per trial: score, and what its Waypoint save holds (ADR-0017)."""
    typer.echo("trial\treward\texception\tsave\tagent\tprograms\tfiles_mb\tmemory_mb\tsave_s\tkey")
    for trial_dir in iter_trial_dirs(job_dir):
        result = load_result(trial_dir)
        record = waypoint_record(trial_dir)
        reward = trial_reward(result) if result else None
        exception = (
            result.exception_info.exception_type if result and result.exception_info else "-"
        )
        save = record.save if record else None
        if record is not None and record.role == "fork":
            stopped = record.stopped_processes
            where = f"copy of {record.opened_from}"
            programs = f"{len(stopped)} stopped" if stopped is not None else "kept running"
            typer.echo(f"{trial_dir.name}\t{reward}\t{exception}\t{where}\t-\t{programs}")
            continue
        if save is None:
            typer.echo(f"{trial_dir.name}\t{reward}\t{exception}\tnone")
            continue
        names = ", ".join(p.command.split(" ")[0].rsplit("/", 1)[-1] for p in save.processes)
        files = round((save.files_bytes or 0) / 1e6, 1)
        memory = round((save.memory_bytes or 0) / 1e6, 1)
        key = "FOUND" if save.secret_found_in else "no"
        typer.echo(
            f"{trial_dir.name}\t{reward}\t{exception}\tfinal\t{save.agent_stopped}\t"
            f"{len(save.processes)} ({names or '-'})\t{files}\t{memory}\t"
            f"{save.capture_ms / 1000:.1f}\t{key}"
        )


@app.command("waypoint-cleanup")
def waypoint_cleanup(
    source_jobs: Annotated[
        list[Path], typer.Argument(help="Waypoint source job dirs whose saves may be removed.")
    ],
    prefix: Annotated[str, typer.Option(help="The repair round's prefix (`trajlab repair`).")],
    jobs_dir: Annotated[Path, typer.Option(help="Where repair jobs are written.")] = Path(
        "corpus/jobs"
    ),
    state_dir: Annotated[Path, typer.Option(help="Waypoint's state dir.")] = Path(
        DEFAULT_STATE_DIR
    ),
    waypoint_bin: Annotated[str, typer.Option(help="The waypoint binary.")] = "waypoint",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would be removed; remove nothing.")
    ] = False,
) -> None:
    """Remove first attempts' Waypoint sessions once no repair can still open their save
    (ADR-0017). Saves still needed are kept and the reason printed."""
    waypoint = Waypoint(state_dir, waypoint_bin)
    for job in source_jobs:
        for trial_dir in iter_trial_dirs(job):
            record = waypoint_record(trial_dir)
            if record is None or record.role != "source":
                continue
            reason = save_keep_reason(trial_dir, jobs_dir, prefix)
            if reason is not None:
                typer.echo(f"keep    {trial_dir.name} ({record.session}): {reason}")
                continue
            if dry_run:
                typer.echo(f"remove  {trial_dir.name} ({record.session}) [dry run]")
                continue
            done = subprocess.run(
                waypoint.argv("cleanup", record.session, "--force"),
                capture_output=True,
                text=True,
            )
            state = "removed" if done.returncode == 0 else f"FAILED: {done.stdout}{done.stderr}"
            typer.echo(f"{state:7} {trial_dir.name} ({record.session})")


@app.command()
def validate(trial_dir: Path) -> None:
    """Validate a trial's agent/trajectory.json, and its enriched trajectory if postprocessed."""
    paths = [trajectory_path(trial_dir)]
    if enriched_trajectory_path(trial_dir).is_file():
        paths.append(enriched_trajectory_path(trial_dir))
    invalid = False
    for path in paths:
        errors = validate_trajectory(path)
        if errors:
            invalid = True
            typer.echo(f"invalid: {path}", err=True)
            for error in errors:
                typer.echo(f"  - {error}", err=True)
        else:
            typer.echo(f"valid: {path}")
    if invalid:
        raise typer.Exit(code=1)


@app.command()
def trace(
    root: Annotated[
        Path, typer.Argument(help="A jobs dir (each job dir is an experiment), or one job dir.")
    ] = Path("corpus/jobs"),
    labels_dir: Annotated[
        Path, typer.Option("--labels", help="Where label files are saved (ignored by git).")
    ] = DEFAULT_LABELS_DIR,
    host: Annotated[
        str, typer.Option(help="Address to listen on. Keep 127.0.0.1 and reach it with ssh -L.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    guide: Annotated[
        Path, typer.Option(help="The labeling guide (Markdown) the site shows beside labels.")
    ] = Path("docs/labeling-guide.md"),
) -> None:
    """Serve a site to read each trial call by call beside its diffs, and to label it (ADR-0015)."""
    if not root.is_dir():
        typer.echo(f"trajlab trace: {root}: not a directory", err=True)
        raise typer.Exit(code=1)
    extra_hosts = frozenset() if host in LOCAL_HOSTS | {"0.0.0.0", "::"} else frozenset({host})
    site = TraceSite(root, LabelStore(labels_dir), extra_hosts=extra_hosts, guide=guide)
    typer.echo(f"trajlab trace: http://{host}:{port}/ (jobs: {root}, labels: {labels_dir})")
    typer.echo("Ctrl+C stops it.")
    try:
        serve(site, host, port)
    except OSError as error:
        typer.echo(f"trajlab trace: cannot listen on {host}:{port}: {error}", err=True)
        raise typer.Exit(code=1) from error
    except KeyboardInterrupt:
        pass


gt_app = typer.Typer(help="Ground truth for error localization by verifier replay (ADR-0013).")
app.add_typer(gt_app, name="gt")

JobDirArgument = Annotated[Path, typer.Argument(help="A finished job dir, e.g. corpus/jobs/x.")]
TrialsOption = Annotated[
    list[str] | None, typer.Option("--trial", help="Only this trial (repeatable).")
]
REPORT_FILENAME = "groundtruth-report.json"
BATCH_LOCK_FILENAME = ".groundtruth-batch.lock"


def _quiet_harbor() -> None:
    logging.getLogger("harbor").setLevel(logging.WARNING)


@gt_app.command("extract")
def gt_extract(
    job_dir: JobDirArgument,
    trial: TrialsOption = None,
    workers: Annotated[int, typer.Option(min=1, help="Trials extracted at once.")] = 4,
    force: Annotated[
        bool, typer.Option("--force", help="Extract again over points.jsonl.")
    ] = False,
) -> None:
    """Extract every timeline point's artifact state of a job's finished trials."""
    _quiet_harbor()
    remove_stale_containers()
    failed = extract_job(finished_trial_dirs(job_dir, trial), workers=workers, force=force)
    if failed:
        typer.echo(f"trajlab gt extract: failed: {', '.join(failed)}", err=True)
        raise typer.Exit(code=1)


def _run_cancellable[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """asyncio.run, with SIGTERM, SIGHUP, and SIGINT cancelling the work so it can clean up."""

    async def main() -> T:
        task = asyncio.ensure_future(coroutine)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            loop.add_signal_handler(signum, task.cancel)
        return await task

    return asyncio.run(main())


@gt_app.command("replay")
def gt_replay(
    job_dir: JobDirArgument,
    timeline: Annotated[
        bool, typer.Option("--timeline", help="Also replay every non-final state.")
    ] = False,
    repeats: Annotated[
        bool, typer.Option("--repeats", help="Add samples around every regression found so far.")
    ] = False,
    trial: TrialsOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only count what is planned.")] = False,
) -> None:
    """Replay what is missing: final states, and with --timeline every state; restartable."""
    _quiet_harbor()
    trial_dirs = finished_trial_dirs(job_dir, trial)
    if dry_run:
        typer.echo(json.dumps(planned_counts(trial_dirs, timeline=timeline, repeats=repeats)))
        return
    lock_path = job_dir / BATCH_LOCK_FILENAME
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.echo(f"trajlab gt replay: another batch holds {lock_path}", err=True)
            raise typer.Exit(code=1) from None
        try:
            _, other = _run_cancellable(replay_job(trial_dirs, timeline=timeline, repeats=repeats))
        except asyncio.CancelledError:
            typer.echo("trajlab gt replay: cancelled; finished replays are kept", err=True)
            raise typer.Exit(code=130) from None
    if other:
        typer.echo(f"trajlab gt replay: {other} replays without a verdict", err=True)


@gt_app.command("report")
def gt_report(job_dir: JobDirArgument, trial: TrialsOption = None) -> None:
    """Write `<job>/groundtruth-report.json`: fidelity gates and flaky checks per trial."""
    report = job_report(finished_trial_dirs(job_dir, trial))
    path = job_dir / REPORT_FILENAME
    path.write_text(json.dumps(report, indent=2) + "\n")
    typer.echo(json.dumps(report["totals"], indent=2))
    typer.echo(f"wrote {path}")


TrialDirArgument = Annotated[Path, typer.Argument(help="A trial dir, e.g. corpus/jobs/x/t__id.")]


class FixModeChoice(StrEnum):
    artifacts = "artifacts"
    environment = "environment"


def _one_trial(trial_dir: Path) -> TrialInputs:
    trial_dir = trial_dir.resolve()
    if not (trial_dir / GROUNDTRUTH_DIRNAME / POINTS_FILENAME).is_file():
        typer.echo(f"trajlab gt: {trial_dir} is not extracted; run trajlab gt extract", err=True)
        raise typer.Exit(code=1)
    return trial_inputs([trial_dir])[0]


@gt_app.command("show")
def gt_show(
    trial_dir: TrialDirArgument,
    list_calls: Annotated[
        bool, typer.Option("--calls", help="List the agent's tool calls instead.")
    ] = False,
) -> None:
    """Print a trial's brief for labeling: task files, timeline, failing checks, fixes so far."""
    _quiet_harbor()
    inputs = _one_trial(trial_dir)
    typer.echo(calls(inputs) if list_calls else brief(inputs), nl=False)


@gt_app.command("try-fix")
def gt_try_fix(
    trial_dir: TrialDirArgument,
    patch: Annotated[Path, typer.Argument(help="Unified diff, paths from the container root.")],
    mode: Annotated[
        FixModeChoice,
        typer.Option(
            help="artifacts: patch the final graded files; environment: patch any file in a "
            "container of the last checkpoint, then --run a command and collect again."
        ),
    ] = FixModeChoice.artifacts,
    run: Annotated[
        str | None, typer.Option("--run", help="Environment mode: command run after patching.")
    ] = None,
    user: Annotated[str, typer.Option(help="Environment mode: user the command runs as.")] = "root",
    workdir: Annotated[
        str | None, typer.Option(help="Environment mode: the command's working directory.")
    ] = None,
    timeout: Annotated[float, typer.Option(help="Environment mode: seconds.")] = 900.0,
) -> None:
    """Apply a fix to a trial's final state, regrade it, and print which checks changed."""
    _quiet_harbor()
    inputs = _one_trial(trial_dir)
    record = try_fix(
        inputs,
        patch,
        mode=mode.value,
        command=run,
        user=user,
        workdir=workdir,
        timeout=timeout,
    )
    shown = record.model_dump(
        mode="json",
        include={
            "fix_id",
            "error",
            "fixed",
            "broken",
            "still_failing",
            "command_exit_code",
            "command_output",
            "replay_id",
        },
    )
    typer.echo(json.dumps(shown, indent=2))


@gt_app.command("blame")
def gt_blame(
    trial_dir: TrialDirArgument,
    path: Annotated[str, typer.Argument(help="Container path of a file, e.g. /app/src/db.cc.")],
) -> None:
    """Print a file's last version with, per line, the timeline point that wrote it."""
    _quiet_harbor()
    inputs = _one_trial(trial_dir)
    history = file_history(inputs, path)
    if not history or history[-1][1] is None:
        typer.echo(f"trajlab gt blame: {path} is absent at the end of the trial", err=True)
        raise typer.Exit(code=1)
    origins = blame([(point.index, lines) for point, lines in history])
    points = {point.index: point for point, _ in history}
    for number, (origin, line) in enumerate(zip(origins, history[-1][1], strict=True), start=1):
        point = points[origin]
        label = f"P{origin}" + (f"/ckpt{point.seq}" if point.seq is not None else f"/{point.kind}")
        typer.echo(f"{number:>5} {label:>12} | {line}")


@gt_app.command("items")
def gt_items(job_dir: JobDirArgument, trial: TrialsOption = None) -> None:
    """Rebuild every trial's groundtruth/items.jsonl from its replays, fixes, and labels."""
    _quiet_harbor()
    kinds: Counter[str] = Counter()
    for inputs in trial_inputs(finished_trial_dirs(job_dir, trial)):
        if not (inputs.trial_dir / GROUNDTRUTH_DIRNAME / POINTS_FILENAME).is_file():
            continue
        build_progress(inputs)
        for item in build_items(inputs):
            kinds[item.kind] += 1
    typer.echo(json.dumps(dict(sorted(kinds.items())), indent=2))


@gt_app.command("reparse")
def gt_reparse(job_dir: JobDirArgument, trial: TrialsOption = None) -> None:
    """Re-read the checks of every recorded replay from its verifier output."""
    changed = sum(reparse_records(d) for d in finished_trial_dirs(job_dir, trial))
    typer.echo(f"{changed} replay records updated")


@gt_app.command("label")
def gt_label(
    trial_dir: TrialDirArgument,
    labels: Annotated[
        Path, typer.Argument(help="A TrialCauses JSON file (contracts.groundtruth).")
    ],
) -> None:
    """Validate the labeler's causes for a trial and store them as groundtruth/labels.json."""
    trial_dir = trial_dir.resolve()
    try:
        parsed = TrialCauses.model_validate_json(labels.read_text())
    except ValueError as error:
        typer.echo(f"trajlab gt label: {labels} is not a valid TrialCauses: {error}", err=True)
        raise typer.Exit(code=1) from error
    if parsed.trial_name != trial_dir.name:
        typer.echo(f"trajlab gt label: trial_name must be {trial_dir.name}", err=True)
        raise typer.Exit(code=1)
    fixes = {f.fix_id for f in read_fixes(trial_dir)}
    unknown = [c.fix_id for c in parsed.causes if c.fix_id and c.fix_id not in fixes]
    if unknown:
        typer.echo(f"trajlab gt label: no such fixes in fixes.jsonl: {unknown}", err=True)
        raise typer.Exit(code=1)
    failing = set(trial_timeline(trial_dir).final_failing)
    claimed = [check for cause in parsed.causes for check in cause.checks]
    not_failing = sorted(set(claimed) - failing)
    if not_failing:
        typer.echo(
            f"trajlab gt label: these checks do not fail at the end: {not_failing}; "
            f"failing checks are {sorted(failing)}",
            err=True,
        )
        raise typer.Exit(code=1)
    twice = sorted(c for c in set(claimed) if claimed.count(c) > 1)
    if twice:
        typer.echo(f"trajlab gt label: checks in more than one cause: {twice}", err=True)
        raise typer.Exit(code=1)
    path = labels_path(trial_dir)
    path.write_text(parsed.model_dump_json(indent=2) + "\n")
    typer.echo(f"wrote {path}")


@gt_app.command("confirm")
def gt_confirm(job_dir: JobDirArgument, trial: TrialsOption = None) -> None:
    """Replay every labeled fix's state until it has its confirming samples."""
    _quiet_harbor()
    count = _run_cancellable(confirm_fixes(finished_trial_dirs(job_dir, trial)))
    typer.echo(f"{count} confirmation replays run")


@gt_app.command("minimize")
def gt_minimize(job_dir: JobDirArgument, trial: TrialsOption = None) -> None:
    """Replay each labeled, confirmed fix without each of its hunks (once per fix)."""
    _quiet_harbor()
    for inputs in trial_inputs(finished_trial_dirs(job_dir, trial)):
        trial_dir = inputs.trial_dir
        if not labels_path(trial_dir).is_file():
            continue
        labels = TrialCauses.model_validate_json(labels_path(trial_dir).read_text())
        fixes = {f.fix_id: f for f in read_fixes(trial_dir)}
        done = read_minimized(trial_dir)
        timeline = trial_timeline(trial_dir)
        base = {t.key: t.final for t in timeline.timelines if t.final and t.final != "flaky"}
        required = QUIET_FIX_SAMPLES if traits(inputs.task_name).quiet else FIX_SAMPLES
        excluded = excluded_checks(trial_dir)
        for cause in labels.causes:
            fix = fixes.get(cause.fix_id or "")
            # The checks the cause's items are built from (gate 2's exclusions left out).
            checks = tuple(sorted(c for c in cause.checks if c not in excluded))
            if fix is None or not checks or (fix.fix_id, checks) in done:
                continue
            final = timeline.points[-1].state_id
            if fix_confirms(fix, checks, final, timeline.records, base, required) is not None:
                continue  # not confirmed (yet): nothing to minimize
            record = minimize_fix(inputs, fix, checks)
            typer.echo(f"{trial_dir.name} {fix.fix_id}: {record.raw_hunks} hunks minimized")


@gt_app.command("revert")
def gt_revert(job_dir: JobDirArgument, trial: TrialsOption = None) -> None:
    """Test every confirmed regression by undoing its point's change on the final state."""
    _quiet_harbor()
    for inputs in trial_inputs(finished_trial_dirs(job_dir, trial)):
        trial_dir = inputs.trial_dir
        if not (trial_dir / GROUNDTRUTH_DIRNAME / POINTS_FILENAME).is_file():
            continue
        timeline = trial_timeline(trial_dir)
        done = read_reverts(trial_dir)
        for index, checks in regressions(trial_dir, timeline).items():
            if (index, checks) in done:
                continue
            record = revert_regression(inputs, timeline.points, index, checks)
            typer.echo(f"{trial_dir.name} P{index}: {record.verdict} {record.reason or ''}")


@gt_app.command("manifest")
def gt_manifest(
    job_dir: JobDirArgument,
    dataset_id: Annotated[str, typer.Option(help="Manifest name, e.g. tb40-sonnet-v2-gt-v1.")],
    source_corpus_id: Annotated[str, typer.Option(help="The corpus the job belongs to.")],
    labeler: Annotated[
        list[str] | None, typer.Option("--labeler", help="Who proposed causes (repeatable).")
    ] = None,
    storage: StorageOption = None,
    manifests_dir: ManifestsDirOption = MANIFESTS_DIR,
) -> None:
    """Write the ground-truth dataset manifest to corpus/manifests/<dataset-id>.json."""
    _quiet_harbor()
    state = repo_state(repo_root(Path.cwd()))
    built = build_gt_manifest(
        dataset_id,
        source_corpus_id,
        job_dir,
        trial_inputs(finished_trial_dirs(job_dir)),
        repo_sha=state.sha,
        repo_dirty=state.dirty,
        labelers=tuple(labeler or ()),
        storage=storage,
    )
    path = manifests_dir / f"{dataset_id}.json"
    path.write_text(built.model_dump_json(indent=2) + "\n")
    typer.echo(f"wrote {path}")


@gt_app.command("summary")
def gt_summary(
    job_dir: JobDirArgument,
    out: Annotated[
        Path | None, typer.Option(help="Write the Markdown here; default stdout.")
    ] = None,
) -> None:
    """Facts about the job's ground truth, as Markdown: replays, gates, items, trials."""
    _quiet_harbor()
    text = summary_markdown(job_dir, trial_inputs(finished_trial_dirs(job_dir)))
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text)
        typer.echo(f"wrote {out}")


@gt_app.command("review-sheet")
def gt_review_sheet(
    job_dir: JobDirArgument,
    out: Annotated[
        Path | None, typer.Option(help="CSV path; default <job>/groundtruth-review.csv.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing sheet.")] = False,
) -> None:
    """Write the human review sheet: items that must be checked plus a stratified sample."""
    _quiet_harbor()
    path = out or job_dir / "groundtruth-review.csv"
    if path.exists() and not force:
        typer.echo(
            f"trajlab gt review-sheet: {path} exists (it may hold reviews); --force", err=True
        )
        raise typer.Exit(code=1)
    count = write_review_sheet(path, trial_inputs(finished_trial_dirs(job_dir)))
    typer.echo(f"wrote {path}: {count} items")


@gt_app.command("refute")
def gt_refute(
    trial_dir: TrialDirArgument,
    item_id: Annotated[str, typer.Option("--item", help="The item id, as items.jsonl has it.")],
    verdict: Annotated[str, typer.Option(help="upheld, refuted, or uncertain.")],
    reasons: Annotated[str, typer.Option(help="Why, with the evidence.")],
    reviewer: Annotated[str, typer.Option(help="Who reviewed, e.g. a model id.")],
    alternative: Annotated[
        list[str] | None,
        typer.Option("--alternative", help="A fix the reviewer tried elsewhere (repeatable)."),
    ] = None,
) -> None:
    """Record an adversarial review of one item; `gt items` turns it into a flag."""
    trial_dir = trial_dir.resolve()
    if item_id not in {i.item_id for i in read_items(trial_dir)}:
        typer.echo(f"trajlab gt refute: no item {item_id} in {trial_dir.name}", err=True)
        raise typer.Exit(code=1)
    fixes = {f.fix_id for f in read_fixes(trial_dir)}
    unknown = [a for a in alternative or [] if a not in fixes]
    if unknown:
        typer.echo(f"trajlab gt refute: no such fixes: {unknown}", err=True)
        raise typer.Exit(code=1)
    record = RefutationRecord(
        trial_name=trial_dir.name,
        item_id=item_id,
        verdict=verdict,  # type: ignore[arg-type]
        reasons=reasons,
        alternative_fix_ids=tuple(alternative or ()),
        reviewer=reviewer,
        created_at=datetime.now(UTC),
    )
    path = trial_dir / GROUNDTRUTH_DIRNAME / REFUTATIONS_FILENAME
    with path.open("a") as handle:
        handle.write(record.model_dump_json() + "\n")
    typer.echo(f"recorded {verdict} for {item_id}")


@gt_app.command("oracle")
def gt_oracle(
    job_dir: JobDirArgument,
    trial: TrialsOption = None,
    timeout: Annotated[float, typer.Option(help="Seconds for each reference solution.")] = 1800.0,
) -> None:
    """Run each task's reference solution on a trial's initial image and regrade it."""
    _quiet_harbor()
    for inputs in trial_inputs(finished_trial_dirs(job_dir, trial)):
        record = run_oracle(inputs, timeout=timeout)
        outcome = record.error or f"reward {record.reward}, failed {list(record.failed_checks)}"
        typer.echo(f"{inputs.trial_dir.name}: {outcome}")
