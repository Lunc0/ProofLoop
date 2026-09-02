"""Local command-line interface for one evaluation workflow run at a time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn, cast

import typer
from pydantic import ValidationError

from skill_evolve.benchmark import (
    LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
    BenchmarkAdapter,
    BenchmarkAdapterError,
    BenchmarkFixtureError,
    BenchmarkRunResult,
    BenchmarkSuite,
    get_adapter,
)
from skill_evolve.benchmark.livesqlbench_adapter import LIVE_SQL_BENCH_DEFAULT_IMAGE
from skill_evolve.benchmark.livesqlbench_heldout import (
    HeldOutError,
    initialise_heldout_manifest,
)
from skill_evolve.benchmark.livesqlbench_rotation import (
    RotationError,
    RotationKind,
    create_rotation_manifest,
    initialise_rotation_reserve,
)
from skill_evolve.candidate import (
    CandidateService,
    CandidateStoreError,
    ExternalSource,
    LocalCandidateStore,
    ResolvedCandidate,
    subject_from_candidate,
)
from skill_evolve.candidate.store import skill_sha256_for_source
from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.config.models import OptimizerConfig
from skill_evolve.fingerprint import create_snapshot
from skill_evolve.optimizer.edit_plan_service import plan_after_evolution
from skill_evolve.optimizer.service import advise_after_evolution
from skill_evolve.registry import RunDirectory, RunNotFoundError, RunStore
from skill_evolve.runner.contracts import RunnerOutput
from skill_evolve.subject import RunSubject
from skill_evolve.workflow import run_experiment

app = typer.Typer(add_completion=False, no_args_is_help=True)
benchmark_app = typer.Typer(add_completion=False, no_args_is_help=True)
livesqlbench_app = typer.Typer(add_completion=False, no_args_is_help=True)
candidate_app = typer.Typer(add_completion=False, no_args_is_help=True)
optimizer_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(candidate_app, name="candidate")
app.add_typer(optimizer_app, name="optimizer")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(livesqlbench_app, name="livesqlbench")

_LIVE_SQL_BENCH_ADAPTER_ID = "livesqlbench-base-lite"
_EVOLUTION_RUN_STATUSES = frozenset(
    {"passed", "task_failure", "runner_invalid", "evaluator_invalid"}
)


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


@optimizer_app.command("advise")
def advise_optimizer(run_id: str) -> None:
    """Generate one LLM advice artifact for an existing Evolution run only."""

    try:
        run = RunStore(_runs_root()).open(run_id)
        subject, run_status, result = _load_evolution_optimizer_context(run, "advice")
        optimizer_result = advise_after_evolution(
            run,
            subject,
            OptimizerConfig(enabled=True),
            run_status=run_status,
        )
    except (RunNotFoundError, OSError, ValueError, json.JSONDecodeError) as error:
        _exit_with_error(str(error), 64)
    receipt = optimizer_result.receipt
    result["optimizer"] = {
        "status": receipt.status,
        "advice_path": optimizer_result.advice_path,
        "provider": receipt.provider,
        "model": receipt.model,
        "prompt_version": receipt.prompt_version,
    }
    run.write_json("result.json", result)
    _echo_json(
        {
            "run_id": run.run_id,
            "optimizer": result["optimizer"],
        }
    )


@optimizer_app.command("plan")
def plan_optimizer(run_id: str) -> None:
    """Generate one semantic edit plan for an existing Evolution run only."""

    try:
        run = RunStore(_runs_root()).open(run_id)
        subject, run_status, result = _load_evolution_optimizer_context(run, "plan")
        plan_result = plan_after_evolution(
            run,
            subject,
            OptimizerConfig(enabled=True),
            run_status=run_status,
            adapter_id=LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
        )
    except (RunNotFoundError, OSError, ValueError, json.JSONDecodeError) as error:
        _exit_with_error(str(error), 64)
    receipt = plan_result.receipt
    result["optimizer_edit_plan"] = {
        "status": receipt.status,
        "plan_path": plan_result.plan_path,
        "skill_map_path": plan_result.skill_map_path,
        "provider": receipt.provider,
        "model": receipt.model,
        "prompt_version": receipt.prompt_version,
        "plan_sha256": receipt.plan_sha256,
        "skill_map_sha256": receipt.skill_map_sha256,
        "cluster_count": receipt.cluster_count,
        "edit_count": receipt.edit_count,
    }
    run.write_json("result.json", result)
    _echo_json(
        {
            "run_id": run.run_id,
            "optimizer_edit_plan": result["optimizer_edit_plan"],
        }
    )


@benchmark_app.command("validate")
def validate_benchmark(
    adapter_id: str,
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
    manifest_id: Annotated[str | None, typer.Option("--manifest")] = None,
) -> None:
    """Validate one explicitly registered local benchmark without printing Gold data."""

    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root, manifest_id)
    _echo_json(adapter.public_summary(suite))


@benchmark_app.command("fixture-check")
def check_benchmark_fixture(
    adapter_id: str,
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
    image: Annotated[str, typer.Option("--image")] = LIVE_SQL_BENCH_DEFAULT_IMAGE,
    manifest_id: Annotated[str | None, typer.Option("--manifest")] = None,
) -> None:
    """Restore an adapter's local PostgreSQL fixtures and verify read-only access."""

    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root, manifest_id)
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
    manifest_id: Annotated[str | None, typer.Option("--manifest")] = None,
) -> None:
    """Score runner output locally without printing Gold SQL or query results."""

    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root, manifest_id)
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
    manifest_id: Annotated[str | None, typer.Option("--manifest")] = None,
) -> None:
    """Run one benchmark through DSH and retain redacted session evidence."""

    candidate = _resolve_candidate(candidate_id) if candidate_id is not None else None
    adapter = _load_adapter(adapter_id)
    suite = _load_benchmark_suite(adapter, data_root, manifest_id)
    _run_adapter_dsh(
        adapter,
        suite,
        dsh_manifest,
        subject_from_candidate(candidate) if candidate is not None else None,
    )


