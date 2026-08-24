"""Local command-line interface for one evaluation workflow run at a time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.fingerprint import create_snapshot
from skill_evolve.registry import RunNotFoundError, RunStore
from skill_evolve.workflow import run_experiment

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def validate(manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Validate a manifest and print its deterministic environment fingerprint."""

    try:
        snapshot = create_snapshot(load_manifest(manifest))
    except ManifestError as error:
        _exit_with_error(str(error), 64)
    typer.echo(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False))


@app.command()
def run(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    suite: Annotated[str | None, typer.Option("--suite")] = None,
) -> None:
    """Run an experiment and write an auditable evidence bundle under runs/."""

    try:
        outcome = run_experiment(manifest, suite, _runs_root())
    except ManifestError as error:
        _exit_with_error(str(error), 64)
    typer.echo(
        json.dumps(
            {
                "run_id": outcome.run.run_id,
                "status": outcome.attribution.status.value,
                "attribution": outcome.attribution.kind.value,
                "report": str(outcome.run.path / "report.md"),
            },
            ensure_ascii=False,
        )
    )
    if outcome.exit_code:
        raise typer.Exit(outcome.exit_code)


@app.command()
def show(run_id: str) -> None:
    """Print the machine-readable result for one local run."""

    try:
        run_directory = RunStore(_runs_root()).open(run_id)
        result = json.loads((run_directory.path / "result.json").read_text(encoding="utf-8"))
    except (RunNotFoundError, FileNotFoundError, json.JSONDecodeError) as error:
        _exit_with_error(str(error), 64)
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


@app.command()
def rerun(
    run_id: str,
    suite: Annotated[str | None, typer.Option("--suite")] = None,
) -> None:
    """Start a new run from an existing run's copied, resolved input bundle."""

    try:
        prior = RunStore(_runs_root()).open(run_id)
        outcome = run_experiment(prior.path / "manifest.resolved.yaml", suite, _runs_root())
    except (RunNotFoundError, ManifestError) as error:
        _exit_with_error(str(error), 64)
    typer.echo(
        json.dumps(
            {"run_id": outcome.run.run_id, "status": outcome.attribution.status.value},
            ensure_ascii=False,
        )
    )
    if outcome.exit_code:
        raise typer.Exit(outcome.exit_code)


def _runs_root() -> Path:
    return Path.cwd() / "runs"


def _exit_with_error(message: str, code: int) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def main() -> None:
    app()
