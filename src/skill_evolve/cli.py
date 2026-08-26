"""Local command-line interface for one evaluation workflow run at a time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from skill_evolve.benchmark import (
    BenchmarkAdapter,
    BenchmarkAdapterError,
    BenchmarkFixtureError,
    BenchmarkRunResult,
    BenchmarkSuite,
    get_adapter,
)
from skill_evolve.benchmark.livesqlbench_adapter import LIVE_SQL_BENCH_DEFAULT_IMAGE
from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.fingerprint import create_snapshot
from skill_evolve.registry import RunNotFoundError, RunStore
from skill_evolve.runner.contracts import RunnerOutput
from skill_evolve.workflow import run_experiment

app = typer.Typer(add_completion=False, no_args_is_help=True)
benchmark_app = typer.Typer(add_completion=False, no_args_is_help=True)
livesqlbench_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(livesqlbench_app, name="livesqlbench")

_LIVE_SQL_BENCH_ADAPTER_ID = "livesqlbench-base-lite"


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


@benchmark_app.command("validate")
def validate_benchmark(
    adapter_id: str,
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
) -> None:
    """Validate one explicitly registered local benchmark without printing Gold data."""

    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root)
    _echo_json(adapter.public_summary(suite))


@benchmark_app.command("fixture-check")
def check_benchmark_fixture(
    adapter_id: str,
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
    image: Annotated[str, typer.Option("--image")] = LIVE_SQL_BENCH_DEFAULT_IMAGE,
) -> None:
    """Restore an adapter's local PostgreSQL fixtures and verify read-only access."""

    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root)
    try:
        result = adapter.fixture_check(suite, image)
    except BenchmarkFixtureError as error:
        _exit_with_error(str(error), 2)
    _echo_json(result)


@benchmark_app.command("evaluate")
def evaluate_benchmark(
    adapter_id: str,
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
    runner_output: Annotated[Path, typer.Argument(dir_okay=False)],
    image: Annotated[str, typer.Option("--image")] = LIVE_SQL_BENCH_DEFAULT_IMAGE,
) -> None:
    """Score runner output locally without printing Gold SQL or query results."""

    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root)
    output = _load_runner_output(runner_output)
    try:
        result = adapter.evaluate(suite, output, image)
    except BenchmarkFixtureError as error:
        _exit_with_error(str(error), 2)
    except BenchmarkAdapterError as error:
        _exit_with_error(str(error), 64)
    _echo_json(result)


@benchmark_app.command("run")
def run_benchmark(
    adapter_id: str,
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
    dsh_manifest: Annotated[Path, typer.Argument(dir_okay=False)],
) -> None:
    """Run one benchmark through DSH and retain redacted session evidence."""

    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root)
    _run_adapter_dsh(adapter, suite, dsh_manifest)


@livesqlbench_app.command("validate")
def validate_livesqlbench(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
) -> None:
    """Compatibility entry point for Base-Lite validation."""

    adapter = _load_adapter(_LIVE_SQL_BENCH_ADAPTER_ID)
    suite = _load_benchmark_suite(adapter, data_root)
    _echo_json(adapter.public_summary(suite))


@livesqlbench_app.command("fixture-check")
def check_livesqlbench_fixture(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    image: Annotated[str, typer.Option("--image")] = LIVE_SQL_BENCH_DEFAULT_IMAGE,
) -> None:
    """Compatibility entry point for Base-Lite fixture verification."""

    adapter = _load_adapter(_LIVE_SQL_BENCH_ADAPTER_ID)
    suite = _load_benchmark_suite(adapter, data_root)
    try:
        result = adapter.fixture_check(suite, image)
    except BenchmarkFixtureError as error:
        _exit_with_error(str(error), 2)
    _echo_json(result)


@livesqlbench_app.command("evaluate")
def evaluate_livesqlbench(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    runner_output: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    image: Annotated[str, typer.Option("--image")] = LIVE_SQL_BENCH_DEFAULT_IMAGE,
) -> None:
    """Compatibility entry point for local Base-Lite Soft-EX scoring."""

    adapter = _load_adapter(_LIVE_SQL_BENCH_ADAPTER_ID)
    suite = _load_benchmark_suite(adapter, data_root)
    output = _load_runner_output(runner_output)
    try:
        result = adapter.evaluate(suite, output, image)
    except BenchmarkFixtureError as error:
        _exit_with_error(str(error), 2)
    except BenchmarkAdapterError as error:
        _exit_with_error(str(error), 64)
    _echo_json(result)


@livesqlbench_app.command("run")
def run_livesqlbench(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    dsh_manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Compatibility entry point for Base-Lite DSH execution."""

    adapter = _load_adapter(_LIVE_SQL_BENCH_ADAPTER_ID)
    suite = _load_benchmark_suite(adapter, data_root)
    _run_adapter_dsh(adapter, suite, dsh_manifest)


def _load_adapter(adapter_id: str) -> BenchmarkAdapter:
    try:
        return get_adapter(adapter_id)
    except BenchmarkAdapterError as error:
        _exit_with_error(str(error), 64)


def _load_benchmark_suite(adapter: BenchmarkAdapter, data_root: Path) -> BenchmarkSuite:
    try:
        return adapter.load_suite(data_root)
    except BenchmarkAdapterError as error:
        _exit_with_error(str(error), 64)


def _load_runner_output(path: Path) -> RunnerOutput:
    try:
        return RunnerOutput.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        _exit_with_error(f"invalid runner output: {error}", 64)


def _run_adapter_dsh(
    adapter: BenchmarkAdapter, suite: BenchmarkSuite, dsh_manifest: Path
) -> None:
    try:
        dsh_resolved = load_manifest(dsh_manifest)
        result = adapter.run_dsh(
            suite,
            dsh_resolved.manifest.environment.dsh,
            dsh_resolved.profile_patch,
            dsh_resolved.manifest.environment.model,
            _runs_root(),
        )
    except (BenchmarkAdapterError, ManifestError) as error:
        _exit_with_error(str(error), 64)
    _echo_run_result(result)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _echo_run_result(result: BenchmarkRunResult) -> None:
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status,
                "attribution": result.attribution,
                "report": str(result.report_path),
                "redacted_session_logs": str(result.redacted_session_logs_path),
            },
            ensure_ascii=False,
        )
    )


def _runs_root() -> Path:
    return Path.cwd() / "runs"


def _exit_with_error(message: str, code: int) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def main() -> None:
    app()
