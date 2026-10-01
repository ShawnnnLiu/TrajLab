"""trajlab command line entry point.

Each subcommand is a stub until its build-order step lands (see CLAUDE.md).
"""

import logging
import signal
import threading
from pathlib import Path
from typing import Annotated

import typer

from trajlab.atif.load import trajectory_path
from trajlab.atif.validate import validate_trajectory
from trajlab.capture.corpus import (
    ManifestError,
    build_manifest,
    repo_root,
    repo_state,
    write_manifest,
)
from trajlab.capture.discover import compose_project_name, load_trial_config
from trajlab.capture.harbor_runner import RunRefusedError, execute, plan_run
from trajlab.checkpoint.backends.docker_commit import DockerCommitBackend
from trajlab.checkpoint.watcher import (
    DEFAULT_SWEEP_INTERVAL_S,
    TrialIdentity,
    Watcher,
    WatcherLockedError,
    hold_watcher_lock,
    watcher_running,
)

app = typer.Typer(help="Capture Claude Code trajectories on Harbor with environment checkpoints.")

MANIFESTS_DIR = Path("corpus/manifests")
DEFAULT_ENV_FILE = Path(".env")

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


BACKENDS = {"docker_commit": DockerCommitBackend}


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
    backend: Annotated[str, typer.Option(help="Snapshot backend.")] = "docker_commit",
    sweep_interval: Annotated[
        float, typer.Option(help="Seconds between rescans for missed requests.")
    ] = DEFAULT_SWEEP_INTERVAL_S,
) -> None:
    """Watch running trials and checkpoint every Nth state-mutating call. Stop with Ctrl-C."""
    if backend not in BACKENDS:
        typer.echo(
            f"trajlab watch: unknown backend {backend!r}; one of {sorted(BACKENDS)}", err=True
        )
        raise typer.Exit(code=1)
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
                sweep_interval=sweep_interval,
            ).run(stop)
    except WatcherLockedError as error:
        typer.echo(f"trajlab watch: {error}", err=True)
        raise typer.Exit(code=1) from error


@app.command()
def postprocess(job_dir: Path) -> None:
    """Join checkpoints into the ATIF trajectory and write trajectory.enriched.json."""
    raise typer.Exit(code=_not_implemented("postprocess"))


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
            if not config.resolve().is_relative_to(root):
                raise ManifestError(f"{config} is not inside the repo at {root}")
            config_repo_path = config.resolve().relative_to(root).as_posix()
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
    """Validate a trial's ATIF trajectory (agent/trajectory.json) against Harbor's schema."""
    path = trajectory_path(trial_dir)
    errors = validate_trajectory(path)
    if errors:
        typer.echo(f"invalid: {path}", err=True)
        for error in errors:
            typer.echo(f"  - {error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"valid: {path}")


def _not_implemented(name: str) -> int:
    typer.echo(f"trajlab {name}: not implemented yet", err=True)
    return 2
