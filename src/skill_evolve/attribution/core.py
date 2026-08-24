"""Conservative Skill versus Eval/data attribution rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from skill_evolve.evaluator.core import CaseEvaluation, Verdict


class AttributionKind(StrEnum):
    NONE = "none"
    SKILL = "skill"
    EVAL_DATA = "eval_data"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_SKILL_CHANGE = "needs_skill_change"
    EXPERIMENT_PAUSED = "experiment_paused"


@dataclass(frozen=True)
class Attribution:
    kind: AttributionKind
    status: RunStatus
    reason: str
    case_ids: tuple[str, ...]
    verdicts: tuple[str, ...]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"kind": self.kind.value, "status": self.status.value}


def attribute(evaluations: tuple[CaseEvaluation, ...]) -> Attribution:
    """Prefer pausing on evaluator/runner evidence defects over blaming a Skill."""

    eval_data = tuple(
        evaluation
        for evaluation in evaluations
        if evaluation.verdict in {Verdict.EVALUATOR_INVALID, Verdict.RUNNER_ERROR}
    )
    if eval_data:
        return _eval_data_attribution(
            reason="evaluator_or_runner_evidence_invalid",
            case_ids=tuple(evaluation.case_id for evaluation in eval_data),
            verdicts=tuple(evaluation.verdict.value for evaluation in eval_data),
        )
    skill_failures = tuple(evaluation for evaluation in evaluations if not evaluation.passed)
    if skill_failures:
        return Attribution(
            kind=AttributionKind.SKILL,
            status=RunStatus.NEEDS_SKILL_CHANGE,
            reason="candidate_sql_failed_objective_evaluation",
            case_ids=tuple(evaluation.case_id for evaluation in skill_failures),
            verdicts=tuple(evaluation.verdict.value for evaluation in skill_failures),
            next_action=(
                "review the regression draft, update the Skill, then rerun the same manifest"
            ),
        )
    return Attribution(
        kind=AttributionKind.NONE,
        status=RunStatus.COMPLETED,
        reason="all_requested_cases_passed",
        case_ids=(),
        verdicts=(),
        next_action="record the result and rerun after a deliberate Skill or environment change",
    )


def eval_data_failure(reason: str) -> Attribution:
    """Represent failures occurring before case-level objective evaluation."""

    return _eval_data_attribution(reason=reason, case_ids=(), verdicts=())


def _eval_data_attribution(
    reason: str, case_ids: tuple[str, ...], verdicts: tuple[str, ...]
) -> Attribution:
    return Attribution(
        kind=AttributionKind.EVAL_DATA,
        status=RunStatus.EXPERIMENT_PAUSED,
        reason=reason,
        case_ids=case_ids,
        verdicts=verdicts,
        next_action="fix and version evaluator/data or runner evidence, then start a new run",
    )
