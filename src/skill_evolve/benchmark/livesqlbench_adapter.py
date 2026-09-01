"""Trusted adapter facade for the fixed LiveSQLBench Base-Lite suite."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from skill_evolve.benchmark.contracts import (
    BenchmarkAdapterError,
    BenchmarkFixtureError,
    BenchmarkRunResult,
    BenchmarkSuite,
    JsonObject,
    JsonValue,
)
from skill_evolve.benchmark.livesqlbench import (
    LiveSQLBenchDataError,
    LiveSQLBenchSuite,
    evaluate_query_case,
    load_base_lite_suite,
    public_evaluation_dict,
    public_summary,
)
from skill_evolve.benchmark.livesqlbench_run import (
    LiveSQLBenchRunError,
    run_base_lite_dsh_suite,
)
from skill_evolve.config.models import DshConfig, EvaluatorConfig, ModelConfig, OptimizerConfig
from skill_evolve.runner.contracts import RunnerCaseOutput, RunnerOutput
from skill_evolve.sql import LiveSQLBenchFixture, LiveSQLBenchFixtureError
from skill_evolve.subject import RunSubject

LIVE_SQL_BENCH_BASE_LITE_ADAPTER_ID = "livesqlbench-base-lite"
LIVE_SQL_BENCH_DEFAULT_IMAGE = "postgres:14.12"
_EVALUATOR_VERSION = "livesqlbench-base-lite-soft-ex-v1"


class LiveSQLBenchBaseLiteAdapter:
    """Expose existing Base-Lite behavior through the generic adapter boundary."""

    adapter_id = LIVE_SQL_BENCH_BASE_LITE_ADAPTER_ID

    def load_suite(
        self, data_root: Path, manifest_id: str | None = None
    ) -> LiveSQLBenchSuite:
        if manifest_id is not None:
            raise BenchmarkAdapterError("fixed LiveSQLBench adapter does not accept --manifest")
        try:
            return load_base_lite_suite(data_root)
        except LiveSQLBenchDataError as error:
            raise BenchmarkAdapterError(str(error)) from error

    def public_summary(self, suite: BenchmarkSuite) -> JsonObject:
        return _suite(suite).public_summary()

    def fixture_check(self, suite: BenchmarkSuite, image: str) -> JsonObject:
        base_lite_suite = _suite(suite)
        evaluator = _evaluator()
        checks: dict[str, JsonValue]
        try:
            with LiveSQLBenchFixture(
                base_lite_suite.dump_root,
                (case.database_name for case in base_lite_suite.cases),
                evaluator,
                image,
            ) as fixture:
                checks = {
                    case.instance_id: fixture.for_database(case.database_name)
                    .execute_read_only("SELECT current_schema()")
                    .ok
                    for case in base_lite_suite.cases
                }
        except LiveSQLBenchFixtureError as error:
            raise BenchmarkFixtureError(str(error)) from error
        return {
            "benchmark": self.adapter_id,
            "fixture": "ready",
            "checks": checks,
            "image": image,
        }

    def evaluate(
        self, suite: BenchmarkSuite, runner_output: RunnerOutput, image: str
    ) -> JsonObject:
        base_lite_suite = _suite(suite)
        output_by_case = _validated_output_by_case(runner_output, base_lite_suite.case_ids)
        evaluator = _evaluator()
        try:
            with LiveSQLBenchFixture(
                base_lite_suite.dump_root,
                (case.database_name for case in base_lite_suite.cases),
                evaluator,
                image,
            ) as fixture:
                evaluations = tuple(
                    evaluate_query_case(
                        case,
                        output_by_case[case.instance_id],
                        fixture.for_database(case.database_name),
                    )
                    for case in base_lite_suite.cases
                )
        except LiveSQLBenchFixtureError as error:
            raise BenchmarkFixtureError(str(error)) from error
        summary: dict[str, JsonValue] = {
            verdict: count for verdict, count in public_summary(evaluations).items()
        }
        return {
            "benchmark": self.adapter_id,
            "evaluator": evaluator.version,
            "cases": [public_evaluation_dict(evaluation) for evaluation in evaluations],
            "summary": summary,
        }

    def run_dsh(
        self,
        suite: BenchmarkSuite,
        dsh_config: DshConfig,
        profile_patch: Path | None,
        model: ModelConfig,
        runs_root: Path,
        subject: RunSubject | None = None,
        optimizer: OptimizerConfig | None = None,
    ) -> BenchmarkRunResult:
        del optimizer
        try:
            outcome = run_base_lite_dsh_suite(
                _suite(suite), dsh_config, profile_patch, model, runs_root, subject
            )
        except LiveSQLBenchRunError as error:
            raise BenchmarkAdapterError(str(error)) from error
        return BenchmarkRunResult(
            run_id=outcome.run.run_id,
            status=outcome.attribution.status.value,
            attribution=outcome.attribution.kind.value,
            report_path=outcome.run.path / "report.md",
            redacted_session_logs_path=outcome.run.path / "raw" / "dsh",
            exit_code=outcome.exit_code,
        )


def _suite(suite: BenchmarkSuite) -> LiveSQLBenchSuite:
    if not isinstance(suite, LiveSQLBenchSuite):
        raise BenchmarkAdapterError("LiveSQLBench adapter received an incompatible suite")
    return suite


def _validated_output_by_case(
    output: RunnerOutput, expected_case_ids: tuple[str, ...]
) -> Mapping[str, RunnerCaseOutput]:
    actual_case_ids = tuple(case.case_id for case in output.cases)
    if len(actual_case_ids) != len(set(actual_case_ids)):
        raise BenchmarkAdapterError("runner output contains duplicate case_id values")
    if set(actual_case_ids) != set(expected_case_ids):
        raise BenchmarkAdapterError(
            "runner output case IDs must exactly match the five configured LiveSQLBench cases"
        )
    return {case.case_id: case for case in output.cases}


def _evaluator() -> EvaluatorConfig:
    return EvaluatorConfig(version=_EVALUATOR_VERSION)
