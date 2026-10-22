"""trajlab command line entry point."""

import logging
import os
import shutil
import signal
import tempfile
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, get_args

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
    resume_env,
)
from trajlab.capture.trace_server import LOCAL_HOSTS, TraceSite, serve
from trajlab.checkpoint.backends.docker_commit import DockerCommitBackend
from trajlab.checkpoint.watcher import (
    DEFAULT_SWEEP_INTERVAL_S,
    TrialIdentity,
    Watcher,
    WatcherLockedError,
    hold_watcher_lock,
    watcher_running,
)
from trajlab.contracts import REPAIR_ARMS, RepairArm
from trajlab.store.build import BuildError, BuildResult, build_trial, start_image
from trajlab.store.docker_images import DockerImageProvider

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
            help="Arms to run; repeat for several. Other than the default four, with --per-task, "
            "the prefix's recorded draw is reused and its other jobs are left alone. Default: "
            "fresh, state, state-traj, traj."
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
    chosen = {arm.value for arm in arms or []}
    repair_arms = tuple(a for a in get_args(RepairArm) if a in chosen) or REPAIR_ARMS
    selection = jobs_dir / INPUTS_DIRNAME / f"{prefix}.selection.json"
    if repair_arms != REPAIR_ARMS:
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
    # The source job may have been started a moment ago; Harbor writes its config.json first.
    while problems := check_sources(source_jobs):
        logging.getLogger(__name__).warning("waiting: %s", "; ".join(problems))
        time.sleep(poll)
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


@app.command()
def postprocess(
    paths: Annotated[list[Path], typer.Argument(help="Job dirs or trial dirs.")],
) -> None:
    """Join checkpoints and compactions into each trial's agent/trajectory.enriched.json."""
    failed = 0
    for path in paths:
        if not path.is_dir():
            typer.echo(f"trajlab postprocess: {path}: not a directory", err=True)
            failed += 1
            continue
        trial_dirs = _trial_dirs(path)
        if not trial_dirs:
            typer.echo(f"trajlab postprocess: {path}: no trial dirs", err=True)
            failed += 1
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
            help="The image every checkpoint of these trials extends by one layer. Default: "
            "each trial's trajlab-preinstall.json or trajlab-resume.json (ADR-0014, item 5).",
        ),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            help="Where each trial's `docker image save` and scratch files go, with room for "
            "its start image plus every top layer, compressed. Default: the system temp dir."
        ),
    ] = None,
) -> None:
    """Build finished, postprocessed trials into the store as first attempts, one line each."""
    failed = 0
    scratch = Path(tempfile.mkdtemp(prefix="trajlab-store-build-", dir=work_dir))
    try:
        for path in paths:
            if not path.is_dir():
                typer.echo(f"trajlab store build: {path}: not a directory", err=True)
                failed += 1
                continue
            trial_dirs = _trial_dirs(path)
            if not trial_dirs:
                typer.echo(f"trajlab store build: {path}: no trial dirs", err=True)
                failed += 1
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
                # disk.
                except (ValueError, OSError) as error:
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
    image = start_image_ref or start_image(trial_dir)
    if image is None:
        raise BuildError(
            f"{trial_dir}: neither trajlab-preinstall.json nor trajlab-resume.json names its "
            "start image; pass --start-image, or --no-images for steps only"
        )
    with DockerImageProvider(scratch, start_image=image) as images:
        return build_trial(trial_dir, store_dir, images=images, work_dir=scratch)


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