@benchmark_app.command("initialize-heldout")
def initialise_heldout_benchmark(
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
) -> None:
    """Create one evaluator-owned final-suite manifest from public metadata only."""

    try:
        receipt = initialise_heldout_manifest(
            data_root,
            repository_root=Path.cwd(),
            candidate_store_root=_candidate_root(),
        )
    except HeldOutError as error:
        _exit_with_error(str(error), 64)
    _echo_json(receipt.public_dict())


@benchmark_app.command("initialize-rotation")
def initialise_rotation_benchmark(
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
) -> None:
    """Create the custody-only five-per-tier rolling reserve once."""

    try:
        receipt = initialise_rotation_reserve(
            data_root,
            repository_root=Path.cwd(),
            candidate_store_root=_candidate_root(),
        )
    except RotationError as error:
        _exit_with_error(str(error), 64)
    _echo_json(receipt.public_dict())


@benchmark_app.command("create-rotation-manifest")
def create_benchmark_rotation_manifest(
    data_root: Annotated[Path, typer.Argument(file_okay=False)],
    kind: Annotated[str, typer.Option("--kind")],
) -> None:
    """Create one unlisted custody manifest for Smoke or Evolution."""

    if kind not in {"smoke", "evolution"}:
        _exit_with_error("rotation kind must be smoke or evolution", 64)
    try:
        receipt = create_rotation_manifest(
            data_root,
            cast(RotationKind, kind),
            repository_root=Path.cwd(),
            candidate_store_root=_candidate_root(),
        )
    except RotationError as error:
        _exit_with_error(str(error), 64)
    _echo_json(receipt.public_dict())


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


def _load_benchmark_suite(
    adapter: BenchmarkAdapter, data_root: Path, manifest_id: str | None = None
) -> BenchmarkSuite:
    try:
        return adapter.load_suite(data_root, manifest_id)
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
    subject: RunSubject | None = None,
) -> None:
    try:
        dsh_resolved = load_manifest(dsh_manifest)
        result = adapter.run_dsh(
            suite,
            dsh_resolved.manifest.environment.dsh,
            dsh_resolved.profile_patch,
            dsh_resolved.manifest.environment.model,
            _runs_root(),
            subject,
            dsh_resolved.manifest.environment.optimizer,
        )
    except (BenchmarkAdapterError, ManifestError) as error:
        _exit_with_error(str(error), 64)
    _echo_run_result(result)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


def _load_evolution_optimizer_context(
    run: RunDirectory, operation: str
) -> tuple[RunSubject, str, dict[str, object]]:
    run_metadata = _read_run_json(run, "run.json", "run metadata")
    if run_metadata.get("adapter_id") != LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID:
        raise ValueError(f"optimizer {operation} requires a completed Evolution run")
    result = _read_run_json(run, "result.json", "run result")
    if result.get("adapter_id") != LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID:
        raise ValueError(f"optimizer {operation} requires an Evolution result")
    run_status = result.get("status")
    if not isinstance(run_status, str) or run_status not in _EVOLUTION_RUN_STATUSES:
        raise ValueError(f"optimizer {operation} requires a completed Evolution result")
    binding = _read_run_json(run, "inputs/subject.json", "materialised subject")
    if set(binding) != {"subject_id", "content_sha256"}:
        raise ValueError("materialised subject has an invalid binding")
    subject_id = binding["subject_id"]
    content_sha256 = binding["content_sha256"]
    if not isinstance(subject_id, str) or not isinstance(content_sha256, str):
        raise ValueError("materialised subject has an invalid binding")
    subject_path = _run_artifact_path(run, "inputs/subject-SKILL.md")
    return (
        RunSubject(
            subject_id=subject_id,
            content_sha256=content_sha256,
            content=subject_path.read_bytes(),
        ),
        run_status,
        result,
    )


def _read_run_json(run: RunDirectory, relative_path: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(_run_artifact_path(run, relative_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _run_artifact_path(run: RunDirectory, relative_path: str) -> Path:
    candidate = run.path / relative_path
    if candidate.is_symlink():
        raise ValueError(f"{relative_path} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(run.path.resolve())
    except (OSError, ValueError) as error:
        raise ValueError(f"{relative_path} must be a local run artifact") from error
    if not resolved.is_file():
        raise ValueError(f"{relative_path} must be a file")
    return resolved


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _echo_run_result(result: BenchmarkRunResult) -> None:
    if result.public_result is not None:
        _echo_json(result.public_result)
        return
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
