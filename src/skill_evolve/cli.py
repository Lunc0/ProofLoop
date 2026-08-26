"""Local command-line interface for one evaluation workflow run at a time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from skill_evolve.benchmark import (
    LiveSQLBenchDataError,
    LiveSQLBenchRunError,
    LiveSQLBenchSuite,
    evaluate_query_case,
    load_base_lite_suite,
    public_evaluation_dict,
    public_summary,
    run_base_lite_dsh,
)
from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.config.models import EvaluatorConfig
from skill_evolve.fingerprint import create_snapshot
from skill_evolve.registry import RunNotFoundError, RunStore
from skill_evolve.runner.contracts import RunnerOutput
from skill_evolve.sql import LiveSQLBenchFixture, LiveSQLBenchFixtureError
from skill_evolve.workflow import run_experiment

app = typer.Typer(add_completion=False, no_args_is_help=True)
livesqlbench_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(livesqlbench_app, name="livesqlbench")

_LIVE_SQL_BENCH_EVALUATOR_VERSION = "livesqlbench-base-lite-soft-ex-v1"
_LIVE_SQL_BENCH_DEFAULT_IMAGE = "postgres:14.12"


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


@livesqlbench_app.command("validate")
def validate_livesqlbench(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
) -> None:
    """Validate local Base-Lite public, GT and official dump inputs without printing gold."""

    suite = _load_livesqlbench_suite(data_root)
    typer.echo(json.dumps(suite.public_summary(), indent=2, ensure_ascii=False))


@livesqlbench_app.command("fixture-check")
def check_livesqlbench_fixture(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    image: Annotated[str, typer.Option("--image")] = _LIVE_SQL_BENCH_DEFAULT_IMAGE,
) -> None:
    """Restore the five selected databases into a disposable local PostgreSQL fixture."""

    suite = _load_livesqlbench_suite(data_root)
    evaluator = _livesqlbench_evaluator()
    try:
        with LiveSQLBenchFixture(
            suite.dump_root,
            (case.database_name for case in suite.cases),
            evaluator,
            image,
        ) as fixture:
            checks = {
                case.instance_id: fixture.for_database(case.database_name)
                .execute_read_only("SELECT current_schema()")
                .ok
                for case in suite.cases
            }
    except LiveSQLBenchFixtureError as error:
        _exit_with_error(str(error), 2)
    typer.echo(
        json.dumps(
            {
                "benchmark": "livesqlbench-base-lite",
                "fixture": "ready",
                "checks": checks,
                "image": image,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@livesqlbench_app.command("evaluate")
def evaluate_livesqlbench(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    runner_output: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    image: Annotated[str, typer.Option("--image")] = _LIVE_SQL_BENCH_DEFAULT_IMAGE,
) -> None:
    """Score precomputed runner output using local Soft-EX without printing gold SQL."""

    suite = _load_livesqlbench_suite(data_root)
    output = _load_livesqlbench_runner_output(runner_output, suite.case_ids)
    output_by_case = {case.case_id: case for case in output.cases}
    evaluator = _livesqlbench_evaluator()
    try:
        with LiveSQLBenchFixture(
            suite.dump_root,
            (case.database_name for case in suite.cases),
            evaluator,
            image,
        ) as fixture:
            evaluations = tuple(
                evaluate_query_case(
                    case,
                    output_by_case[case.instance_id],
                    fixture.for_database(case.database_name),
                )
                for case in suite.cases
            )
    except LiveSQLBenchFixtureError as error:
        _exit_with_error(str(error), 2)
    typer.echo(
        json.dumps(
            {
                "benchmark": "livesqlbench-base-lite",
                "evaluator": evaluator.version,
                "cases": [public_evaluation_dict(evaluation) for evaluation in evaluations],
                "summary": public_summary(evaluations),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@livesqlbench_app.command("run")
def run_livesqlbench(
    data_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    dsh_manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Run five Query tasks through DSH and retain redacted session-log evidence."""

    try:
        dsh_resolved = load_manifest(dsh_manifest)
        outcome = run_base_lite_dsh(
            data_root,
            dsh_resolved.manifest.environment.dsh,
            dsh_resolved.profile_patch,
            dsh_resolved.manifest.environment.model,
            _runs_root(),
        )
    except (LiveSQLBenchDataError, LiveSQLBenchRunError, ManifestError) as error:
        _exit_with_error(str(error), 64)
    typer.echo(
        json.dumps(
            {
                "run_id": outcome.run.run_id,
                "status": outcome.attribution.status.value,
                "attribution": outcome.attribution.kind.value,
                "report": str(outcome.run.path / "report.md"),
                "redacted_session_logs": str(outcome.run.path / "raw" / "dsh"),
            },
            ensure_ascii=False,
        )
    )
    if outcome.exit_code:
        raise typer.Exit(outcome.exit_code)


def _load_livesqlbench_suite(data_root: Path) -> LiveSQLBenchSuite:
    try:
        return load_base_lite_suite(data_root)
    except LiveSQLBenchDataError as error:
        _exit_with_error(str(error), 64)


def _load_livesqlbench_runner_output(
    path: Path, expected_case_ids: tuple[str, ...]
) -> RunnerOutput:
    try:
        output = RunnerOutput.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        _exit_with_error(f"invalid runner output: {error}", 64)
    actual_case_ids = tuple(case.case_id for case in output.cases)
    if len(actual_case_ids) != len(set(actual_case_ids)):
        _exit_with_error("runner output contains duplicate case_id values", 64)
    if set(actual_case_ids) != set(expected_case_ids):
        _exit_with_error(
            "runner output case IDs must exactly match the five configured LiveSQLBench cases",
            64,
        )
    return output


def _livesqlbench_evaluator() -> EvaluatorConfig:
    return EvaluatorConfig(version=_LIVE_SQL_BENCH_EVALUATOR_VERSION)


def _runs_root() -> Path:
    return Path.cwd() / "runs"


def _exit_with_error(message: str, code: int) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def main() -> None:
    app()
