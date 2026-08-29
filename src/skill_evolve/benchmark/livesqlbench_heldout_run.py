"""Custody-only DSH execution for the LiveSQLBench final held-out suite."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_evolve.attribution.core import Attribution, RunStatus, attribute, eval_data_failure
from skill_evolve.benchmark.contracts import JsonObject, JsonValue
from skill_evolve.benchmark.livesqlbench import (
    LiveSQLBenchCase,
    evaluate_query_case,
    public_evaluation_dict,
)
from skill_evolve.benchmark.livesqlbench_heldout import (
    HELDOUT_CASE_COUNT,
    HELDOUT_TIERS,
    HeldOutError,
    HeldOutLiveSQLBenchSuite,
)
from skill_evolve.config.models import CaseConfig, DshConfig, EvaluatorConfig, ModelConfig
from skill_evolve.evaluator.core import CaseEvaluation, Verdict
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
from skill_evolve.subject import RunSubject

_EVALUATOR_VERSION = "livesqlbench-base-lite-soft-ex-v1"


class HeldOutLiveSQLBenchRunError(RuntimeError):
    """A custody-only held-out run cannot safely start or retain evidence."""


@dataclass(frozen=True)
class HeldOutLiveSQLBenchRunOutcome:
    """Private evidence location plus the deliberately aggregate terminal result."""

    run: RunDirectory
    environment_fingerprint: str
    attribution: Attribution
    evaluations: tuple[CaseEvaluation, ...]
    public_result: JsonObject

    @property
    def exit_code(self) -> int:
        return 2 if self.attribution.status is RunStatus.EXPERIMENT_PAUSED else 0


def run_heldout_dsh_suite(
    suite: HeldOutLiveSQLBenchSuite,
    dsh_config: DshConfig,
    profile_patch: Path | None,
    model: ModelConfig,
    subject: RunSubject | None = None,
) -> HeldOutLiveSQLBenchRunOutcome:
    """Run all selected cases serially, retaining all case evidence only in custody."""

    if dsh_config.runner != "dsh":
        raise HeldOutLiveSQLBenchRunError("held-out automatic runs require dsh.runner: dsh")
    _require_custody_session_log_root(suite, dsh_config)
    try:
        run = _custody_run_store(suite).create()
    except HeldOutError as error:
        raise HeldOutLiveSQLBenchRunError(str(error)) from error
    started_at = time.monotonic()
    environment_fingerprint = _environment_fingerprint(suite, dsh_config, model, subject)
    _write_initial_custody_evidence(
        run, suite, dsh_config, model, environment_fingerprint, subject
    )
    evaluations: list[CaseEvaluation] = []
    runner_cases: list[RunnerCaseOutput] = []
    stderr_parts: list[str] = []

    for case in suite.cases:
        try:
            runner_case, execution, evaluation = _run_case(
                case,
                suite,
                dsh_config,
                profile_patch,
                model,
                run,
                environment_fingerprint,
                subject,
            )
        except (LiveSQLBenchFixtureError, RunnerError) as error:
            return _finish_paused_run(
                run,
                suite,
                environment_fingerprint,
                tuple(evaluations),
                f"live_fixture_or_runner_invalid: {type(error).__name__}",
                subject,
                _elapsed_milliseconds(started_at),
            )
        evaluations.append(evaluation)
        runner_cases.append(runner_case)
        _write_runner_artifacts(run, case.instance_id, execution)
        if execution.stderr:
            stderr_parts.append(f"[{case.instance_id}]\n{execution.stderr}")

    runner_output = RunnerOutput(schema_version="v1", cases=tuple(runner_cases))
    runner_output_bytes = (runner_output.model_dump_json(indent=2) + "\n").encode()
    run.write_bytes("raw/runner-output.json", runner_output_bytes)
    if stderr_parts:
        run.write_text("raw/runner.stderr.log", "\n\n".join(stderr_parts))
    return _finish_run(
        run,
        suite,
        environment_fingerprint,
        tuple(evaluations),
        subject,
        _elapsed_milliseconds(started_at),
    )


def _run_case(
    case: LiveSQLBenchCase,
    suite: HeldOutLiveSQLBenchSuite,
    dsh_config: DshConfig,
    profile_patch: Path | None,
    model: ModelConfig,
    run: RunDirectory,
    environment_fingerprint: str,
    subject: RunSubject | None,
) -> tuple[RunnerCaseOutput, RunnerExecution, CaseEvaluation]:
    evaluator = EvaluatorConfig(version=_EVALUATOR_VERSION)
    with LiveSQLBenchFixture(suite.dump_root, (case.database_name,), evaluator) as fixture:
        with ReadOnlyQueryService(fixture.for_database(case.database_name)) as query_service:
            run_input = RunInput(
                run_id=run.run_id,
                workspace=suite.data_root,
                environment_fingerprint=environment_fingerprint,
                subject_ref=subject.subject_id if subject is not None else None,
                cases=(_runner_case_config(case),),
                schema_file=case.schema_file,
                subject=subject,
                case_context_files=((case.instance_id, case.public_context_files[1:]),),
                agent_workspace_root=run.path / "agent-workspace",
                database_access=ReadOnlyDatabaseAccess(query_endpoint=query_service.endpoint),
            )
            execution = DshRunner(dsh_config, profile_patch, model).run(run_input)
        if len(execution.output.cases) != 1:
            raise RunnerError("DSH did not provide one held-out case output")
        runner_case = execution.output.cases[0]
        if runner_case.case_id != case.instance_id:
            raise RunnerError("DSH returned an output for an unexpected held-out case")
        evaluation = evaluate_query_case(
            case,
            runner_case,
            fixture.for_database(case.database_name),
        )
    return runner_case, execution, evaluation


def _runner_case_config(case: LiveSQLBenchCase) -> CaseConfig:
    """Build a DSH input that never contains the selected case's Gold SQL."""

    return CaseConfig(
        id=case.instance_id,
        slice="held_out",
        prompt=case.prompt,
        expected_query="SELECT 1",
    )


