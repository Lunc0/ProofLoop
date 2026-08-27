"""Static adapter facade for the custody-only LiveSQLBench final suite."""

from __future__ import annotations

from pathlib import Path

from skill_evolve.benchmark.contracts import (
    BenchmarkAdapterError,
    BenchmarkFixtureError,
    BenchmarkRunResult,
    BenchmarkSuite,
    JsonObject,
)
from skill_evolve.benchmark.livesqlbench_heldout import (
    LIVE_SQL_BENCH_BASE_LITE_HELDOUT_ADAPTER_ID,
    HeldOutError,
    HeldOutLiveSQLBenchSuite,
    load_heldout_suite,
)
from skill_evolve.benchmark.livesqlbench_heldout_run import (
    HeldOutLiveSQLBenchRunError,
    run_heldout_dsh_suite,
)
from skill_evolve.candidate.contracts import ResolvedCandidate
from skill_evolve.config.models import DshConfig, EvaluatorConfig, ModelConfig
from skill_evolve.runner.contracts import RunnerOutput
from skill_evolve.sql import LiveSQLBenchFixture, LiveSQLBenchFixtureError

_EVALUATOR_VERSION = "livesqlbench-base-lite-soft-ex-v1"


class LiveSQLBenchBaseLiteHeldOutAdapter:
    """Expose a final evaluator-owned suite without a public case-level interface."""

    adapter_id: str = LIVE_SQL_BENCH_BASE_LITE_HELDOUT_ADAPTER_ID

    def load_suite(self, data_root: Path) -> HeldOutLiveSQLBenchSuite:
        try:
            return load_heldout_suite(
                data_root,
                repository_root=Path.cwd(),
                candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
            )
        except HeldOutError as error:
            raise BenchmarkAdapterError(str(error)) from error

    def public_summary(self, suite: BenchmarkSuite) -> JsonObject:
        return _suite(suite).public_summary()

    def fixture_check(self, suite: BenchmarkSuite, image: str) -> JsonObject:
        heldout_suite = _suite(suite)
        evaluator = EvaluatorConfig(version=_EVALUATOR_VERSION)
        try:
            with LiveSQLBenchFixture(
                heldout_suite.dump_root,
                (case.database_name for case in heldout_suite.cases),
                evaluator,
                image,
            ) as fixture:
                for case in heldout_suite.cases:
                    if not fixture.for_database(case.database_name).execute_read_only(
                        "SELECT current_schema()"
                    ).ok:
                        raise BenchmarkFixtureError("held-out fixture verification failed")
        except LiveSQLBenchFixtureError as error:
            raise BenchmarkFixtureError("held-out fixture verification failed") from error
        return {
            **heldout_suite.public_summary(),
            "fixture": "ready",
        }

    def evaluate(
        self, suite: BenchmarkSuite, runner_output: RunnerOutput, image: str
    ) -> JsonObject:
        """Reject a public runner-output path before it can expose case identity."""

        del suite, runner_output, image
        raise BenchmarkAdapterError(
            "held-out suites require custody-only benchmark run execution"
        )

    def run_dsh(
        self,
        suite: BenchmarkSuite,
        dsh_config: DshConfig,
        profile_patch: Path | None,
        model: ModelConfig,
        runs_root: Path,
        candidate: ResolvedCandidate | None = None,
    ) -> BenchmarkRunResult:
        """Run only below custody; the ordinary workspace run root is intentionally ignored."""

        del runs_root
        try:
            outcome = run_heldout_dsh_suite(
                _suite(suite), dsh_config, profile_patch, model, candidate
            )
        except HeldOutLiveSQLBenchRunError as error:
            raise BenchmarkAdapterError(str(error)) from error
        return BenchmarkRunResult(
            run_id=outcome.run.run_id,
            status=outcome.attribution.status.value,
            attribution=outcome.attribution.kind.value,
            report_path=None,
            redacted_session_logs_path=None,
            exit_code=outcome.exit_code,
            public_result=outcome.public_result,
        )


def _suite(suite: BenchmarkSuite) -> HeldOutLiveSQLBenchSuite:
    if not isinstance(suite, HeldOutLiveSQLBenchSuite):
        raise BenchmarkAdapterError("held-out adapter received an incompatible suite")
    return suite
