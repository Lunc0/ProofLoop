"""Benchmark-specific adapters that preserve the core runner boundary."""

from skill_evolve.benchmark.contracts import (
    BenchmarkAdapter,
    BenchmarkAdapterError,
    BenchmarkFixtureError,
    BenchmarkRunResult,
    BenchmarkSuite,
    JsonObject,
    JsonValue,
)
from skill_evolve.benchmark.livesqlbench import (
    APPROVED_BASE_LITE_QUERY_IDS,
    LiveSQLBenchCase,
    LiveSQLBenchDataError,
    LiveSQLBenchSuite,
    evaluate_query_case,
    load_base_lite_suite,
    public_evaluation_dict,
    public_summary,
)
from skill_evolve.benchmark.livesqlbench_run import (
    LiveSQLBenchRunError,
    LiveSQLBenchRunOutcome,
    run_base_lite_dsh,
    run_base_lite_dsh_suite,
)
from skill_evolve.benchmark.registry import get_adapter, registered_adapter_ids

__all__ = [
    "APPROVED_BASE_LITE_QUERY_IDS",
    "BenchmarkAdapter",
    "BenchmarkAdapterError",
    "BenchmarkFixtureError",
    "BenchmarkRunResult",
    "BenchmarkSuite",
    "JsonObject",
    "JsonValue",
    "LiveSQLBenchCase",
    "LiveSQLBenchDataError",
    "LiveSQLBenchSuite",
    "LiveSQLBenchRunError",
    "LiveSQLBenchRunOutcome",
    "evaluate_query_case",
    "get_adapter",
    "load_base_lite_suite",
    "public_evaluation_dict",
    "public_summary",
    "registered_adapter_ids",
    "run_base_lite_dsh",
    "run_base_lite_dsh_suite",
]