def _write_initial_custody_evidence(
    run: RunDirectory,
    suite: HeldOutLiveSQLBenchSuite,
    dsh_config: DshConfig,
    model: ModelConfig,
    environment_fingerprint: str,
    subject: RunSubject | None,
) -> None:
    subject_binding = subject.public_binding() if subject is not None else None
    environment: dict[str, Any] = {
        "environment_fingerprint": environment_fingerprint,
        "benchmark": {
            "suite_id": suite.manifest.suite_id,
            "manifest_sha256": suite.manifest.manifest_sha256,
            "case_count": HELDOUT_CASE_COUNT,
        },
        "dsh": dsh_config.model_dump(mode="json"),
        "model": model.model_dump(mode="json"),
        "evaluator": {"version": _EVALUATOR_VERSION},
    }
    if subject_binding is not None:
        environment["subject"] = subject_binding
    run.write_json("environment.json", environment)
    metadata: dict[str, Any] = {
        "schema_version": "livesqlbench-heldout-v1",
        "run_id": run.run_id,
        "benchmark": suite.manifest.suite_id,
        "phase": "snapshotted",
    }
    if subject_binding is not None:
        metadata["subject"] = subject_binding
    run.write_json("run.json", metadata)
    if subject is not None:
        run.materialise_subject(subject)


def _write_runner_artifacts(
    run: RunDirectory, case_id: str, execution: RunnerExecution
) -> None:
    for artifact in execution.raw_artifacts:
        run.write_bytes(f"raw/{artifact.relative_path}", artifact.content)
    run.write_bytes(f"raw/dsh/{case_id}.runner-output.json", execution.raw_output)


def _finish_paused_run(
    run: RunDirectory,
    suite: HeldOutLiveSQLBenchSuite,
    environment_fingerprint: str,
    evaluations: tuple[CaseEvaluation, ...],
    reason: str,
    subject: RunSubject | None,
    elapsed_milliseconds: int,
) -> HeldOutLiveSQLBenchRunOutcome:
    return _write_final_custody_evidence(
        run,
        suite,
        environment_fingerprint,
        evaluations,
        eval_data_failure(reason),
        subject,
        elapsed_milliseconds,
    )


