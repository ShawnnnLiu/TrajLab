"""trajlab command line entry point.

Each subcommand is a stub until its build-order step lands (see CLAUDE.md).
"""

from pathlib import Path

import typer

app = typer.Typer(help="Capture Claude Code trajectories on Harbor with environment checkpoints.")


@app.command()
def run(config: Path) -> None:
    """Run a Harbor job from a config file and record a corpus manifest."""
    raise typer.Exit(code=_not_implemented("run"))


@app.command()
def watch(jobs_dir: Path, backend: str = "docker_commit") -> None:
    """Watch running trials and take a checkpoint at every tool call."""
    raise typer.Exit(code=_not_implemented("watch"))


@app.command()
def postprocess(job_dir: Path) -> None:
    """Join checkpoints into the ATIF trajectory and write trajectory.enriched.json."""
    raise typer.Exit(code=_not_implemented("postprocess"))


@app.command()
def manifest(job_dir: Path) -> None:
    """Write a corpus manifest for a finished job directory."""
    raise typer.Exit(code=_not_implemented("manifest"))


@app.command()
def validate(trial_dir: Path) -> None:
    """Validate a trial's ATIF trajectory."""
    raise typer.Exit(code=_not_implemented("validate"))


def _not_implemented(name: str) -> int:
    typer.echo(f"trajlab {name}: not implemented yet", err=True)
    return 2
