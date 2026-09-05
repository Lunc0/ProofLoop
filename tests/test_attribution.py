"""Attribution must pause on evaluator/data defects before suggesting Skill work."""

from __future__ import annotations

from skill_evolve.attribution import AttributionKind, RunStatus, attribute
from skill_evolve.evaluator.core import CaseEvaluation, Verdict


def _evaluation(verdict: Verdict) -> CaseEvaluation:
    return CaseEvaluation(
        case_id="customer-count",
        slice="fast",
        prompt="Count customers.",
        verdict=verdict,
        candidate_sql="SELECT 1",
        expected_query="SELECT 1",
        detail=None,
        expected_result=None,
        actual_result=None,
        usage={"input_tokens": None, "output_tokens": None, "cost_usd": None},
    )


def test_evaluator_failure_pauses_even_when_another_case_is_skill_failure() -> None:
    result = attribute((_evaluation(Verdict.WRONG_RESULT), _evaluation(Verdict.EVALUATOR_INVALID)))

    assert result.kind is AttributionKind.EVAL_DATA
    assert result.status is RunStatus.EXPERIMENT_PAUSED


def test_candidate_sql_failure_requires_skill_change() -> None:
    result = attribute((_evaluation(Verdict.WRONG_RESULT),))

    assert result.kind is AttributionKind.SKILL
    assert result.status is RunStatus.NEEDS_SKILL_CHANGE


def test_runner_error_requires_skill_change() -> None:
    result = attribute((_evaluation(Verdict.RUNNER_ERROR),))

    assert result.kind is AttributionKind.SKILL
    assert result.status is RunStatus.NEEDS_SKILL_CHANGE
