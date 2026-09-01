"""Offline service tests for bounded Evolution optimization advice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skill_evolve.config.models import OptimizerConfig
from skill_evolve.optimizer.client import OptimizerClientResponse
from skill_evolve.optimizer.service import advise_after_evolution
from skill_evolve.registry.store import RunDirectory, RunStore
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import canonical_json_bytes
from skill_evolve.trajectory.contracts import (
    EvaluatedSkill,
    EvolutionAgentInputV3,
    EvolutionProjectionCaseV3,
    ProjectionCandidateOutput,
    ProjectionEvaluation,
    ProjectionEvidence,
)


class FakeOptimizerClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def advise(
        self, system_prompt: str, user_prompt: str, config: OptimizerConfig
    ) -> OptimizerClientResponse:
        del config
        self.calls.append((system_prompt, user_prompt))
        return OptimizerClientResponse(self.content)


def test_valid_evolution_input_writes_advice_without_raw_session(tmp_path: Path) -> None:
    run = RunStore(tmp_path / "runs").create()
    subject = _subject()
    _write_input(run, subject)
    raw_session = run.path / "raw" / "dsh" / "case-1.session.jsonl"
    raw_session.parent.mkdir(parents=True)
    raw_session.write_text("not-for-optimizer", encoding="utf-8")
    client = FakeOptimizerClient(_model_response("case-1"))

    result = advise_after_evolution(
        run,
        subject,
        OptimizerConfig(enabled=True),
        run_status="task_failure",
        client=client,
    )

    assert result.receipt.status == "proposed"
    assert result.advice_path == "optimizer/advice.json"
    assert len(client.calls) == 1
    assert "not-for-optimizer" not in client.calls[0][1]
    advice = json.loads((run.path / "optimizer" / "advice.json").read_text(encoding="utf-8"))
    assert advice["source"]["evaluated_skill_sha256"] == subject.content_sha256
    assert advice["suggestions"][0]["evidence_case_ids"] == ["case-1"]
    receipt = json.loads((run.path / "optimizer" / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "proposed"


def test_empty_trajectory_skips_without_calling_client(tmp_path: Path) -> None:
    run = RunStore(tmp_path / "runs").create()
    subject = _subject()
    _write_input(run, subject, trajectories=())
    client = FakeOptimizerClient(_model_response("case-1"))

    result = advise_after_evolution(
        run,
        subject,
        OptimizerConfig(enabled=True),
        run_status="passed",
        client=client,
    )

    assert result.receipt.status == "skipped_no_failed_trajectories"
    assert client.calls == []
    assert (run.path / "optimizer" / "receipt.json").is_file()


def test_unknown_evidence_case_in_model_advice_is_rejected(tmp_path: Path) -> None:
    run = RunStore(tmp_path / "runs").create()
    subject = _subject()
    _write_input(run, subject)
    client = FakeOptimizerClient(_model_response("other-case"))

    result = advise_after_evolution(
        run,
        subject,
        OptimizerConfig(enabled=True),
        run_status="task_failure",
        client=client,
    )

    assert result.receipt.status == "invalid_response"
    assert not (run.path / "optimizer" / "advice.json").exists()


def test_tampered_agent_input_skips_without_calling_client(tmp_path: Path) -> None:
    run = RunStore(tmp_path / "runs").create()
    subject = _subject()
    _write_input(run, subject)
    payload_path = run.path / "agent-input" / "trajectory.json"
    payload_path.write_text('{"tampered":true}\n', encoding="utf-8")
    client = FakeOptimizerClient(_model_response("case-1"))

    result = advise_after_evolution(
        run,
        subject,
        OptimizerConfig(enabled=True),
        run_status="task_failure",
        client=client,
    )

    assert result.receipt.status == "skipped_invalid_evidence"
    assert client.calls == []


def _subject() -> RunSubject:
    content = b"# SQL Skill\n\n- Inspect schema before SQL.\n"
    return RunSubject(
        subject_id="candidate-1",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _write_input(
    run: RunDirectory,
    subject: RunSubject,
    *,
    trajectories: tuple[EvolutionProjectionCaseV3, ...] | None = None,
) -> None:
    input_trajectories = trajectories if trajectories is not None else (_failed_case(),)
    payload = EvolutionAgentInputV3(
        projection_rule_version="evolution-projection-v3.1",
        environment_fingerprint="e" * 64,
        evaluated_skill=EvaluatedSkill(
            subject_id=subject.subject_id,
            content_sha256=subject.content_sha256,
        ),
        skill=subject.content.decode("utf-8"),
        trajectories=input_trajectories,
    )
    payload_bytes = canonical_json_bytes(payload.model_dump(mode="json"))
    run.write_bytes("agent-input/trajectory.json", payload_bytes + b"\n")
    run.write_json(
        "agent-input/manifest.json",
        {
            "schema_version": "evolution-coding-agent-input-v3",
            "exposed_for_optimization": True,
            "manifest_sha256": "a" * 64,
            "environment_fingerprint": "e" * 64,
            "projection_rule_version": payload.projection_rule_version,
            "agent_input_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "included_case_count": len(payload.trajectories),
            "omitted_case_count": 0,
        },
    )


def _failed_case() -> EvolutionProjectionCaseV3:
    return EvolutionProjectionCaseV3(
        case_id="case-1",
        prompt="Count customers.",
        task_contract="Submit SQL.",
        difficulty_tier="Simple",
        context=(),
        evidence=(ProjectionEvidence(kind="tool_failure", priority="P0", outcome="failed"),),
        candidate_output=ProjectionCandidateOutput(sql=None),
        evaluation=ProjectionEvaluation(
            slice="evolution",
            verdict="no_sql_generated",
            usage={},
        ),
    )


def _model_response(case_id: str) -> str:
    return json.dumps(
        {
            "schema_version": "skill-optimization-advice-v1",
            "summary": "One failed task has bounded evidence.",
            "suggestions": [
                {
                    "evidence_case_ids": [case_id],
                    "observation": "No SQL was submitted.",
                    "suggested_change": "Add a submission verification rule.",
                    "suggested_markdown_fragment": "- Verify one SQL statement is submitted.",
                }
            ],
        }
    )
