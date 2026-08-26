"""Public benchmark CLI compatibility and safe registry lookup coverage."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from skill_evolve.cli import app
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


def test_benchmark_validate_matches_livesqlbench_compatibility_entrypoint(
    tmp_path: Path,
) -> None:
    data_root = _write_base_lite_fixture(tmp_path)
    runner = CliRunner()

    generic = runner.invoke(
        app,
        ["benchmark", "validate", "livesqlbench-base-lite", str(data_root)],
    )
    compatibility = runner.invoke(app, ["livesqlbench", "validate", str(data_root)])

    assert generic.exit_code == 0
    assert compatibility.exit_code == 0
    assert json.loads(generic.stdout) == json.loads(compatibility.stdout)
    assert "private gold" not in generic.stdout
    assert "expected_query" not in generic.stdout


def test_benchmark_rejects_unknown_adapter_with_usage_exit_code(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["benchmark", "validate", "unknown", str(tmp_path)])

    assert result.exit_code == 64
    assert "unknown benchmark adapter" in result.stderr


def test_benchmark_help_lists_the_four_public_commands() -> None:
    result = CliRunner().invoke(app, ["benchmark", "--help"])

    assert result.exit_code == 0
    for command in ("validate", "fixture-check", "evaluate", "run"):
        assert command in result.stdout


def test_benchmark_fixture_and_evaluate_match_compatibility_entrypoints(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_adapter.LiveSQLBenchFixture", StubFixture
    )
    data_root = _write_base_lite_fixture(tmp_path / "data")
    output_path = tmp_path / "runner-output.json"
    output_path.write_text(
        RunnerOutput(
            schema_version="v1",
            cases=tuple(
                RunnerCaseOutput(
                    case_id=case_id,
                    status="completed",
                    candidate_sql="SELECT 1",
                    trajectory=(),
                )
                for case_id in ("alien_2", "polar_10", "gaming_1", "archeology_5", "cross_db_8")
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    runner = CliRunner()

    generic_fixture = runner.invoke(
        app,
        ["benchmark", "fixture-check", "livesqlbench-base-lite", str(data_root)],
    )
    compatibility_fixture = runner.invoke(app, ["livesqlbench", "fixture-check", str(data_root)])
    generic_evaluate = runner.invoke(
        app,
        [
            "benchmark",
            "evaluate",
            "livesqlbench-base-lite",
            str(data_root),
            str(output_path),
        ],
    )
    compatibility_evaluate = runner.invoke(
        app,
        ["livesqlbench", "evaluate", str(data_root), str(output_path)],
    )

    assert generic_fixture.exit_code == compatibility_fixture.exit_code == 0
    assert json.loads(generic_fixture.stdout) == json.loads(compatibility_fixture.stdout)
    assert generic_evaluate.exit_code == compatibility_evaluate.exit_code == 0
    assert json.loads(generic_evaluate.stdout) == json.loads(compatibility_evaluate.stdout)
    assert "private gold" not in generic_evaluate.stdout
