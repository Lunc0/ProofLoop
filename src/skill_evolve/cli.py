"""Local command-line interface for one evaluation workflow run at a time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from skill_evolve.benchmark import (
    BenchmarkAdapter,
    BenchmarkAdapterError,
    BenchmarkFixtureError,
    BenchmarkRunResult,
    BenchmarkSuite,
    get_adapter,
)
from skill_evolve.benchmark.livesqlbench_adapter import LIVE_SQL_BENCH_DEFAULT_IMAGE
from skill_evolve.candidate import (
    CandidateService,
    CandidateStoreError,
    ExternalSource,
    LocalCandidateStore,
    ResolvedCandidate,
)
from skill_evolve.candidate.store import skill_sha256_for_source
from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.fingerprint import create_snapshot
from skill_evolve.registry import RunNotFoundError, RunStore
from skill_evolve.runner.contracts import RunnerOutput
from skill_evolve.workflow import run_experiment

app = typer.Typer(add_completion=False, no_args_is_help=True)
benchmark_app = typer.Typer(add_completion=False, no_args_is_help=True)
livesqlbench_app = typer.Typer(add_completion=False, no_args_is_help=True)
candidate_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(candidate_app, name="candidate")
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
    candidate_id: Annotated[str | None, typer.Option("--candidate")] = None,
) -> None:
    """Run one benchmark through DSH and retain redacted session evidence."""

    candidate = _resolve_candidate(candidate_id) if candidate_id is not None else None
    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root)
    _run_adapter_dsh(adapter, suite, dsh_manifest, candidate)


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
    adapter: BenchmarkAdapter,
    suite: BenchmarkSuite,
    dsh_manifest: Path,
    candidate: ResolvedCandidate | None = None,
) -> None:
    try:
        dsh_resolved = load_manifest(dsh_manifest)
        result = adapter.run_dsh(
            suite,
            dsh_resolved.manifest.environment.dsh,
            dsh_resolved.profile_patch,
            dsh_resolved.manifest.environment.model,
            _runs_root(),
            candidate,
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


def _candidate_root() -> Path:
    return Path.cwd() / ".local" / "skill-evolve"


def _candidate_service() -> CandidateService:
    return CandidateService(LocalCandidateStore(_candidate_root()))


@candidate_app.command("init")
def initialise_candidate(
    skill_id: str,
    skill_file: Path,
    candidate_name: Annotated[str, typer.Option("--candidate-name")] = "000-baseline",
    source_url: Annotated[str | None, typer.Option("--source-url")] = None,
    source_repository: Annotated[str | None, typer.Option("--source-repository")] = None,
    source_commit: Annotated[str | None, typer.Option("--source-commit")] = None,
    license_id: Annotated[str | None, typer.Option("--license-id")] = None,
    notice_file: Annotated[Path | None, typer.Option("--notice-file")] = None,
) -> None:
    """Create one immutable local baseline Skill candidate."""

    try:
        external_source, resolved_notice = _external_source_from_options(
            skill_file,
            source_url,
            source_repository,
            source_commit,
            license_id,
            notice_file,
        )
        candidate = _candidate_service().initialise(
            skill_id,
            skill_file,
            candidate_name=candidate_name,
            external_source=external_source,
            notice_source=resolved_notice,
        )
    except (CandidateStoreError, ValidationError, ValueError) as error:
        _exit_with_error(str(error), 64)
    _echo_json(_candidate_payload(candidate))


@candidate_app.command("import")
def import_candidate(
    skill_id: str,
    candidate_name: str,
    skill_file: Path,
    parent_candidate_id: Annotated[str, typer.Option("--parent")],
    source_run_id: Annotated[str, typer.Option("--source-run")],
    source_url: Annotated[str | None, typer.Option("--source-url")] = None,
    source_repository: Annotated[str | None, typer.Option("--source-repository")] = None,
    source_commit: Annotated[str | None, typer.Option("--source-commit")] = None,
    license_id: Annotated[str | None, typer.Option("--license-id")] = None,
    notice_file: Annotated[Path | None, typer.Option("--notice-file")] = None,
) -> None:
    """Import a new immutable child candidate from one explicit text file."""

    try:
        RunStore(_runs_root()).open(source_run_id)
        external_source, resolved_notice = _external_source_from_options(
            skill_file,
            source_url,
            source_repository,
            source_commit,
            license_id,
            notice_file,
        )
        candidate = _candidate_service().import_candidate(
            skill_id,
            candidate_name,
            skill_file,
            parent_candidate_id=parent_candidate_id,
            source_run_id=source_run_id,
            external_source=external_source,
            notice_source=resolved_notice,
        )
    except (CandidateStoreError, RunNotFoundError, ValidationError, ValueError) as error:
        _exit_with_error(str(error), 64)
    _echo_json(_candidate_payload(candidate))


@candidate_app.command("list")
def list_candidates(skill_id: str) -> None:
    """List verified public metadata for candidates of one Skill."""

    try:
        candidates = _candidate_service().list_candidates(skill_id)
    except (CandidateStoreError, ValueError) as error:
        _exit_with_error(str(error), 64)
    _echo_json(
        {"skill_id": skill_id, "candidates": [_candidate_payload(item) for item in candidates]}
    )


@candidate_app.command("show")
def show_candidate(candidate_id: str) -> None:
    """Show one verified candidate's metadata and catalog state, never its text."""

    _echo_json(_candidate_payload(_resolve_candidate(candidate_id)))


