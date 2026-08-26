"""LiveSQLBench automatic-run coverage without a real DSH invocation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from skill_evolve.benchmark.livesqlbench_run import run_base_lite_dsh
from skill_evolve.config.models import DshConfig, ModelConfig
from skill_evolve.runner.contracts import (
    RawArtifact,
    RunnerCaseOutput,
    RunnerExecution,
    RunnerOutput,
)
from skill_evolve.sql.postgres import QueryExecution, QueryResult
from tests.test_livesqlbench_import import _write_base_lite_fixture


@dataclass
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


class StubQueryService:
    def __init__(self, executor: StubExecutor) -> None:
        del executor

    def __enter__(self) -> StubQueryService:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:54321/query"


class StubDshRunner:
    def __init__(self, config: DshConfig, profile_patch: Path | None) -> None:
        del config, profile_patch

    def run(self, input) -> RunnerExecution:
        assert len(input.cases) == 1
        runner_case = input.cases[0]
        assert runner_case.expected_query == "SELECT 1"
        assert input.schema_file is not None
        assert input.schema_file.suffix == ".txt"
        context_paths = input.context_files_for(runner_case.id)
        assert len(context_paths) == 2
        assert all("gt" not in path.parts for path in context_paths)
        output_case = RunnerCaseOutput(
            case_id=runner_case.id,
            status="completed",
            candidate_sql="SELECT 1",
            trajectory=(),
        )
        output = RunnerOutput(schema_version="v1", cases=(output_case,))
        return RunnerExecution(
            output=output,
            raw_output=(output.model_dump_json() + "\n").encode(),
            raw_artifacts=(
                RawArtifact(
                    relative_path=f"dsh/{runner_case.id}.session.jsonl",
                    content=b'{"type":"session","value":"redacted"}\n',
                ),
            ),
        )


def test_automatic_run_writes_redacted_session_evidence_without_gt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.LiveSQLBenchFixture", StubFixture)
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.ReadOnlyQueryService", StubQueryService
    )
    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.DshRunner", StubDshRunner)
    data_root = _write_base_lite_fixture(tmp_path / "data")
    dsh_config = DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh")
    model = ModelConfig(provider="test", name="stub")

    outcome = run_base_lite_dsh(data_root, dsh_config, None, model, tmp_path / "runs")

    assert outcome.attribution.status.value == "completed"
    assert len(outcome.evaluations) == 5
    raw_dsh = outcome.run.path / "raw" / "dsh"
    assert len(tuple(raw_dsh.glob("*.session.jsonl"))) == 5
    evaluation_json = (outcome.run.path / "evaluation.json").read_text(encoding="utf-8")
    runner_json = (outcome.run.path / "raw" / "runner-output.json").read_text(encoding="utf-8")
    assert "private gold" not in evaluation_json
    assert "private gold" not in runner_json
    assert "expected_query" not in evaluation_json
    result_json = json.loads((outcome.run.path / "result.json").read_text(encoding="utf-8"))
    assert result_json["summary"] == {"pass": 5}
