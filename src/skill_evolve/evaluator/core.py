"""Objective PostgreSQL evaluation, independent of the agent's own claims."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from skill_evolve.config.models import CaseConfig, EvaluatorConfig
from skill_evolve.runner.contracts import RunnerCaseOutput
from skill_evolve.sql.postgres import QueryExecution, QueryResult


class Verdict(StrEnum):
    PASS = "pass"
    EVALUATOR_INVALID = "evaluator_invalid"
    RUNNER_ERROR = "runner_error"
    NO_SQL_GENERATED = "no_sql_generated"
    UNSAFE_SQL = "unsafe_sql"
    SQL_EXECUTION_ERROR = "sql_execution_error"
    WRONG_RESULT = "wrong_result"


class QueryExecutor(Protocol):
    def execute_read_only(self, query: str) -> QueryExecution: ...


@dataclass(frozen=True)
class CaseEvaluation:
    case_id: str
    slice: str
    prompt: str
    verdict: Verdict
    candidate_sql: str | None
    expected_query: str
    detail: str | None
    expected_result: dict[str, Any] | None
    actual_result: dict[str, Any] | None
    usage: dict[str, int | float | None]

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"verdict": self.verdict.value}


def evaluate_case(
    case: CaseConfig,
    runner_case: RunnerCaseOutput,
    executor: QueryExecutor,
    evaluator: EvaluatorConfig,
) -> CaseEvaluation:
    """Evaluate expected and candidate SQL through one restricted executor."""

    usage = runner_case.usage.model_dump(mode="json")
    if runner_case.status == "runner_error":
        return _evaluation(
            case,
            runner_case,
            Verdict.RUNNER_ERROR,
            "runner returned an error",
            usage,
        )

    expected = executor.execute_read_only(case.expected_query)
    if not expected.ok:
        return _evaluation(
            case,
            runner_case,
            Verdict.EVALUATOR_INVALID,
            expected.error or "expected query could not be evaluated",
            usage,
        )
    if runner_case.candidate_sql is None or not runner_case.candidate_sql.strip():
        return _evaluation(
            case,
            runner_case,
            Verdict.NO_SQL_GENERATED,
            "runner completed without candidate SQL",
            usage,
            expected_result=_normalise_result(expected.result),
        )

    actual = executor.execute_read_only(runner_case.candidate_sql)
    if not actual.ok:
        verdict = Verdict.UNSAFE_SQL if actual.unsafe else Verdict.SQL_EXECUTION_ERROR
        return _evaluation(
            case,
            runner_case,
            verdict,
            actual.error or "candidate query could not be evaluated",
            usage,
            expected_result=_normalise_result(expected.result),
        )
    if not _results_equivalent(actual.result, expected.result, evaluator):
        return _evaluation(
            case,
            runner_case,
            Verdict.WRONG_RESULT,
            "candidate result is not equivalent to expected result",
            usage,
            expected_result=_normalise_result(expected.result),
            actual_result=_normalise_result(actual.result),
        )
    return _evaluation(
        case,
        runner_case,
        Verdict.PASS,
        None,
        usage,
        expected_result=_normalise_result(expected.result),
        actual_result=_normalise_result(actual.result),
    )


def _evaluation(
    case: CaseConfig,
    runner_case: RunnerCaseOutput,
    verdict: Verdict,
    detail: str | None,
    usage: dict[str, int | float | None],
    expected_result: dict[str, Any] | None = None,
    actual_result: dict[str, Any] | None = None,
) -> CaseEvaluation:
    return CaseEvaluation(
        case_id=case.id,
        slice=case.slice,
        prompt=case.prompt,
        verdict=verdict,
        candidate_sql=runner_case.candidate_sql,
        expected_query=case.expected_query,
        detail=detail,
        expected_result=expected_result,
        actual_result=actual_result,
        usage=usage,
    )


def _results_equivalent(
    actual: QueryResult | None,
    expected: QueryResult | None,
    evaluator: EvaluatorConfig,
) -> bool:
    if actual is None or expected is None:
        return False
    if actual.columns != expected.columns or len(actual.rows) != len(expected.rows):
        return False
    if evaluator.result_order == "strict":
        return all(
            _rows_equivalent(actual_row, expected_row, evaluator.numeric_tolerance)
            for actual_row, expected_row in zip(actual.rows, expected.rows, strict=True)
        )
    unmatched_expected = list(expected.rows)
    for actual_row in actual.rows:
        match_index = next(
            (
                index
                for index, expected_row in enumerate(unmatched_expected)
                if _rows_equivalent(actual_row, expected_row, evaluator.numeric_tolerance)
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched_expected.pop(match_index)
    return True


def _rows_equivalent(actual: tuple[Any, ...], expected: tuple[Any, ...], tolerance: float) -> bool:
    return len(actual) == len(expected) and all(
        _values_equivalent(actual_value, expected_value, tolerance)
        for actual_value, expected_value in zip(actual, expected, strict=True)
    )


def _values_equivalent(actual: Any, expected: Any, tolerance: float) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if tolerance > 0 and _is_numeric(actual) and _is_numeric(expected):
        return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal(str(tolerance))
    return _normalise_value(actual) == _normalise_value(expected)


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _normalise_result(result: QueryResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    rows = [[_normalise_value(value) for value in row] for row in result.rows]
    return {"columns": list(result.columns), "rows": rows}


def _normalise_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, (float, Decimal)):
        return {"type": "numeric", "value": format(Decimal(str(value)), "f")}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (date, datetime, time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, UUID):
        return {"type": "uuid", "value": str(value)}
    return {"type": type(value).__name__, "value": str(value)}


def stable_result_json(result: QueryResult) -> str:
    """Expose deterministic evidence formatting for reports and tests."""

    return json.dumps(_normalise_result(result), sort_keys=True, separators=(",", ":"))
