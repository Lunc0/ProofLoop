"""v2 optimizer-facing projection: direct dependencies, not business semantics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skill_evolve.benchmark.livesqlbench import LiveSQLBenchCase
from skill_evolve.evaluator.core import CaseEvaluation, Verdict
from skill_evolve.runner.contracts import RunnerCaseOutput
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.agent_input import build_evolution_agent_input
from skill_evolve.trajectory.compressor import canonical_json_bytes, compress_session_jsonl
from skill_evolve.trajectory.dependency import extract_sql_references, sample_coverage


def test_sql_references_are_broader_than_sample_coverage() -> None:
    sample = extract_sql_references(
        "SELECT o.price FROM orders AS o WHERE o.active = true LIMIT 20"
    )
    aggregate = extract_sql_references(
        "SELECT MIN(o.price), MAX(o.price) FROM orders AS o WHERE o.active = true"
    )
    cte = extract_sql_references(
        "WITH chosen AS (SELECT o.price FROM orders AS o) SELECT * FROM chosen"
    )

    assert sample.tables == ("ORDERS",)
    assert sample.qualified_columns == ("ORDERS.ACTIVE", "ORDERS.PRICE")
    assert sample_coverage(sample, aggregate) == ("ORDERS.PRICE",)
    assert cte.parse_status == "parsed"
    assert cte.tables == ("ORDERS",)
    assert cte.has_cte is True
    assert cte.is_limited_sample is False
    assert sample_coverage(cte, aggregate) == ()


def test_sample_coverage_requires_equal_filter_values_and_operators() -> None:
    sample = extract_sql_references(
        "SELECT o.price FROM orders AS o WHERE o.kind = 'A' AND o.rank > 10 LIMIT 20"
    )
    different_string = extract_sql_references(
        "SELECT MIN(o.price) FROM orders AS o WHERE o.kind = 'B' AND o.rank > 10"
    )
    different_number = extract_sql_references(
        "SELECT MIN(o.price) FROM orders AS o WHERE o.kind = 'A' AND o.rank > 20"
    )
    different_operator = extract_sql_references(
        "SELECT MIN(o.price) FROM orders AS o WHERE o.kind = 'A' AND o.rank >= 10"
    )

    assert sample_coverage(sample, different_string) == ()
    assert sample_coverage(sample, different_number) == ()
    assert sample_coverage(sample, different_operator) == ()


def test_sql_readonly_powershell_command_is_extracted_conservatively(tmp_path: Path) -> None:
    case = _case(tmp_path)
    session = _jsonl(
        _header(),
        _tool_call(
            1,
            1,
            1,
            "sql",
            "pwsh",
            '{"command":".\\\\sql-readonly.ps1 -Query \'SELECT o.price FROM orders AS o\'"}',
        ),
        _tool_result(2, 1, 1, "sql", '{"columns":["price"],"rows":[[10]]}'),
        _turn_end(3, 1),
    )
    trajectory = compress_session_jsonl(
        session,
        RunnerCaseOutput(case_id=case.instance_id, status="completed", trajectory=()),
        _evaluation(case.instance_id),
        runner_outcome="completed",
    )

    projection = build_evolution_agent_input(
        "custody-manifest", "test-environment", _subject(), ((case, trajectory),)
    )
    evidence = next(
        item for item in projection.trajectories[0].evidence if item.kind == "sql_query"
    )

    assert evidence.detail["tables"] == ["ORDERS"]
    assert evidence.detail["qualified_columns"] == ["ORDERS.PRICE"]


def test_v2_projection_selects_direct_context_and_keeps_failure_chain(tmp_path: Path) -> None:
    case = _case(tmp_path)
    session = _jsonl(
        _header(),
        _tool_call(1, 1, 1, "discover", "glob", '{"pattern":"*.sql"}'),
        _tool_result(2, 1, 1, "discover", "ok", meta={"paths": ["schema.sql", "notes.sql"]}),
        _tool_call(3, 1, 2, "read-schema", "read", '{"path":"schema.sql"}'),
        _tool_result(4, 1, 2, "read-schema", "schema loaded"),
        _assistant(5, 1, 3, "Use IRS and inspect price."),
        _tool_call(6, 1, 4, "bad-query", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
        _tool_result(7, 1, 4, "bad-query", "multiple SQL statements are not allowed"),
        _tool_call(
            8,
            1,
            5,
            "sample",
            "sql-readonly",
            '{"query":"SELECT o.price FROM orders AS o WHERE o.active = true LIMIT 20"}',
        ),
        _tool_result(9, 1, 5, "sample", '{"columns":["price"],"rows":[[10],[20],[30]]}'),
        _tool_call(
            10,
            1,
            6,
            "aggregate",
            "sql-readonly",
            '{"query":"SELECT MIN(o.price), MAX(o.price) FROM orders AS o WHERE o.active = true"}',
        ),
        _tool_result(11, 1, 6, "aggregate", '{"columns":["min","max"],"rows":[[10,30]]}'),
        _assistant(12, 1, 7, "I will submit one query."),
        _turn_end(13, 1),
    )
    trajectory = compress_session_jsonl(
        session,
        RunnerCaseOutput(
            case_id=case.instance_id,
            status="completed",
            candidate_sql=None,
            final_message="```sql\nSELECT o.price FROM orders AS o\n```",
            trajectory=(),
        ),
        _evaluation(case.instance_id),
        runner_outcome="completed",
    )

    projection = build_evolution_agent_input(
        "custody-manifest", "test-environment", _subject(), ((case, trajectory),)
    )
    payload = canonical_json_bytes(projection.model_dump(mode="json")).decode("utf-8")
    projected_case = projection.trajectories[0]

    assert projection.schema_version == "evolution-coding-agent-input-v2"
    assert "sha256" not in payload
    assert "PRIVATE GOLD" not in payload
    assert "seq" not in payload
    assert "workspace_discovery" in {evidence.kind for evidence in projected_case.evidence}
    failure = next(
        evidence for evidence in projected_case.evidence if evidence.kind == "tool_failure"
    )
    assert failure.priority == "P0"
    assert failure.detail["later_same_tool_success"] is True
    sample = next(
        evidence
        for evidence in projected_case.evidence
        if evidence.kind == "sql_query" and "LIMIT 20" in evidence.detail["query"]
    )
    assert sample.detail["coverage"] == "covered"
    assert sample.detail["covered_fields"] == ["ORDERS.PRICE"]
    assert "sample" not in sample.detail["result"]
    assert projected_case.candidate_output.sql is None
    assert projected_case.final_submission_attempt is not None
    assert projected_case.final_submission_attempt.extraction_status == "runner_rejected"
    selected_context = {context.role: context for context in projected_case.context}
    assert selected_context["schema"].selection == "direct_reference"
    assert "CREATE TABLE orders" in selected_context["schema"].text
    assert "CREATE TABLE customers" in selected_context["schema"].text
    assert "CREATE TABLE inventory" not in selected_context["schema"].text
    assert selected_context["knowledge"].selection == "direct_reference"
    assert '"IRS"' in selected_context["knowledge"].text
    assert '"UNUSED"' not in selected_context["knowledge"].text


def _case(tmp_path: Path) -> LiveSQLBenchCase:
    schema = tmp_path / "schema.sql"
    knowledge = tmp_path / "knowledge.json"
    columns = tmp_path / "columns.json"
    schema.write_text(
        """CREATE TABLE orders (id integer, customer_id integer REFERENCES customers(id),
price integer, active boolean);
CREATE TABLE customers (id integer);
CREATE TABLE inventory (id integer);""",
        encoding="utf-8",
    )
    knowledge.write_text(
        '[{"id":"IRS","definition":"keep"},{"id":"UNUSED","definition":"drop"}]',
        encoding="utf-8",
    )
    columns.write_text(
        '[{"name":"price","meaning":"amount"},{"name":"unused","meaning":"drop"}]',
        encoding="utf-8",
    )
    return LiveSQLBenchCase(
        instance_id="case-1",
        database_name="database",
        prompt="Find public data.",
        schema_file=schema,
        knowledge_file=knowledge,
        column_meaning_file=columns,
        gold_sql="PRIVATE GOLD",
        required_knowledge_ids=(),
        order_matters=True,
        difficulty_tier="Simple",
    )


def _evaluation(case_id: str) -> CaseEvaluation:
    return CaseEvaluation(
        case_id=case_id,
        slice="Query",
        prompt="public prompt",
        verdict=Verdict.NO_SQL_GENERATED,
        candidate_sql=None,
        expected_query="PRIVATE GOLD",
        detail="No SQL was submitted.",
        expected_result={"private": "gold"},
        actual_result=None,
        usage={"input_tokens": 2, "output_tokens": 1, "cost_usd": None},
    )


def _subject() -> RunSubject:
    content = b"# skill\n"
    return RunSubject(
        subject_id="subject",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _header() -> dict[str, object]:
    return {"type": "session", "version": 0, "id": "session-1", "createdAt": 1}


def _assistant(seq: int, turn: int, step: int, text: str) -> dict[str, object]:
    return {
        "type": "assistant/message",
        "seq": seq,
        "data": {
            "turn": turn,
            "step": step,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
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
                    "isError": False,
                }
            ]
        },
    }
    if meta is not None:
        data["meta"] = meta
    return {"type": "tool/result", "seq": seq, "time": seq, "data": data}


def _turn_end(seq: int, turn: int) -> dict[str, object]:
    return {"type": "turn/end", "seq": seq, "data": {"turn": turn, "reason": {"kind": "completed"}}}


def _jsonl(*events: dict[str, object]) -> bytes:
    return b"".join(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n" for event in events)
