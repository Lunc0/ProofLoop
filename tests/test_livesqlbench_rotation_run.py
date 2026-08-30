"""Stub-only rolling execution, neutral status, gate, and comparison coverage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from skill_evolve.benchmark.livesqlbench_heldout import (
    HELDOUT_ROOT_ENVIRONMENT_VARIABLE,
    initialise_heldout_manifest,
)
from skill_evolve.benchmark.livesqlbench_rotation import (
    ROTATION_ROOT_ENVIRONMENT_VARIABLE,
    create_rotation_manifest,
    initialise_rotation_reserve,
    load_rotation_suite,
)
from skill_evolve.benchmark.livesqlbench_rotation_run import (
    RotationRunError,
    compare_evolution_results,
    has_successful_smoke_gate,
    run_rotation_dsh_suite,
)
from skill_evolve.config.models import DshConfig, ModelConfig
from skill_evolve.runner.contracts import (
    AgentExecution,
    RawArtifact,
    RunnerCaseOutput,
    RunnerExecution,
    RunnerOutput,
)
from skill_evolve.sql.postgres import QueryExecution, QueryResult
from skill_evolve.subject import RunSubject
from tests.test_livesqlbench_heldout import _write_final_suite_fixture


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
        assert database_name.startswith("db")
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


class PassingStubDshRunner:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def run(self, input) -> RunnerExecution:
        case = input.cases[0]
        assert input.subject is not None
        output_case = RunnerCaseOutput(
            case_id=case.id,
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
                    relative_path=f"dsh/{case.id}.session.jsonl",
                    content=_session_jsonl(case.id),
                ),
            ),
        )


class TaskFailureStubDshRunner(PassingStubDshRunner):
    def run(self, input) -> RunnerExecution:
        case = input.cases[0]
        output_case = RunnerCaseOutput(
            case_id=case.id,
            status="completed",
            candidate_sql=None,
            trajectory=(),
        )
        output = RunnerOutput(schema_version="v1", cases=(output_case,))
        return RunnerExecution(
            output=output,
            raw_output=(output.model_dump_json() + "\n").encode(),
            raw_artifacts=(
                RawArtifact(
                    relative_path=f"dsh/{case.id}.session.jsonl",
                    content=_session_jsonl(case.id),
                ),
            ),
        )


class TimeoutStubDshRunner(PassingStubDshRunner):
    def run(self, input) -> RunnerExecution:
        case = input.cases[0]
        output_case = RunnerCaseOutput(
            case_id=case.id,
            status="runner_error",
            candidate_sql=None,
            trajectory=(),
            agent_execution=AgentExecution(status="error", error="DSH timed out after 300000 ms"),
        )
        output = RunnerOutput(schema_version="v1", cases=(output_case,))
        return RunnerExecution(
            output=output,
            raw_output=(output.model_dump_json() + "\n").encode(),
            raw_artifacts=(
                RawArtifact(
                    relative_path=f"dsh/{case.id}.session.jsonl",
                    content=_partial_session_jsonl(case.id),
                ),
            ),
        )


def test_smoke_gate_controls_evolution_and_metrics_are_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke_suite, evolution_suite = _prepare_suites(tmp_path, monkeypatch)
    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.LiveSQLBenchFixture", StubFixture)
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.ReadOnlyQueryService", StubQueryService
    )
    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.DshRunner", PassingStubDshRunner)
    subject = _subject("first")
    dsh_config = DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh")
    model = ModelConfig(provider="test", name="stub")

    smoke = run_rotation_dsh_suite(
        smoke_suite, dsh_config, None, model, tmp_path / "runs", subject
    )
    assert smoke.status == "passed"
    assert has_successful_smoke_gate(smoke_suite.custody, subject)
    assert "next_action" not in smoke.public_result
    assert smoke.public_result["tiers"] == {
        "Simple": {"case_count": 2, "evaluated_count": 2, "passed_count": 2, "pass_rate": 1.0},
        "Moderate": {"case_count": 2, "evaluated_count": 2, "passed_count": 2, "pass_rate": 1.0},
        "Challenging": {"case_count": 1, "evaluated_count": 1, "passed_count": 1, "pass_rate": 1.0},
    }

    evolution = run_rotation_dsh_suite(
        evolution_suite, dsh_config, None, model, tmp_path / "runs", subject
    )
    assert evolution.status == "passed"
    assert evolution.public_result["macro_average"] == 1.0
    assert evolution.comparison_key is not None
    assert compare_evolution_results(evolution, evolution) == "comparable"
    agent_input = evolution.run.path / "agent-input" / "trajectory.json"
    assert agent_input.is_file()
    assert not (smoke.run.path / "agent-input").exists()
    agent_input_text = agent_input.read_text(encoding="utf-8")
    assert "expected_query" not in agent_input_text
    assert "Markdown fenced code block" in agent_input_text

    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", TaskFailureStubDshRunner
    )
    task_failure = run_rotation_dsh_suite(
        evolution_suite, dsh_config, None, model, tmp_path / "runs", subject
    )
    task_failure_input = (task_failure.run.path / "agent-input" / "trajectory.json").read_text(
        encoding="utf-8"
    )
    assert task_failure.status == "task_failure"
    assert '"verdict":"no_sql_generated"' in task_failure_input

    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", TimeoutStubDshRunner
    )
    timeout = run_rotation_dsh_suite(
        evolution_suite, dsh_config, None, model, tmp_path / "runs", subject
    )
    timeout_input = (timeout.run.path / "agent-input" / "trajectory.json").read_text(
        encoding="utf-8"
    )
    assert timeout.status == "runner_invalid"
    assert '"trace_completeness":"partial_actionable"' in timeout_input
    assert '"runner_outcome":"timeout"' in timeout_input

    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", TaskFailureStubDshRunner
    )
    failing_subject = _subject("second")
    failed_smoke = run_rotation_dsh_suite(
        smoke_suite, dsh_config, None, model, tmp_path / "runs", failing_subject
    )
    assert failed_smoke.status == "task_failure"
    assert not has_successful_smoke_gate(smoke_suite.custody, failing_subject)
    with pytest.raises(RotationRunError, match="successful Smoke gate"):
        run_rotation_dsh_suite(
            evolution_suite,
            dsh_config,
            None,
            model,
            tmp_path / "runs",
            failing_subject,
        )


def _prepare_suites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, object]:
    data_root = _write_final_suite_fixture(tmp_path / "data", database_count=24)
    heldout_root = tmp_path / "heldout"
    rotation_root = tmp_path / "rotation"
    heldout_root.mkdir()
    rotation_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(heldout_root))
    monkeypatch.setenv(ROTATION_ROOT_ENVIRONMENT_VARIABLE, str(rotation_root))
    common = {
        "repository_root": Path.cwd(),
        "candidate_store_root": Path.cwd() / ".local" / "skill-evolve",
    }
    initialise_heldout_manifest(data_root, **common)
    initialise_rotation_reserve(data_root, **common)
    smoke = create_rotation_manifest(data_root, "smoke", **common)
    evolution = create_rotation_manifest(data_root, "evolution", **common)
    return (
        load_rotation_suite(data_root, smoke.manifest_id, **common),
        load_rotation_suite(data_root, evolution.manifest_id, **common),
    )


def _subject(content: str) -> RunSubject:
    content_bytes = content.encode("utf-8")
    return RunSubject(
        subject_id=f"subject-{content}",
        content_sha256=hashlib.sha256(content_bytes).hexdigest(),
        content=content_bytes,
    )


def _session_jsonl(case_id: str) -> bytes:
    events = (
        {"type": "session", "version": 0, "id": f"session-{case_id}", "createdAt": 1},
        {
            "type": "assistant/message",
            "seq": 1,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
        },
        {"type": "turn/end", "seq": 2, "data": {"turn": 1, "reason": {"kind": "completed"}}},
    )
    return b"".join(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n" for event in events)


def _partial_session_jsonl(case_id: str) -> bytes:
    events = (
        {"type": "session", "version": 0, "id": f"session-{case_id}", "createdAt": 1},
        {
            "type": "tool/call",
            "seq": 1,
            "data": {
                "turn": 1,
                "step": 1,
                "callId": "call-1",
                "name": "read",
                "arguments": '{"path":"schema.sql"}',
            },
        },
    )
    return b"".join(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n" for event in events)
