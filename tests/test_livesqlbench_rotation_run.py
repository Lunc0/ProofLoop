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
from skill_evolve.config.models import DshConfig, ModelConfig, OptimizerConfig
from skill_evolve.optimizer.client import OptimizerClientResponse
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


class AdviceFakeClient:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.calls: list[tuple[str, str]] = []

    def advise(
        self, system_prompt: str, user_prompt: str, config: OptimizerConfig
    ) -> OptimizerClientResponse:
        del config
        self.calls.append((system_prompt, user_prompt))
        return OptimizerClientResponse(
            json.dumps(
                {
                    "schema_version": "skill-optimization-advice-v1",
                    "summary": "A failed task has bounded evidence.",
                    "suggestions": [
                        {
                            "evidence_case_ids": [self.case_id],
                            "observation": "No SQL was submitted.",
                            "suggested_change": "Add a submission check.",
                            "suggested_markdown_fragment": (
                                "- Confirm a single SQL statement is submitted."
                            ),
                        }
                    ],
                }
            )
        )


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


class PartialSmokeStubDshRunner(PassingStubDshRunner):
    passed_case_count = 4
    call_count = 0

    def run(self, input) -> RunnerExecution:
        type(self).call_count += 1
        if type(self).call_count <= type(self).passed_case_count:
            return super().run(input)
        return TaskFailureStubDshRunner().run(input)


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
        "Simple": {"case_count": 3, "evaluated_count": 3, "passed_count": 3, "pass_rate": 1.0},
        "Moderate": {"case_count": 2, "evaluated_count": 2, "passed_count": 2, "pass_rate": 1.0},
        "Challenging": {
            "case_count": 0,
            "evaluated_count": 0,
            "passed_count": 0,
            "pass_rate": None,
        },
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
    passing_input = json.loads(agent_input.read_text(encoding="utf-8"))
    assert passing_input["schema_version"] == "evolution-coding-agent-input-v3"
    assert passing_input["trajectories"] == []
    assert "omitted_cases" not in passing_input

    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", TaskFailureStubDshRunner
    )
    task_failure = run_rotation_dsh_suite(
        evolution_suite, dsh_config, None, model, tmp_path / "runs", subject
    )
    task_failure_input = json.loads(
        (task_failure.run.path / "agent-input" / "trajectory.json").read_text(encoding="utf-8")
    )
    assert task_failure.status == "task_failure"
    assert len(task_failure_input["trajectories"]) == len(evolution_suite.cases)
    assert all(
        case["evaluation"]["verdict"] == "no_sql_generated"
        for case in task_failure_input["trajectories"]
    )

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


