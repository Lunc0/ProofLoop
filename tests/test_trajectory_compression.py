"""Deterministic, rule-based DSH trajectory projection coverage."""

from __future__ import annotations

import json

from skill_evolve.evaluator.core import CaseEvaluation, Verdict
from skill_evolve.runner.contracts import AgentExecution, RunnerCaseOutput
from skill_evolve.trajectory.compressor import canonical_json_bytes, compress_session_jsonl


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
    assert call.summary["queries"] == ["PostgreSQL join"]
    assert call.summary["retrieved_at_ms"] == 2
    assert call.summary["sources"] == [
        {"title": "A", "url": "https://a.example", "snippet": "first"},
        {"title": "B", "url": "https://b.example", "snippet": "second"},
    ]


def _runner_case() -> RunnerCaseOutput:
    return RunnerCaseOutput(
        case_id="case-1",
        status="completed",
        final_message="```sql\nSELECT 1\n```",
        candidate_sql="SELECT 1",
        trajectory=(),
    )


def _evaluation(verdict: Verdict = Verdict.PASS) -> CaseEvaluation:
    return CaseEvaluation(
        case_id="case-1",
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