def _finish_run(
    run: RunDirectory,
    suite: HeldOutLiveSQLBenchSuite,
    environment_fingerprint: str,
    evaluations: tuple[CaseEvaluation, ...],
    subject: RunSubject | None,
    elapsed_milliseconds: int,
) -> HeldOutLiveSQLBenchRunOutcome:
    return _write_final_custody_evidence(
        run,
        suite,
        environment_fingerprint,
        evaluations,
        attribute(evaluations),
        subject,
        elapsed_milliseconds,
    )


def _write_final_custody_evidence(
    run: RunDirectory,
    suite: HeldOutLiveSQLBenchSuite,
    environment_fingerprint: str,
    evaluations: tuple[CaseEvaluation, ...],
    attribution: Attribution,
    subject: RunSubject | None,
    elapsed_milliseconds: int,
) -> HeldOutLiveSQLBenchRunOutcome:
    private_evaluations = [public_evaluation_dict(evaluation) for evaluation in evaluations]
    run.write_json("evaluation.json", {"cases": private_evaluations})
    run.write_json("attribution.json", attribution.to_dict())
    private_result: dict[str, Any] = {
        "schema_version": "livesqlbench-heldout-v1",
        "run_id": run.run_id,
        "environment_fingerprint": environment_fingerprint,
        "status": attribution.status.value,
        "attribution": attribution.to_dict(),
        "cases": private_evaluations,
    }
    if subject is not None:
        private_result["subject"] = subject.public_binding()
    run.write_json("result.json", private_result)
    run.write_text(
        "report.md",
        build_report(run.run_id, environment_fingerprint, attribution, evaluations),
    )
    public_result = _public_result(
        suite, evaluations, attribution, subject, elapsed_milliseconds
    )
    run.write_json("aggregate-result.json", public_result)
    return HeldOutLiveSQLBenchRunOutcome(
        run=run,
        environment_fingerprint=environment_fingerprint,
        attribution=attribution,
        evaluations=evaluations,
        public_result=public_result,
    )


def _public_result(
    suite: HeldOutLiveSQLBenchSuite,
    evaluations: tuple[CaseEvaluation, ...],
    attribution: Attribution,
    subject: RunSubject | None,
    elapsed_milliseconds: int,
) -> JsonObject:
    metrics = _tier_metrics(suite, evaluations)
    public_result: JsonObject = {
        "suite_id": suite.manifest.suite_id,
        "manifest_sha256": suite.manifest.manifest_sha256,
        "subject_binding": _public_subject_binding(subject),
        "status": attribution.status.value,
        "case_count": HELDOUT_CASE_COUNT,
        "tiers": metrics,
        "macro_average": _macro_average(metrics),
        "duration_ms": elapsed_milliseconds,
        "cost_usd": _total_cost(evaluations),
    }
    return public_result


def _tier_metrics(
    suite: HeldOutLiveSQLBenchSuite, evaluations: tuple[CaseEvaluation, ...]
) -> dict[str, JsonValue]:
    tier_by_case_id = {case.instance_id: case.difficulty_tier for case in suite.cases}
    grouped: dict[str, list[CaseEvaluation]] = {tier: [] for tier in HELDOUT_TIERS}
    for evaluation in evaluations:
        tier = tier_by_case_id.get(evaluation.case_id)
        if tier is None:
            raise HeldOutLiveSQLBenchRunError("held-out evaluator produced invalid evidence")
        grouped[tier].append(evaluation)
    return {
        tier: {
            "case_count": 10,
            "evaluated_count": len(grouped[tier]),
            "passed_count": sum(
                evaluation.verdict is Verdict.PASS for evaluation in grouped[tier]
            ),
            "pass_rate": sum(
                evaluation.verdict is Verdict.PASS for evaluation in grouped[tier]
            )
            / 10,
        }
        for tier in HELDOUT_TIERS
    }


