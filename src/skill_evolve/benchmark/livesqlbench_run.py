"""Auditable, GT-safe DSH execution for the fixed LiveSQLBench smoke suite."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_evolve.attribution.core import Attribution, RunStatus, attribute, eval_data_failure
from skill_evolve.benchmark.livesqlbench import (
    LiveSQLBenchCase,
    LiveSQLBenchSuite,
    evaluate_query_case,
    load_base_lite_suite,
    public_evaluation_dict,
    public_summary,
)
from skill_evolve.candidate.contracts import ResolvedCandidate
from skill_evolve.config.models import CaseConfig, DshConfig, EvaluatorConfig, ModelConfig
from skill_evolve.evaluator.core import CaseEvaluation
from skill_evolve.registry.store import RunDirectory, RunStore
from skill_evolve.reporting.artifacts import build_report
from skill_evolve.runner.contracts import (
    ReadOnlyDatabaseAccess,
    RunInput,
    RunnerCaseOutput,
    RunnerError,
    RunnerExecution,
    RunnerOutput,
)
from skill_evolve.runner.dsh import DshRunner
from skill_evolve.sql.livesqlbench import LiveSQLBenchFixture, LiveSQLBenchFixtureError
from skill_evolve.sql.readonly_service import ReadOnlyQueryService

_EVALUATOR_VERSION = "livesqlbench-base-lite-soft-ex-v1"


class LiveSQLBenchRunError(RuntimeError):
    """A LiveSQLBench execution cannot safely start."""


@dataclass(frozen=True)
class LiveSQLBenchRunOutcome:
    """Public result metadata for one completed or paused benchmark run."""

    run: RunDirectory
    environment_fingerprint: str
    attribution: Attribution
    evaluations: tuple[CaseEvaluation, ...]

    @property
    def exit_code(self) -> int:
        return 2 if self.attribution.status is RunStatus.EXPERIMENT_PAUSED else 0


def run_base_lite_dsh(
    data_root: Path,
    dsh_config: DshConfig,
    profile_patch: Path | None,
    model: ModelConfig,
    runs_root: Path,
    candidate: ResolvedCandidate | None = None,
) -> LiveSQLBenchRunOutcome:
    """Run five public tasks serially and score them without persisting GT material."""

    return run_base_lite_dsh_suite(
        load_base_lite_suite(data_root), dsh_config, profile_patch, model, runs_root, candidate
    )


def run_base_lite_dsh_suite(
    suite: LiveSQLBenchSuite,
    dsh_config: DshConfig,
    profile_patch: Path | None,
    model: ModelConfig,
    runs_root: Path,
    candidate: ResolvedCandidate | None = None,
) -> LiveSQLBenchRunOutcome:
    """Run one already-validated Base-Lite suite through the existing DSH boundary."""

    if dsh_config.runner != "dsh":
        raise LiveSQLBenchRunError("LiveSQLBench automatic runs require dsh.runner: dsh")
    environment_fingerprint = _environment_fingerprint(suite, dsh_config, model, candidate)
    run = RunStore(runs_root).create()
    _write_initial_run_evidence(run, suite, dsh_config, model, environment_fingerprint, candidate)
    evaluations: list[CaseEvaluation] = []
    runner_cases: list[RunnerCaseOutput] = []
    trajectories: list[dict[str, Any]] = []
    stderr_parts: list[str] = []

    for case in suite.cases:
        try:
            runner_case, execution, evaluation = _run_case(
                case, suite, dsh_config, profile_patch, run, environment_fingerprint, candidate
            )
        except (LiveSQLBenchFixtureError, RunnerError) as error:
            return _finish_paused_run(
                run,
                environment_fingerprint,
                tuple(evaluations),
                f"live_fixture_or_runner_invalid: {type(error).__name__}",
                candidate,
            )
        evaluations.append(evaluation)
        runner_cases.append(runner_case)
        trajectories.append(_public_trajectory(runner_case))
        _write_runner_artifacts(run, case, execution)
        if execution.stderr:
            stderr_parts.append(f"[{case.instance_id}]\n{execution.stderr}")

    runner_output = RunnerOutput(schema_version="v1", cases=tuple(runner_cases))
    runner_output_json = (runner_output.model_dump_json(indent=2) + "\n").encode()
    run.write_bytes("raw/runner-output.json", runner_output_json)
    if stderr_parts:
        run.write_text("raw/runner.stderr.log", "\n\n".join(stderr_parts))
    run.write_json("trajectory.json", {"cases": trajectories})
    return _finish_run(run, environment_fingerprint, tuple(evaluations), candidate)


def _run_case(
    case: LiveSQLBenchCase,
    suite: LiveSQLBenchSuite,
    dsh_config: DshConfig,
    profile_patch: Path | None,
    run: RunDirectory,
    environment_fingerprint: str,
    candidate: ResolvedCandidate | None,
) -> tuple[RunnerCaseOutput, RunnerExecution, CaseEvaluation]:
    evaluator = EvaluatorConfig(version=_EVALUATOR_VERSION)
    runner_case_config = _runner_case_config(case)
    with LiveSQLBenchFixture(suite.dump_root, (case.database_name,), evaluator) as fixture:
        with ReadOnlyQueryService(fixture.for_database(case.database_name)) as query_service:
            input = RunInput(
                run_id=run.run_id,
                workspace=suite.data_root,
                environment_fingerprint=environment_fingerprint,
                candidate_ref=candidate.candidate_id if candidate is not None else None,
                cases=(runner_case_config,),
                schema_file=case.schema_file,
                candidate=candidate,
                case_context_files=((case.instance_id, case.public_context_files[1:]),),
                agent_workspace_root=run.path / "agent-workspace",
                database_access=ReadOnlyDatabaseAccess(query_endpoint=query_service.endpoint),
            )
            execution = DshRunner(dsh_config, profile_patch).run(input)
        if len(execution.output.cases) != 1:
            raise RunnerError(
                f"DSH returned {len(execution.output.cases)} outputs for {case.instance_id}"
            )
        runner_case = execution.output.cases[0]
        if runner_case.case_id != case.instance_id:
            raise RunnerError(f"DSH returned an output for unexpected case {runner_case.case_id!r}")
        evaluation = evaluate_query_case(
            case,
            runner_case,
            fixture.for_database(case.database_name),
        )
    return runner_case, execution, evaluation


def _runner_case_config(case: LiveSQLBenchCase) -> CaseConfig:
    """Build a runner-only case that cannot carry gold SQL into DSH."""

    return CaseConfig(
        id=case.instance_id,
        slice="fast",
        prompt=case.prompt,
        expected_query="SELECT 1",
    )


def _write_initial_run_evidence(
    run: RunDirectory,
    suite: LiveSQLBenchSuite,
    dsh_config: DshConfig,
    model: ModelConfig,
    environment_fingerprint: str,
    candidate: ResolvedCandidate | None,
) -> None:
    candidate_binding = candidate.public_binding() if candidate is not None else None
    environment: dict[str, Any] = {
        "environment_fingerprint": environment_fingerprint,
        "benchmark": suite.public_summary(),
        "dsh": dsh_config.model_dump(mode="json"),
        "model": model.model_dump(mode="json"),
        "evaluator": {"version": _EVALUATOR_VERSION},
    }
    if candidate_binding is not None:
        environment["candidate"] = candidate_binding
    run.write_json(
        "environment.json",
        environment,
    )
    run_metadata: dict[str, Any] = {
        "schema_version": "livesqlbench-v1",
        "run_id": run.run_id,
        "benchmark": "livesqlbench-base-lite",
        "requested_suite": "fast",
        "phase": "snapshotted",
    }
    if candidate_binding is not None:
        run_metadata["candidate"] = candidate_binding
    run.write_json(
        "run.json",
        run_metadata,
    )
    if candidate is not None:
        run.materialise_candidate(candidate)


def _write_runner_artifacts(
    run: RunDirectory, case: LiveSQLBenchCase, execution: RunnerExecution
) -> None:
    for artifact in execution.raw_artifacts:
        run.write_bytes(f"raw/{artifact.relative_path}", artifact.content)
    run.write_bytes(
        f"raw/dsh/{case.instance_id}.runner-output.json",
        execution.raw_output,
    )


def _public_trajectory(runner_case: RunnerCaseOutput) -> dict[str, Any]:
    return {
        "case_id": runner_case.case_id,
        "candidate_sql": runner_case.candidate_sql,
        "trajectory": [event.model_dump(mode="json") for event in runner_case.trajectory],
        "usage": runner_case.usage.model_dump(mode="json"),
    }


def _finish_paused_run(
    run: RunDirectory,
    environment_fingerprint: str,
    evaluations: tuple[CaseEvaluation, ...],
    reason: str,
    candidate: ResolvedCandidate | None,
) -> LiveSQLBenchRunOutcome:
    attribution = eval_data_failure(reason)
    return _write_final_run_evidence(
        run, environment_fingerprint, evaluations, attribution, candidate
    )


def _finish_run(
    run: RunDirectory,
    environment_fingerprint: str,
    evaluations: tuple[CaseEvaluation, ...],
    candidate: ResolvedCandidate | None,
) -> LiveSQLBenchRunOutcome:
    return _write_final_run_evidence(
        run,
        environment_fingerprint,
        evaluations,
        attribute(evaluations),
        candidate,
    )


def _write_final_run_evidence(
    run: RunDirectory,
    environment_fingerprint: str,
    evaluations: tuple[CaseEvaluation, ...],
    attribution: Attribution,
    candidate: ResolvedCandidate | None,
) -> LiveSQLBenchRunOutcome:
    public_evaluations = [public_evaluation_dict(evaluation) for evaluation in evaluations]
    run.write_json("evaluation.json", {"cases": public_evaluations})
    run.write_json("attribution.json", attribution.to_dict())
    result: dict[str, Any] = {
            "schema_version": "livesqlbench-v1",
            "run_id": run.run_id,
            "environment_fingerprint": environment_fingerprint,
            "status": attribution.status.value,
            "attribution": attribution.to_dict(),
            "summary": public_summary(evaluations),
            "cases": public_evaluations,
    }
    if candidate is not None:
        result["candidate"] = candidate.public_binding()
    run.write_json("result.json", result)
    run.write_text(
        "report.md",
        build_report(run.run_id, environment_fingerprint, attribution, evaluations),
    )
    return LiveSQLBenchRunOutcome(
        run=run,
        environment_fingerprint=environment_fingerprint,
        attribution=attribution,
        evaluations=evaluations,
    )


def _environment_fingerprint(
    suite: LiveSQLBenchSuite,
    dsh_config: DshConfig,
    model: ModelConfig,
    candidate: ResolvedCandidate | None,
) -> str:
    payload = {
        "benchmark": "livesqlbench-base-lite",
        "source_digests": suite.source_digests(),
        "dsh": dsh_config.model_dump(mode="json"),
        "model": model.model_dump(mode="json"),
        "evaluator": _EVALUATOR_VERSION,
    }
    if candidate is not None:
        payload["candidate"] = candidate.public_binding()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
