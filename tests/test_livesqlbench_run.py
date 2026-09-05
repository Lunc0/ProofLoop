"""LiveSQLBench automatic-run coverage without a real DSH invocation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from skill_evolve.benchmark.livesqlbench_run import run_base_lite_dsh
from skill_evolve.candidate import CandidateService, LocalCandidateStore, subject_from_candidate
from skill_evolve.config.models import DshConfig, ModelConfig
from skill_evolve.runner.contracts import (
    RawArtifact,
    RunnerCaseOutput,
    RunnerError,
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
    def __init__(
        self, config: DshConfig, profile_patch: Path | None, model: ModelConfig
    ) -> None:
        del config, profile_patch, model

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


class FirstRunnerErrorDshRunner(StubDshRunner):
    call_count = 0

    def run(self, input) -> RunnerExecution:
        type(self).call_count += 1
        if type(self).call_count == 1:
            raise RunnerError("private runner failure")
        return super().run(input)


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


def test_automatic_run_binds_verified_candidate_without_notices_or_gt(
    tmp_path, monkeypatch
) -> None:
    captured_subject_ids: list[str] = []

    class CandidateStubDshRunner(StubDshRunner):
        def run(self, input) -> RunnerExecution:
            assert input.subject is not None
            assert input.subject_ref == input.subject.subject_id
            assert input.subject.content == b"# Candidate\n"
            assert all(
                "gt" not in path.parts for path in input.context_files_for(input.cases[0].id)
            )
            captured_subject_ids.append(input.subject.subject_id)
            return super().run(input)

    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.LiveSQLBenchFixture", StubFixture)
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.ReadOnlyQueryService", StubQueryService
    )
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", CandidateStubDshRunner
    )
    skill = tmp_path / "incoming" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_bytes(b"# Candidate\n")
    candidate_service = CandidateService(
        LocalCandidateStore(tmp_path / ".local" / "skill-evolve")
    )
    candidate = candidate_service.initialise(
        "dbhub", skill, candidate_name="000-upstream-baseline"
    )
    data_root = _write_base_lite_fixture(tmp_path / "data")
    outcome = run_base_lite_dsh(
        data_root,
        DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh"),
        None,
        ModelConfig(provider="test", name="stub"),
        tmp_path / "runs",
        subject_from_candidate(candidate),
    )

    environment = json.loads((outcome.run.path / "environment.json").read_text(encoding="utf-8"))
    result = json.loads((outcome.run.path / "result.json").read_text(encoding="utf-8"))
    subject = subject_from_candidate(candidate)
    assert captured_subject_ids == [subject.subject_id] * 5
    assert environment["subject"] == subject.public_binding()
    assert result["subject"] == subject.public_binding()
    subject_input = outcome.run.path / "inputs" / "subject-SKILL.md"
    assert subject_input.read_bytes() == candidate.skill_bytes
    assert not (outcome.run.path / "inputs" / "NOTICE.md").exists()
    assert "private gold" not in (outcome.run.path / "result.json").read_text(encoding="utf-8")


def test_runner_exception_is_counted_as_wrong_and_does_not_stop_base_lite(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.LiveSQLBenchFixture", StubFixture)
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.ReadOnlyQueryService", StubQueryService
    )
    FirstRunnerErrorDshRunner.call_count = 0
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", FirstRunnerErrorDshRunner
    )

    outcome = run_base_lite_dsh(
        _write_base_lite_fixture(tmp_path / "data"),
        DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh"),
        None,
        ModelConfig(provider="test", name="stub"),
        tmp_path / "runs",
    )

    assert FirstRunnerErrorDshRunner.call_count == 5
    assert outcome.attribution.status.value == "needs_skill_change"
    assert len(outcome.evaluations) == 5
    assert outcome.evaluations[0].verdict.value == "runner_error"
    assert outcome.evaluations[0].candidate_sql is None
    assert (outcome.run.path / "raw" / "runner-output.json").is_file()
    result = json.loads((outcome.run.path / "result.json").read_text(encoding="utf-8"))
    assert result["summary"] == {"pass": 4, "runner_error": 1}
    assert "private runner failure" not in json.dumps(result)
