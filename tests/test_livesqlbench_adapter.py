"""GT-safe behavior of the LiveSQLBench adapter facade."""

from __future__ import annotations

import json
from pathlib import Path

from skill_evolve.benchmark.livesqlbench_adapter import LiveSQLBenchBaseLiteAdapter
from skill_evolve.runner.contracts import RunnerCaseOutput, RunnerOutput
from skill_evolve.sql.postgres import QueryExecution, QueryResult
from tests.test_livesqlbench_import import _write_base_lite_fixture


class StubExecutor:
    def execute_read_only(self, query: str) -> QueryExecution:
        del query
        return QueryExecution(result=QueryResult(columns=("value",), rows=((1,),)))


class StubFixture:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def __enter__(self) -> StubFixture:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback

    def for_database(self, database_name: str) -> StubExecutor:
        assert database_name in {"alien", "polar", "gaming", "archeology", "cross_db"}
        return StubExecutor()


def test_adapter_evaluate_serialises_only_public_result_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_adapter.LiveSQLBenchFixture", StubFixture
    )
    adapter = LiveSQLBenchBaseLiteAdapter()
    suite = adapter.load_suite(_write_base_lite_fixture(tmp_path))
    output = RunnerOutput(
        schema_version="v1",
        cases=tuple(
            RunnerCaseOutput(
                case_id=case_id,
                status="completed",
                candidate_sql="SELECT 1",
                trajectory=(),
            )
            for case_id in suite.case_ids
        ),
    )

    result = adapter.evaluate(suite, output, "postgres:test")
    serialised = json.dumps(result)

    assert result["summary"] == {"pass": 5}
    assert "private gold" not in serialised
    assert "expected_query" not in serialised
    assert "expected_result" not in serialised