def test_smoke_gate_requires_v2_evidence_and_at_least_three_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke_suite, evolution_suite = _prepare_suites(tmp_path, monkeypatch)
    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.LiveSQLBenchFixture", StubFixture)
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.ReadOnlyQueryService", StubQueryService
    )
    dsh_config = DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh")
    model = ModelConfig(provider="test", name="stub")

    PartialSmokeStubDshRunner.call_count = 0
    PartialSmokeStubDshRunner.passed_case_count = 3
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", PartialSmokeStubDshRunner
    )
    three_of_five_subject = _subject("three-of-five")
    three_of_five = run_rotation_dsh_suite(
        smoke_suite, dsh_config, None, model, tmp_path / "runs", three_of_five_subject
    )
    assert three_of_five.status == "task_failure"
    assert has_successful_smoke_gate(smoke_suite.custody, three_of_five_subject)
    receipts = list(smoke_suite.custody.gate_root.glob("*.json"))
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "v2"
    assert receipt["passed_case_count"] == 3
    assert receipt["case_count"] == 5
    assert receipt["minimum_passed_case_count"] == 3

    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.DshRunner", PassingStubDshRunner)
    assert (
        run_rotation_dsh_suite(
            evolution_suite, dsh_config, None, model, tmp_path / "runs", three_of_five_subject
        ).status
        == "passed"
    )

    PartialSmokeStubDshRunner.call_count = 0
    PartialSmokeStubDshRunner.passed_case_count = 2
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", PartialSmokeStubDshRunner
    )
    two_of_five_subject = _subject("two-of-five")
    two_of_five = run_rotation_dsh_suite(
        smoke_suite, dsh_config, None, model, tmp_path / "runs", two_of_five_subject
    )
    assert two_of_five.status == "task_failure"
    assert not has_successful_smoke_gate(smoke_suite.custody, two_of_five_subject)
    with pytest.raises(RotationRunError, match="successful Smoke gate"):
        run_rotation_dsh_suite(
            evolution_suite, dsh_config, None, model, tmp_path / "runs", two_of_five_subject
        )

    legacy_subject = _subject("legacy")
    legacy_payload = {
        "schema_version": "v1",
        "subject": legacy_subject.public_binding(),
        "smoke_manifest_sha256": "a" * 64,
        "smoke_environment_fingerprint": "b" * 64,
    }
    legacy_payload["gate_sha256"] = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    smoke_suite.custody.gate_root.mkdir(exist_ok=True)
    (smoke_suite.custody.gate_root / "legacy.json").write_text(
        json.dumps(legacy_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert not has_successful_smoke_gate(smoke_suite.custody, legacy_subject)

    historical_v2_subject = _subject("historical-v2")
    historical_v2_payload = {
        "schema_version": "v2",
        "subject": historical_v2_subject.public_binding(),
        "smoke_manifest_sha256": "c" * 64,
        "smoke_environment_fingerprint": "d" * 64,
        "passed_case_count": 4,
        "case_count": 5,
        "minimum_passed_case_count": 4,
    }
    historical_v2_payload["gate_sha256"] = hashlib.sha256(
        json.dumps(historical_v2_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (smoke_suite.custody.gate_root / "historical-v2.json").write_text(
        json.dumps(historical_v2_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert has_successful_smoke_gate(smoke_suite.custody, historical_v2_subject)

    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.DshRunner", TimeoutStubDshRunner)
    invalid_subject = _subject("invalid")
    invalid = run_rotation_dsh_suite(
        smoke_suite, dsh_config, None, model, tmp_path / "runs", invalid_subject
    )
    assert invalid.status == "runner_invalid"
    assert not has_successful_smoke_gate(smoke_suite.custody, invalid_subject)


def test_enabled_optimizer_runs_after_evolution_failure_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke_suite, evolution_suite = _prepare_suites(tmp_path, monkeypatch)
    monkeypatch.setattr("skill_evolve.benchmark.livesqlbench_run.LiveSQLBenchFixture", StubFixture)
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.ReadOnlyQueryService", StubQueryService
    )
    subject = _subject("optimizer")
    dsh_config = DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh")
    model = ModelConfig(provider="test", name="stub")
    optimizer = OptimizerConfig(enabled=True)
    fake = AdviceFakeClient(evolution_suite.cases[0].instance_id)

    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", PassingStubDshRunner
    )
    smoke = run_rotation_dsh_suite(
        smoke_suite,
        dsh_config,
        None,
        model,
        tmp_path / "runs",
        subject,
        optimizer,
        optimizer_client=fake,
    )
    assert smoke.status == "passed"
    assert fake.calls == []
    assert "optimizer" not in smoke.public_result

    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_run.DshRunner", TaskFailureStubDshRunner
    )
    evolution = run_rotation_dsh_suite(
        evolution_suite,
        dsh_config,
        None,
        model,
        tmp_path / "runs",
        subject,
        optimizer,
        optimizer_client=fake,
    )

    assert evolution.status == "task_failure"
    assert len(fake.calls) == 1
    assert evolution.public_result["optimizer"] == {
        "status": "proposed",
        "advice_path": "optimizer/advice.json",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "prompt_version": "skill-optimization-advice-prompt-v1",
    }
    assert (evolution.run.path / "optimizer" / "advice.json").is_file()
    assert (evolution.run.path / "optimizer" / "receipt.json").is_file()


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
