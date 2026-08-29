"""Trusted adapter facades for custody-owned rolling LiveSQLBench suites."""

from __future__ import annotations

from pathlib import Path

from skill_evolve.benchmark.contracts import (
    BenchmarkAdapterError,
    BenchmarkFixtureError,
    BenchmarkRunResult,
    BenchmarkSuite,
    JsonObject,
)
from skill_evolve.benchmark.livesqlbench_rotation import (
    LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
    LIVE_SQL_BENCH_BASE_LITE_SMOKE_ROTATION_ADAPTER_ID,
    RotationError,
    RotationLiveSQLBenchSuite,
    load_rotation_suite,
)
from skill_evolve.benchmark.livesqlbench_rotation_run import (
    RotationRunError,
    run_rotation_dsh_suite,
)
from skill_evolve.config.models import DshConfig, EvaluatorConfig, ModelConfig
from skill_evolve.runner.contracts import RunnerOutput
from skill_evolve.sql import LiveSQLBenchFixture, LiveSQLBenchFixtureError
from skill_evolve.subject import RunSubject

_EVALUATOR_VERSION = "livesqlbench-base-lite-soft-ex-v1"


class _RotationAdapter:
    adapter_id: str

    def load_suite(
        self, data_root: Path, manifest_id: str | None = None
    ) -> RotationLiveSQLBenchSuite:
        if manifest_id is None:
            raise BenchmarkAdapterError("rolling LiveSQLBench adapters require --manifest")
        try:
            suite = load_rotation_suite(
                data_root,
                manifest_id,
                repository_root=Path.cwd(),
                candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
            )
        except RotationError as error:
            raise BenchmarkAdapterError(str(error)) from error
        if suite.adapter_id != self.adapter_id:
            raise BenchmarkAdapterError("rotation manifest belongs to a different adapter")
        return suite

    def public_summary(self, suite: BenchmarkSuite) -> JsonObject:
        return _suite(suite, self.adapter_id).public_summary()

    def fixture_check(self, suite: BenchmarkSuite, image: str) -> JsonObject:
        rotation_suite = _suite(suite, self.adapter_id)
        evaluator = EvaluatorConfig(version=_EVALUATOR_VERSION)
        try:
            with LiveSQLBenchFixture(
                rotation_suite.dump_root,
                (case.database_name for case in rotation_suite.cases),
                evaluator,
                image,
            ) as fixture:
                for case in rotation_suite.cases:
                    if not fixture.for_database(case.database_name).execute_read_only(
                        "SELECT current_schema()"
                    ).ok:
                        raise BenchmarkFixtureError("rolling fixture verification failed")
        except LiveSQLBenchFixtureError as error:
            raise BenchmarkFixtureError("rolling fixture verification failed") from error
        return {**rotation_suite.public_summary(), "fixture": "ready"}

    def evaluate(
        self, suite: BenchmarkSuite, runner_output: RunnerOutput, image: str
    ) -> JsonObject:
        del suite, runner_output, image
        raise BenchmarkAdapterError("rolling suites require managed benchmark run execution")

    def run_dsh(
        self,
        suite: BenchmarkSuite,
        dsh_config: DshConfig,
        profile_patch: Path | None,
        model: ModelConfig,
        runs_root: Path,
        subject: RunSubject | None = None,
    ) -> BenchmarkRunResult:
        try:
            outcome = run_rotation_dsh_suite(
                _suite(suite, self.adapter_id),
                dsh_config,
                profile_patch,
                model,
                runs_root,
                subject,
            )
        except RotationRunError as error:
            raise BenchmarkAdapterError(str(error)) from error
        return BenchmarkRunResult(
            run_id=outcome.run.run_id,
            status=outcome.status,
            attribution=(
                "infrastructure"
                if outcome.status in {"runner_invalid", "evaluator_invalid"}
                else "none"
            ),
            report_path=outcome.run.path / "report.md",
            redacted_session_logs_path=outcome.run.path / "raw" / "dsh",
            exit_code=outcome.exit_code,
            public_result=outcome.public_result,
        )


class LiveSQLBenchBaseLiteSmokeRotationAdapter(_RotationAdapter):
    adapter_id = LIVE_SQL_BENCH_BASE_LITE_SMOKE_ROTATION_ADAPTER_ID


class LiveSQLBenchBaseLiteEvolutionAdapter(_RotationAdapter):
    adapter_id = LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID


def _suite(suite: BenchmarkSuite, adapter_id: str) -> RotationLiveSQLBenchSuite:
    if not isinstance(suite, RotationLiveSQLBenchSuite) or suite.adapter_id != adapter_id:
        raise BenchmarkAdapterError("rotation adapter received an incompatible suite")
    return suite