@candidate_app.command("verify")
def verify_candidate(candidate_id: str) -> None:
    """Recompute a candidate hash and validate its complete lineage."""

    candidate = _resolve_candidate(candidate_id)
    _echo_json(
        {
            "candidate_id": candidate.candidate_id,
            "skill_sha256": candidate.skill_sha256,
            "verified": True,
        }
    )


@candidate_app.command("select")
def select_candidate(candidate_id: str) -> None:
    """Set the local default for one Skill without modifying candidate files."""

    try:
        candidate = _candidate_service().select(candidate_id)
    except (CandidateStoreError, ValueError) as error:
        _exit_with_error(str(error), 64)
    _echo_json(_candidate_payload(candidate))


def _resolve_candidate(candidate_id: str) -> ResolvedCandidate:
    try:
        return _candidate_service().verify(candidate_id)
    except (CandidateStoreError, ValueError) as error:
        _exit_with_error(str(error), 64)


def _candidate_payload(candidate: ResolvedCandidate) -> dict[str, object]:
    return {
        "metadata": candidate.metadata.model_dump(mode="json"),
        "catalog": candidate.catalog_entry.model_dump(mode="json"),
        "binding": candidate.public_binding(),
    }


def _external_source_from_options(
    skill_file: Path,
    source_url: str | None,
    source_repository: str | None,
    source_commit: str | None,
    license_id: str | None,
    notice_file: Path | None,
) -> tuple[ExternalSource | None, Path | None]:
    values = (source_url, source_repository, source_commit, license_id, notice_file)
    if not any(value is not None for value in values):
        return None, None
    if any(value is None for value in values):
        raise CandidateStoreError(
            "external source requires --source-url, --source-repository, --source-commit, "
            "--license-id, and --notice-file"
        )
    if (
        source_url is None
        or source_repository is None
        or source_commit is None
        or license_id is None
        or notice_file is None
    ):
        raise CandidateStoreError("external source options are incomplete")
    return (
        ExternalSource(
            canonical_url=source_url,
            source_repository=source_repository,
            commit=source_commit,
            file_sha256=skill_sha256_for_source(skill_file),
            license_id=license_id,
        ),
        notice_file,
    )


def _exit_with_error(message: str, code: int) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def main() -> None:
    app()
