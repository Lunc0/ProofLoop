"""v3 optimizer-facing projection: bounded episodes, not business semantics."""

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


def test_native_sql_submit_intent_is_projected(tmp_path: Path) -> None:
    case = _case(tmp_path)
    session = _jsonl(
        _header(),
        _tool_call(
            1,
            1,
            1,
            "submit",
            "sql_submit",
            '{"query":"SELECT o.price FROM orders AS o"}',
        ),
        _tool_result(2, 1, 1, "submit", '{"ok":true,"intent":"submit"}'),
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

    assert evidence.detail["intent"] == "submit"


def test_v3_projection_selects_context_and_keeps_failure_episode(tmp_path: Path) -> None:
    case = _case(tmp_path)
    subject = _subject()
    session = _jsonl(
        _header(),
        _tool_call(1, 1, 1, "read-skill", "read", '{"path":"subject/SKILL.md"}'),
        _tool_result(2, 1, 1, "read-skill", subject.content.decode("utf-8")),
        _tool_call(3, 1, 2, "discover", "glob", '{"pattern":"*.sql"}'),
        _tool_result(4, 1, 2, "discover", "ok", meta={"paths": ["schema.sql", "notes.sql"]}),
        _tool_call(5, 1, 3, "read-schema", "read", '{"path":"schema.sql"}'),
        _tool_result(6, 1, 3, "read-schema", "schema loaded"),
        _assistant(7, 1, 4, "Use IRS and inspect price."),
        _tool_call(8, 1, 5, "bad-query", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
        _tool_result(9, 1, 5, "bad-query", "multiple SQL statements are not allowed"),
        _assistant(10, 1, 6, "I will use one statement."),
        _tool_call(
            11,
            1,
            7,
            "sample",
            "sql-readonly",
            '{"query":"SELECT o.price FROM orders AS o WHERE o.active = true LIMIT 20"}',
        ),
        _tool_result(12, 1, 7, "sample", '{"columns":["price"],"rows":[[10],[20],[30]]}'),
        _tool_call(
            13,
            1,
            8,
            "aggregate",
            "sql-readonly",
            '{"query":"SELECT MIN(o.price), MAX(o.price) FROM orders AS o WHERE o.active = true"}',
        ),
        _tool_result(14, 1, 8, "aggregate", '{"columns":["min","max"],"rows":[[10,30]]}'),
        _assistant(15, 1, 9, "I will submit one query."),
        _turn_end(16, 1),
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
        "custody-manifest", "test-environment", subject, ((case, trajectory),)
    )
    payload = canonical_json_bytes(projection.model_dump(mode="json")).decode("utf-8")
    projected_case = projection.trajectories[0]

    assert projection.schema_version == "evolution-coding-agent-input-v3"
    assert projection.evaluated_skill.content_sha256 == hashlib.sha256(
        projection.skill.encode("utf-8")
    ).hexdigest()
    assert "Do not infer the expected SQL or result from private material." in projection.skill
    assert "redacted_session_sha256" not in payload
    assert "PRIVATE GOLD" not in payload
    assert '"seq":' not in payload
    assert "workspace_discovery" in {evidence.kind for evidence in projected_case.evidence}
    assert "public_assistant_statement" not in {
        evidence.kind for evidence in projected_case.evidence
    }
    episode = next(
        evidence
        for evidence in projected_case.evidence
        if evidence.kind == "failure_recovery_episode"
    )
    assert episode.priority == "P0"
    assert episode.pre_failure_public_context == "Use IRS and inspect price."
    assert episode.post_failure_public_context == "I will use one statement."
    assert episode.failed_action.detail["direct_failure"] == {
        "category": "invalid_request",
        "cause": "multiple_sql_statements",
    }
    assert episode.recovery_observation == "first_subsequent_success_observed"
    assert len(episode.post_failure_actions) == 1
    sample = episode.post_failure_actions[0]
    assert sample.kind == "sql_query"
    assert sample.detail["coverage"] == "covered"
    assert sample.detail["covered_fields"] == ["ORDERS.PRICE"]
    assert "sample" not in sample.detail["result"]
    assert episode.skill_access.occurred_before_failure == "observed"
    assert episode.skill_access.subject_match == "exact"
    assert episode.skill_access.rule_excerpt is not None
    assert "multiple SQL statements" in episode.skill_access.rule_excerpt.text
    assert episode.skill_access.rule_excerpt.selection_basis == (
        "adapter_and_direct_failure_cause:sql-readonly:multiple_sql_statements"
    )
    assert len(canonical_json_bytes(episode.model_dump(mode="json"))) <= 4_096
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


def test_v3_projection_excludes_read_targets_and_redacts_prefixed_windows_paths(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    session = _jsonl(
        _header(),
        _tool_call(
            1,
            1,
            1,
            "read-session",
            "read",
            '{"path":"prefixC:\\\\custody\\\\case.session.jsonl"}',
        ),
        _tool_result(2, 1, 1, "read-session", "local session evidence"),
        _tool_call(3, 1, 2, "bad", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
        _tool_result(4, 1, 2, "bad", "multiple SQL statements are not allowed"),
        _tool_call(
            5,
            1,
            3,
            "post-failure",
            "pwsh",
            "Get-Content prefixC:\\custody\\case.session.jsonl",
        ),
        _tool_result(6, 1, 3, "post-failure", "ok"),
        _turn_end(7, 1),
    )

    projected_case = _projected_case(case, _subject(), session, runner_outcome="completed")
    payload = canonical_json_bytes(projected_case.model_dump(mode="json")).decode("utf-8")

    assert ".session.jsonl" not in payload
    assert "C:\\\\custody" not in payload
    assert "file_read" not in {item.kind for item in projected_case.evidence}
    episode = next(
        item for item in projected_case.evidence if item.kind == "failure_recovery_episode"
    )
    assert "argument_sha256" in episode.post_failure_actions[0].detail["action"]


def test_v3_failure_episode_uses_result_boundary_and_strict_step_context(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    subject = _subject()
    projected_case = _projected_case(
        case,
        subject,
        _jsonl(
            _header(),
            _assistant(1, 1, 1, "Known before the failed call."),
            _tool_call(2, 1, 2, "bad", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
            _assistant(3, 1, 2, "This shares the call step and is not pre-context."),
            _tool_call(4, 1, 3, "already-started", "sql-readonly", '{"query":"SELECT 3"}'),
            _tool_result(5, 1, 4, "bad", "multiple SQL statements are not allowed"),
            _assistant(6, 1, 4, "This shares the result step and is not post-context."),
            _tool_result(7, 1, 4, "already-started", '{"columns":["value"],"rows":[[3]]}'),
            _tool_call(8, 1, 5, "recovery", "sql-readonly", '{"query":"SELECT 4"}'),
            _tool_result(9, 1, 5, "recovery", '{"columns":["value"],"rows":[[4]]}'),
            _assistant(10, 1, 6, "Confirmed after the error result."),
            _turn_end(11, 1),
        ),
        runner_outcome="completed",
    )
    episode = _failure_episode(projected_case)

    assert episode.pre_failure_public_context == "Known before the failed call."
    assert episode.post_failure_public_context == "Confirmed after the error result."
    assert len(episode.post_failure_actions) == 1
    assert episode.post_failure_actions[0].detail["query"] == "SELECT 4"
    assert episode.recovery_observation == "first_subsequent_success_observed"


def test_v3_skill_rule_excerpt_requires_the_failed_adapter(tmp_path: Path) -> None:
    case = _case(tmp_path)
    content = b"# SQL skill\n\nDo not send multiple SQL statements to archive-sql.\n"
    subject = RunSubject(
        subject_id="subject",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    projected_case = _projected_case(
        case,
        subject,
        _jsonl(
            _header(),
            _tool_call(1, 1, 1, "read-skill", "read", '{"path":"subject/SKILL.md"}'),
            _tool_result(2, 1, 1, "read-skill", content.decode("utf-8")),
            _tool_call(3, 1, 2, "bad", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
            _tool_result(4, 1, 2, "bad", "multiple SQL statements are not allowed"),
            _assistant(5, 1, 3, "Stop."),
            _turn_end(6, 1),
        ),
        runner_outcome="completed",
    )
    episode = _failure_episode(projected_case)

    assert episode.skill_access.occurred_before_failure == "observed"
    assert episode.skill_access.subject_match == "exact"
    assert episode.skill_access.rule_excerpt is None


def test_v3_skill_access_degrades_conservatively(tmp_path: Path) -> None:
    case = _case(tmp_path)
    subject = _subject()
    declared_path = _projected_case(
        case,
        subject,
        _jsonl(
            _header(),
            _tool_call(1, 1, 1, "read-skill", "read", '{"path":"subject/SKILL.md"}'),
            _tool_result(2, 1, 1, "read-skill", "partial Skill output"),
            _assistant(3, 1, 2, "Try a query."),
            _tool_call(4, 1, 3, "bad", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
            _tool_result(5, 1, 3, "bad", "multiple SQL statements are not allowed"),
            _assistant(6, 1, 4, "Stop."),
            _turn_end(7, 1),
        ),
        runner_outcome="completed",
    )
    declared_episode = _failure_episode(declared_path)
    assert declared_episode.skill_access.occurred_before_failure == "observed"
    assert declared_episode.skill_access.subject_match == "declared_subject_path"

    complete_without_read = _projected_case(
        case,
        subject,
        _jsonl(
            _header(),
            _assistant(1, 1, 1, "Try a query."),
            _tool_call(2, 1, 2, "bad", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
            _tool_result(3, 1, 2, "bad", "multiple SQL statements are not allowed"),
            _assistant(4, 1, 3, "Stop."),
            _turn_end(5, 1),
        ),
        runner_outcome="completed",
    )
    complete_episode = _failure_episode(complete_without_read)
    assert complete_episode.skill_access.occurred_before_failure == "not_observed"
    assert complete_episode.skill_access.subject_match == "unknown"

    partial_without_read = _projected_case(
        case,
        subject,
        _jsonl(
            _header(),
            _assistant(1, 1, 1, "Try a query."),
            _tool_call(2, 1, 2, "bad", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
            _tool_result(3, 1, 2, "bad", "multiple SQL statements are not allowed"),
        ),
        runner_outcome="timeout",
    )
    partial_episode = _failure_episode(partial_without_read)
    assert partial_episode.skill_access.occurred_before_failure == "unknown"
    assert partial_episode.skill_access.subject_match == "unknown"


def test_v3_failure_episode_stops_before_a_new_failure(tmp_path: Path) -> None:
    case = _case(tmp_path)
    projected_case = _projected_case(
        case,
        _subject(),
        _jsonl(
            _header(),
            _assistant(1, 1, 1, "Try a query."),
            _tool_call(2, 1, 2, "first", "sql-readonly", '{"query":"SELECT 1; SELECT 2"}'),
            _tool_result(3, 1, 2, "first", "multiple SQL statements are not allowed"),
            _tool_call(4, 1, 3, "second", "sql-readonly", '{"query":"SELECT 3; SELECT 4"}'),
            _tool_result(5, 1, 3, "second", "multiple SQL statements are not allowed"),
            _tool_call(6, 1, 4, "success", "sql-readonly", '{"query":"SELECT 5"}'),
            _tool_result(7, 1, 4, "success", '{"columns":["value"],"rows":[[5]]}'),
            _assistant(8, 1, 5, "Done."),
            _turn_end(9, 1),
        ),
        runner_outcome="completed",
    )
    episodes = [
        evidence
        for evidence in projected_case.evidence
        if evidence.kind == "failure_recovery_episode"
    ]

    assert len(episodes) == 2
    assert episodes[0].post_failure_actions == ()
    assert episodes[0].recovery_observation == "no_success_observed"
    assert len(episodes[1].post_failure_actions) == 1
    assert episodes[1].post_failure_actions[0].outcome == "succeeded"


def test_v3_failure_episode_enforces_total_budget(tmp_path: Path) -> None:
    case = _case(tmp_path)
    queries = ["SELECT '" + ("x" * 180) + "'" for _ in range(20)]
    projected_case = _projected_case(
        case,
        _subject(),
        _jsonl(
            _header(),
            _assistant(1, 1, 1, "Search."),
            _tool_call(
                2,
                1,
                2,
                "oversized",
                "web_search",
                json.dumps({"queries": queries}),
            ),
            _tool_result(3, 1, 2, "oversized", "request rejected", is_error=True),
            _assistant(4, 1, 3, "Stop."),
            _turn_end(5, 1),
        ),
        runner_outcome="completed",
    )
    episode = _failure_episode(projected_case)

    assert len(canonical_json_bytes(episode.model_dump(mode="json"))) <= 4_096
    assert episode.failed_action.detail["tool"] == "web_search"
    assert episode.failed_action.detail["direct_failure"]["cause"] == "tool_result_is_error"
    assert len(episode.failed_action.detail["action"]["queries"]) <= 2


def _projected_case(
    case: LiveSQLBenchCase, subject: RunSubject, session: bytes, *, runner_outcome: str
):
    trajectory = compress_session_jsonl(
        session,
        RunnerCaseOutput(case_id=case.instance_id, status="completed", trajectory=()),
        _evaluation(case.instance_id),
        runner_outcome=runner_outcome,
    )
    return build_evolution_agent_input(
        "custody-manifest", "test-environment", subject, ((case, trajectory),)
    ).trajectories[0]


def _failure_episode(projected_case):
    return next(
        evidence
        for evidence in projected_case.evidence
        if evidence.kind == "failure_recovery_episode"
    )


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
    content = (
        b"# SQL skill\n\nDo not send multiple SQL statements to sql-readonly.\n"
        b"Do not infer the expected SQL or result from private material.\n"
    )
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
    is_error: bool = False,
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
    return {"type": "tool/result", "seq": seq, "time": seq, "data": data}


def _turn_end(seq: int, turn: int) -> dict[str, object]:
    return {"type": "turn/end", "seq": seq, "data": {"turn": turn, "reason": {"kind": "completed"}}}


def _jsonl(*events: dict[str, object]) -> bytes:
    return b"".join(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n" for event in events)
