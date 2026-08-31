"""Deterministic, rule-based DSH trajectory projection coverage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from skill_evolve.benchmark.livesqlbench import LiveSQLBenchCase
from skill_evolve.evaluator.core import CaseEvaluation, Verdict
from skill_evolve.runner.contracts import AgentExecution, RunnerCaseOutput
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.agent_input import build_evolution_agent_input
from skill_evolve.trajectory.compressor import (
    TrajectoryCompressionError,
    canonical_json_bytes,
    compress_session_jsonl,
)
from skill_evolve.trajectory.policy import CompressorPolicy


def test_complete_trajectory_pairs_cross_step_result_and_strips_reasoning() -> None:
    session = _jsonl(
        _header(),
        _assistant(
            1,
            1,
            1,
            [
                {"type": "reasoning", "text": "private chain of thought"},
                {"type": "text", "text": "Inspect schema at D:/custody/schema.sql token=secret"},
            ],
        ),
        _tool_call(2, 1, 1, "call-1", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
        _tool_result(
            3,
            1,
            2,
            "call-1",
            "multiple SQL statements are not allowed [exit code: 1]",
            is_error=False,
        ),
        _assistant(4, 1, 3, [{"type": "text", "text": "```sql\nSELECT 1\n```"}]),
        _turn_end(5, 1),
    )

    first = compress_session_jsonl(
        session,
        _runner_case(),
        _evaluation(),
        runner_outcome="completed",
    )
    second = compress_session_jsonl(
        session,
        _runner_case(),
        _evaluation(),
        runner_outcome="completed",
    )

    assert canonical_json_bytes(first.model_dump(mode="json")) == canonical_json_bytes(
        second.model_dump(mode="json")
    )
    assert first.trace.trace_completeness == "complete"
    call = first.trajectory[0].tool_calls[0]
    assert call.result is not None
    assert call.result.step == 2
    assert call.status == "failed"
    assert call.direct_failure is not None
    assert call.direct_failure.direct_cause == "multiple_sql_statements"
    output = canonical_json_bytes(first.model_dump(mode="json")).decode("utf-8")
    assert "private chain of thought" not in output
    assert "D:/custody" not in output
    assert "secret" not in output
    assert "PRIVATE GOLD" not in output


def test_partial_timeout_is_actionable_when_actions_exist() -> None:
    session = _jsonl(
        _header(),
        _tool_call(1, 1, 1, "call-1", "read", '{"path":"schema.sql"}'),
    )
    runner_case = RunnerCaseOutput(
        case_id="case-1",
        status="runner_error",
        final_message=None,
        candidate_sql=None,
        trajectory=(),
        agent_execution=AgentExecution(status="error", error="DSH timed out after 300000 ms"),
    )

    compressed = compress_session_jsonl(
        session,
        runner_case,
        _evaluation(Verdict.RUNNER_ERROR),
        runner_outcome="timeout",
        deadline_ms=300_000,
    )

    assert compressed.trace.trace_completeness == "partial_actionable"
    assert compressed.trace.missing_terminal_events == ("turn/end", "final_assistant_text")
    assert compressed.trace.deadline_ms == 300_000
    assert compressed.compression.missing_result_count == 1


def test_startup_only_timeout_is_partial_insufficient() -> None:
    compressed = compress_session_jsonl(
        _jsonl(_header(), {"type": "step/start", "seq": 1, "data": {"turn": 1, "step": 1}}),
        RunnerCaseOutput(case_id="case-1", status="runner_error", trajectory=()),
        _evaluation(Verdict.RUNNER_ERROR),
        runner_outcome="timeout",
        deadline_ms=300_000,
    )

    assert compressed.trace.trace_completeness == "partial_insufficient"
    assert compressed.trajectory == ()


def test_web_search_is_allowed_and_has_deterministic_summary() -> None:
    session = _jsonl(
        _header(),
        _tool_call(1, 1, 1, "call-web", "web_search", '{"queries":["PostgreSQL join"]}'),
        _tool_result(
            2,
            1,
            1,
            "call-web",
            "result",
            is_error=False,
            meta={
                "sources": [
                    {"title": "B", "url": "https://b.example", "snippet": "second"},
                    {"title": "A", "url": "https://a.example", "snippet": "first"},
                ]
            },
        ),
        _assistant(3, 1, 2, [{"type": "text", "text": "done"}]),
        _turn_end(4, 1),
    )

    compressed = compress_session_jsonl(
        session, _runner_case(), _evaluation(), runner_outcome="completed"
    )

    call = compressed.trajectory[0].tool_calls[0]
    assert call.adapter == "web_search"
    assert call.status == "succeeded"
    assert call.summary["untrusted_evidence"] is True
    assert [query.text for query in call.summary["queries"]] == ["PostgreSQL join"]
    assert call.summary["retrieved_at_ms"] == 2
    assert [
        (source["title"].text, source["url"].text, source["snippet"].text)
        for source in call.summary["sources"]
    ] == [
        ("A", "https://a.example", "first"),
        ("B", "https://b.example", "second"),
    ]


def test_file_path_summary_prevents_distinct_reads_being_deduplicated() -> None:
    session = _jsonl(
        _header(),
        _tool_call(
            1,
            1,
            1,
            "read-schema",
            "read",
            '{"file_path":"D:\\\\custody\\\\agent-workspace\\\\case-1\\\\schema.sql"}',
        ),
        _tool_call(
            2,
            1,
            2,
            "read-task",
            "read",
            '{"file_path":"D:\\\\custody\\\\agent-workspace\\\\case-1\\\\TASK.md"}',
        ),
        _assistant(3, 1, 3, [{"type": "text", "text": "done"}]),
        _turn_end(4, 1),
    )

    compressed = compress_session_jsonl(
        session, _runner_case(), _evaluation(), runner_outcome="completed"
    )

    calls = [call for step in compressed.trajectory for call in step.tool_calls]
    assert [call.summary["target"].text for call in calls] == [
        "workspace/case-1/schema.sql",
        "workspace/case-1/TASK.md",
    ]
    assert all(call.summary["untrusted_evidence"] is True for call in calls)
    assert not any(signal.kind == "repeated_exact_tool_call" for signal in compressed.signals)


def test_post_failure_signal_excludes_successes_before_the_failure() -> None:
    session = _jsonl(
        _header(),
        _tool_call(1, 1, 1, "before", "pwsh", '{"command":"Get-ChildItem"}'),
        _tool_result(2, 1, 1, "before", "ok", is_error=False),
        _tool_call(3, 1, 2, "failed", "pwsh", '{"command":"Get-ChildItem"}'),
        _tool_result(4, 1, 2, "failed", "failed", is_error=True),
        _tool_call(5, 1, 3, "after", "pwsh", '{"command":"Get-ChildItem"}'),
        _tool_result(6, 1, 3, "after", "ok", is_error=False),
        _assistant(7, 1, 4, [{"type": "text", "text": "done"}]),
        _turn_end(8, 1),
    )

    compressed = compress_session_jsonl(
        session, _runner_case(), _evaluation(), runner_outcome="completed"
    )

    signal = next(
        signal
        for signal in compressed.signals
        if signal.kind == "post_failure_same_adapter_success"
    )
    assert signal.call_ids == ("after",)
    assert signal.detail == {"relationship": "temporal_only"}
    assert not any(signal.kind == "failure_recovered" for signal in compressed.signals)


def test_web_summary_text_is_bounded_and_marked_untrusted() -> None:
    oversized_snippet = "x" * 2_000
    session = _jsonl(
        _header(),
        _tool_call(1, 1, 1, "call-web", "web_search", '{"queries":["PostgreSQL join"]}'),
        _tool_result(
            2,
            1,
            1,
            "call-web",
            "result",
            is_error=False,
            meta={
                "sources": [
                    {
                        "title": "title",
                        "url": "https://example.test",
                        "snippet": oversized_snippet,
                    }
                ]
            },
        ),
        _assistant(3, 1, 2, [{"type": "text", "text": "done"}]),
        _turn_end(4, 1),
    )
    policy = CompressorPolicy(max_summary_text_bytes=64)

    compressed = compress_session_jsonl(
        session, _runner_case(), _evaluation(), runner_outcome="completed", policy=policy
    )

    call = compressed.trajectory[0].tool_calls[0]
    snippet = call.summary["sources"][0]["snippet"]
    assert call.summary["untrusted_evidence"] is True
    assert snippet.truncated is True
    assert len(snippet.text.encode("utf-8")) <= policy.max_summary_text_bytes
    assert "web_snippet" in compressed.compression.truncated_fields


def test_evolution_input_bounds_prompt_and_context_source(tmp_path: Path) -> None:
    case = _evolution_case(tmp_path, prompt="prompt " * 30)
    trajectory = compress_session_jsonl(
        _jsonl(
            _header(),
            _assistant(1, 1, 1, [{"type": "text", "text": "done"}]),
            _turn_end(2, 1),
        ),
        RunnerCaseOutput(case_id=case.instance_id, status="completed", trajectory=()),
        _evaluation(Verdict.NO_SQL_GENERATED, case_id=case.instance_id),
        runner_outcome="completed",
    )
    subject = _subject("skill")
    policy = CompressorPolicy(max_prompt_bytes=32, max_context_source_bytes=64)

    agent_input = build_evolution_agent_input(
        "manifest", "environment", subject, ((case, trajectory),), policy=policy
    )

    prompt = agent_input.trajectories[0].prompt
    assert prompt.endswith("…<truncated>")
    assert len(prompt.encode("utf-8")) <= policy.max_prompt_bytes
    payload = canonical_json_bytes(agent_input.model_dump(mode="json")).decode("utf-8")
    assert "redacted_session_sha256" not in payload
    assert agent_input.evaluated_skill.content_sha256 in payload

    with pytest.raises(
        TrajectoryCompressionError, match="Evolution agent input exceeds size limit"
    ):
        build_evolution_agent_input(
            "manifest",
            "environment",
            subject,
            ((case, trajectory),),
            policy=CompressorPolicy(max_agent_input_bytes=1),
        )

    case.schema_file.write_text("x" * 65, encoding="utf-8")
    with pytest.raises(
        TrajectoryCompressionError, match="schema context exceeds source size limit"
    ):
        build_evolution_agent_input(
            "manifest", "environment", subject, ((case, trajectory),), policy=policy
        )


def test_projection_only_limits_do_not_change_v1_custody_artifact() -> None:
    session = _jsonl(
        _header(),
        _assistant(1, 1, 1, [{"type": "text", "text": "done"}]),
        _turn_end(2, 1),
    )
    first_policy = CompressorPolicy(max_projection_text_bytes=32)
    second_policy = CompressorPolicy(max_projection_text_bytes=512)

    first = compress_session_jsonl(
        session, _runner_case(), _evaluation(), runner_outcome="completed", policy=first_policy
    )
    second = compress_session_jsonl(
        session, _runner_case(), _evaluation(), runner_outcome="completed", policy=second_policy
    )

    assert first_policy.sha256 == second_policy.sha256
    assert canonical_json_bytes(first.model_dump(mode="json")) == canonical_json_bytes(
        second.model_dump(mode="json")
    )


def _runner_case() -> RunnerCaseOutput:
    return RunnerCaseOutput(
        case_id="case-1",
        status="completed",
        final_message="```sql\nSELECT 1\n```",
        candidate_sql="SELECT 1",
        trajectory=(),
    )


def _evaluation(verdict: Verdict = Verdict.PASS, *, case_id: str = "case-1") -> CaseEvaluation:
    return CaseEvaluation(
        case_id=case_id,
        slice="Query",
        prompt="public prompt",
        verdict=verdict,
        candidate_sql="SELECT 1",
        expected_query="PRIVATE GOLD SQL",
        detail=None,
        expected_result={"private": "gold"},
        actual_result=None,
        usage={"input_tokens": 2, "output_tokens": 1, "cost_usd": None},
    )


def _header() -> dict[str, object]:
    return {"type": "session", "version": 0, "id": "session-1", "createdAt": 1}


def _assistant(
    seq: int, turn: int, step: int, content: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "type": "assistant/message",
        "seq": seq,
        "data": {
            "turn": turn,
            "step": step,
            "message": {"role": "assistant", "content": content},
            "usage": {"inputTokens": 2, "outputTokens": 1},
        },
    }


def _tool_call(
    seq: int, turn: int, step: int, call_id: str, name: str, arguments: str
) -> dict[str, object]:
    return {
        "type": "tool/call",
        "seq": seq,
        "data": {
            "turn": turn,
            "step": step,
            "callId": call_id,
            "name": name,
            "arguments": arguments,
        },
    }


def _tool_result(
    seq: int,
    turn: int,
    step: int,
    call_id: str,
    text: str,
    *,
    is_error: bool,
    meta: dict[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "turn": turn,
        "step": step,
        "message": {
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": call_id,
                    "content": [{"type": "text", "text": text}],
                    "isError": is_error,
                }
            ]
        },
    }
    if meta is not None:
        data["meta"] = meta
    return {
        "type": "tool/result",
        "seq": seq,
        "time": seq,
        "data": data,
    }


def _turn_end(seq: int, turn: int) -> dict[str, object]:
    return {"type": "turn/end", "seq": seq, "data": {"turn": turn, "reason": {"kind": "completed"}}}


def _jsonl(*events: dict[str, object]) -> bytes:
    return b"".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        for event in events
    )


def _subject(content: str) -> RunSubject:
    encoded = content.encode("utf-8")
    return RunSubject(
        subject_id="subject",
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        content=encoded,
    )


def _evolution_case(tmp_path: Path, *, prompt: str) -> LiveSQLBenchCase:
    context_names = ("schema.sql", "knowledge.json", "columns.json")
    context_paths = tuple(tmp_path / name for name in context_names)
    for path in context_paths:
        path.write_text("public context", encoding="utf-8")
    return LiveSQLBenchCase(
        instance_id="case-1",
        database_name="database",
        prompt=prompt,
        schema_file=context_paths[0],
        knowledge_file=context_paths[1],
        column_meaning_file=context_paths[2],
        gold_sql="PRIVATE GOLD",
        required_knowledge_ids=(),
        order_matters=True,
        difficulty_tier="Simple",
    )
