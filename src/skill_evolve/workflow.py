"""Orchestrate one auditable, local SQL Skill evaluation workflow."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_evolve.attribution.core import Attribution, RunStatus, attribute, eval_data_failure
from skill_evolve.candidate.contracts import ResolvedCandidate
from skill_evolve.config.loader import ResolvedManifest, load_manifest
from skill_evolve.config.models import CaseConfig
from skill_evolve.evaluator.core import CaseEvaluation, evaluate_case
from skill_evolve.fingerprint.snapshot import EnvironmentSnapshot, create_snapshot
from skill_evolve.registry.store import RunDirectory, RunStore
from skill_evolve.reporting.artifacts import (
    build_regression_draft,
    build_repair_checklist,
    build_report,
    build_skill_change_suggestion,
)
from skill_evolve.runner.contracts import (
    ReadOnlyDatabaseAccess,
    RunInput,
    RunnerError,
    RunnerExecution,
)
from skill_evolve.runner.dsh import DshRunner
from skill_evolve.runner.fake import FakeRunner
from skill_evolve.sql.postgres import FixtureError, PostgresFixture
from skill_evolve.sql.readonly_service import ReadOnlyQueryService


@dataclass(frozen=True)
class RunOutcome:
    run: RunDirectory
    snapshot: EnvironmentSnapshot
    attribution: Attribution
    evaluations: tuple[CaseEvaluation, ...]

    @property
    def exit_code(self) -> int:
        return 2 if self.attribution.status is RunStatus.EXPERIMENT_PAUSED else 0


def run_experiment(
    manifest_path: Path,
    suite: str | None,
    runs_root: Path,
    candidate: ResolvedCandidate | None = None,
) -> RunOutcome:
    """Execute a single run, producing immutable evidence even when it pauses."""

    resolved = load_manifest(manifest_path)
    selected_cases = resolved.select_cases(suite)
    snapshot = create_snapshot(resolved, candidate)
    run = RunStore(runs_root).create()
    run.materialise_manifest(resolved)
    if candidate is not None:
        run.materialise_candidate(candidate)
    run.write_json("environment.json", snapshot.to_dict())
    run_metadata: dict[str, Any] = {
            "schema_version": "v1",
            "run_id": run.run_id,
            "experiment": resolved.manifest.experiment,
            "requested_suite": suite,
            "phase": "snapshotted",
    }
    if candidate is not None:
        run_metadata["candidate"] = candidate.public_binding()
    run.write_json("run.json", run_metadata)

    try:
        if resolved.manifest.environment.dsh.runner == "dsh":
            with PostgresFixture(
                resolved.manifest.environment.database,
                resolved.manifest.environment.evaluator,
                resolved.schema_file,
                resolved.seed_file,
            ) as exploration_fixture, ReadOnlyQueryService(exploration_fixture) as query_service:
                execution = _run_runner(
                    resolved,
                    run,
                    snapshot,
                    selected_cases,
                    ReadOnlyDatabaseAccess(query_endpoint=query_service.endpoint),
                    candidate,
                )
        else:
            execution = _run_runner(resolved, run, snapshot, selected_cases, candidate=candidate)
    except RunnerError as error:
        return _finish_paused_run(run, snapshot, (), "runner_evidence_invalid", str(error))
    except FixtureError as error:
        return _finish_paused_run(run, snapshot, (), "postgres_fixture_invalid", str(error))

    run.write_bytes("raw/runner-output.json", execution.raw_output)
    for artifact in execution.raw_artifacts:
        run.write_bytes(f"raw/{artifact.relative_path}", artifact.content)
    if execution.stderr:
        run.write_text("raw/runner.stderr.log", execution.stderr)
    run.write_json(
        "trajectory.json",
        {
            "cases": [
                {
                    "case_id": case.case_id,
                    "candidate_sql": case.candidate_sql,
                    "trajectory": [event.model_dump(mode="json") for event in case.trajectory],
                    "usage": case.usage.model_dump(mode="json"),
                }
                for case in execution.output.cases
            ]
        },
    )

    runner_cases = {case.case_id: case for case in execution.output.cases}
    try:
        with PostgresFixture(
            resolved.manifest.environment.database,
            resolved.manifest.environment.evaluator,
            resolved.schema_file,
            resolved.seed_file,
        ) as fixture:
            evaluations = tuple(
                evaluate_case(
                    case,
                    runner_cases[case.id],
                    fixture,
                    resolved.manifest.environment.evaluator,
                )
                for case in selected_cases
            )
    except FixtureError as error:
        return _finish_paused_run(run, snapshot, (), "postgres_fixture_invalid", str(error))

    attribution = attribute(evaluations)
    return _finish_run(run, snapshot, evaluations, attribution)


def _run_runner(
    resolved: ResolvedManifest,
    run: RunDirectory,
    snapshot: EnvironmentSnapshot,
    selected_cases: tuple[CaseConfig, ...],
    database_access: ReadOnlyDatabaseAccess | None = None,
    candidate: ResolvedCandidate | None = None,
) -> RunnerExecution:
    if resolved.fake_output is not None:
        run.write_bytes("raw/runner-output.json", resolved.fake_output.read_bytes())
    input = RunInput(
        run_id=run.run_id,
        workspace=resolved.workspace,
        environment_fingerprint=snapshot.fingerprint,
        candidate_ref=(
            candidate.candidate_id if candidate is not None else resolved.manifest.candidate.ref
        ),
        cases=selected_cases,
        schema_file=resolved.schema_file,
        candidate_file=resolved.candidate_digest_file,
        candidate=candidate,
        case_context_files=resolved.case_context_files,
        agent_workspace_root=run.path / "agent-workspace",
        database_access=database_access,
    )
    if resolved.manifest.environment.dsh.runner == "fake":
        if resolved.fake_output is None:
            raise RunnerError("fake runner requires a fake output file")
        return FakeRunner(resolved.fake_output).run(input)
    return DshRunner(
        resolved.manifest.environment.dsh,
        resolved.profile_patch,
        resolved.manifest.environment.model,
    ).run(input)


def _finish_paused_run(
    run: RunDirectory,
    snapshot: EnvironmentSnapshot,
    evaluations: tuple[CaseEvaluation, ...],
    reason: str,
    detail: str,
) -> RunOutcome:
    attribution = eval_data_failure(reason)
    return _finish_run(run, snapshot, evaluations, attribution, paused_detail=detail)


def _finish_run(
    run: RunDirectory,
    snapshot: EnvironmentSnapshot,
    evaluations: tuple[CaseEvaluation, ...],
    attribution: Attribution,
    paused_detail: str | None = None,
) -> RunOutcome:
    run.write_json(
        "evaluation.json", {"cases": [evaluation.to_dict() for evaluation in evaluations]}
    )
    run.write_json("attribution.json", attribution.to_dict())
    result = {
        "schema_version": "v1",
        "run_id": run.run_id,
        "environment_fingerprint": snapshot.fingerprint,
        "status": attribution.status.value,
        "attribution": attribution.to_dict(),
        "summary": _summary(evaluations),
        "cases": [evaluation.to_dict() for evaluation in evaluations],
    }
    if snapshot.candidate is not None:
        result["candidate"] = snapshot.candidate
    run.write_json("result.json", result)
    run.write_text(
        "report.md", build_report(run.run_id, snapshot.fingerprint, attribution, evaluations)
    )
    if attribution.status is RunStatus.EXPERIMENT_PAUSED:
        detail = paused_detail or _paused_case_detail(evaluations)
        run.write_text("repair-checklist.md", build_repair_checklist(attribution, detail))
    elif attribution.status is RunStatus.NEEDS_SKILL_CHANGE:
        run.write_text(
            "regression-case.draft.yaml",
            build_regression_draft(run.run_id, snapshot.fingerprint, evaluations),
        )
        run.write_text("skill-change-suggestion.md", build_skill_change_suggestion(evaluations))
    return RunOutcome(run=run, snapshot=snapshot, attribution=attribution, evaluations=evaluations)


def _summary(evaluations: tuple[CaseEvaluation, ...]) -> dict[str, Any]:
    verdicts = Counter(evaluation.verdict.value for evaluation in evaluations)
    slices: dict[str, dict[str, int]] = {}
    for evaluation in evaluations:
        slice_counts = slices.setdefault(evaluation.slice, {})
        slice_counts[evaluation.verdict.value] = slice_counts.get(evaluation.verdict.value, 0) + 1
    return {"verdicts": dict(sorted(verdicts.items())), "slices": slices}


def _paused_case_detail(evaluations: tuple[CaseEvaluation, ...]) -> str | None:
    details = [
        f"{evaluation.case_id}: {evaluation.detail}"
        for evaluation in evaluations
        if evaluation.detail is not None
        and evaluation.verdict.value in {"evaluator_invalid", "runner_error"}
    ]
    return "\n".join(details) or None