def _macro_average(metrics: dict[str, JsonValue]) -> float | None:
    tier_results: list[dict[str, JsonValue]] = []
    for tier in HELDOUT_TIERS:
        value = metrics[tier]
        if not isinstance(value, dict):
            return None
        tier_results.append(value)
    if any(item.get("evaluated_count") != 10 for item in tier_results):
        return None
    rates: list[float] = []
    for item in tier_results:
        rate = item.get("pass_rate")
        if not isinstance(rate, (float, int)):
            return None
        rates.append(float(rate))
    return sum(rates) / len(rates)


def _total_cost(evaluations: tuple[CaseEvaluation, ...]) -> float | None:
    if len(evaluations) != HELDOUT_CASE_COUNT:
        return None
    costs = [evaluation.usage.get("cost_usd") for evaluation in evaluations]
    known_costs: list[float] = []
    for cost in costs:
        if not isinstance(cost, (float, int)):
            return None
        known_costs.append(float(cost))
    return sum(known_costs)


def _public_subject_binding(subject: RunSubject | None) -> JsonObject | None:
    if subject is None:
        return None
    return {
        "subject_id": subject.subject_id,
        "content_sha256": subject.content_sha256,
    }


def _environment_fingerprint(
    suite: HeldOutLiveSQLBenchSuite,
    dsh_config: DshConfig,
    model: ModelConfig,
    subject: RunSubject | None,
) -> str:
    payload: dict[str, Any] = {
        "benchmark": suite.manifest.suite_id,
        "manifest_sha256": suite.manifest.manifest_sha256,
        "source_digests": suite.source_digests(),
        "dsh": dsh_config.model_dump(mode="json"),
        "model": model.model_dump(mode="json"),
        "evaluator": _EVALUATOR_VERSION,
    }
    if subject is not None:
        payload["subject"] = subject.public_binding()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _custody_run_store(suite: HeldOutLiveSQLBenchSuite) -> RunStore:
    runs_root = suite.custody.runs_root
    try:
        if os.path.lexists(runs_root):
            if runs_root.is_symlink() or not runs_root.is_dir():
                raise HeldOutError("held-out evaluator custody root is unavailable")
        else:
            runs_root.mkdir()
        if runs_root.is_symlink() or runs_root.resolve(strict=True).parent != suite.custody.root:
            raise HeldOutError("held-out evaluator custody root is unavailable")
    except OSError as error:
        raise HeldOutError("held-out evaluator custody root is unavailable") from error
    return RunStore(runs_root)


def _require_custody_session_log_root(
    suite: HeldOutLiveSQLBenchSuite, dsh_config: DshConfig
) -> None:
    """Require the DSH audit-log location to be an existing custody child.

    The DSH deployment itself must be configured to write there; this check
    makes a missing or differently mounted session root fail before Docker or
    DSH can start instead of silently leaving held-out evidence elsewhere.
    """

    configured = dsh_config.session_log_root
    if configured is None:
        raise HeldOutLiveSQLBenchRunError(
            "held-out runs require a custody-owned dsh.session_log_root"
        )
    try:
        supplied = Path(configured)
        required = suite.custody.dsh_session_logs_root
        if not supplied.is_absolute() or supplied != required:
            raise HeldOutLiveSQLBenchRunError(
                "held-out runs require a custody-owned dsh.session_log_root"
            )
        if supplied.is_symlink() or not supplied.is_dir():
            raise HeldOutLiveSQLBenchRunError(
                "held-out runs require a custody-owned dsh.session_log_root"
            )
        if supplied.resolve(strict=True).parent != suite.custody.root:
            raise HeldOutLiveSQLBenchRunError(
                "held-out runs require a custody-owned dsh.session_log_root"
            )
    except OSError:
        raise HeldOutLiveSQLBenchRunError(
            "held-out runs require a custody-owned dsh.session_log_root"
        ) from None


def _elapsed_milliseconds(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))
