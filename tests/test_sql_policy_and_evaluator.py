"""Pure SQL safety and objective-evaluation tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from skill_evolve.config.models import CaseConfig, EvaluatorConfig
from skill_evolve.evaluator import Verdict, evaluate_case
from skill_evolve.runner.contracts import RunnerCaseOutput, Usage
from skill_evolve.sql.policy import SqlPolicyError, validate_read_only_sql
from skill_evolve.sql.postgres import QueryExecution, QueryResult


@dataclass
class StubExecutor:
    responses: list[QueryExecution]

    def execute_read_only(self, query: str) -> QueryExecution:
        del query
        return self.responses.pop(0)


def _case() -> CaseConfig:
    return CaseConfig(
        id="customer-count",
        slice="fast",
        prompt="Count customers.",
        expected_query="SELECT COUNT(*) AS count FROM customers",
    )


def _runner_case(sql: str | None) -> RunnerCaseOutput:
    return RunnerCaseOutput(
        case_id="customer-count",
        status="completed",
        candidate_sql=sql,
        trajectory=(),
        usage=Usage(cost_usd=0.01),
    )


def _evaluator() -> EvaluatorConfig:
    return EvaluatorConfig(version="sql-result-v1")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM customers",
        "WITH counts AS (SELECT COUNT(*) AS total FROM customers) SELECT total FROM counts",
        "EXPLAIN SELECT * FROM customers",
        "SELECT 'DELETE FROM customers' AS text",
    ],
)
def test_read_only_policy_accepts_select_queries(query: str) -> None:
    validate_read_only_sql(query)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM customers",
        "SELECT 1; SELECT 2",
        "WITH removed AS (DELETE FROM customers RETURNING id) SELECT * FROM removed",
        "SELECT * INTO other_table FROM customers",
    ],
)
def test_read_only_policy_rejects_mutation_capable_queries(query: str) -> None:
    with pytest.raises(SqlPolicyError):
        validate_read_only_sql(query)


def test_equivalent_results_pass_even_when_rows_are_reordered() -> None:
    expected = QueryExecution(result=QueryResult(columns=("id",), rows=((1,), (2,))))
    actual = QueryExecution(result=QueryResult(columns=("id",), rows=((2,), (1,))))

    evaluation = evaluate_case(
        _case(),
        _runner_case("SELECT id FROM customers"),
        StubExecutor([expected, actual]),
        _evaluator(),
    )

    assert evaluation.verdict is Verdict.PASS
    assert evaluation.usage["cost_usd"] == 0.01


def test_invalid_expected_query_is_evaluator_failure() -> None:
    expected = QueryExecution(error="syntax error")

    evaluation = evaluate_case(
        _case(), _runner_case("SELECT 1"), StubExecutor([expected]), _evaluator()
    )

    assert evaluation.verdict is Verdict.EVALUATOR_INVALID


def test_unsafe_candidate_is_skill_failure() -> None:
    expected = QueryExecution(result=QueryResult(columns=("id",), rows=((1,),)))
    actual = QueryExecution(error="read-only policy rejected keyword: DELETE", unsafe=True)

    evaluation = evaluate_case(
        _case(),
        _runner_case("DELETE FROM customers"),
        StubExecutor([expected, actual]),
        _evaluator(),
    )

    assert evaluation.verdict is Verdict.UNSAFE_SQL
