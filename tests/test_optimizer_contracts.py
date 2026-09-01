"""Strict persisted contracts for the v0.9 optimizer advice path."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_evolve.optimizer.contracts import AdviceSuggestion, ModelAdvice


def test_model_advice_rejects_unknown_cases_and_full_skill_documents() -> None:
    with pytest.raises(ValidationError, match="full SKILL"):
        ModelAdvice(
            summary="Observed a failed query.",
            suggestions=(
                AdviceSuggestion(
                    evidence_case_ids=("case-1",),
                    observation="The query failed.",
                    suggested_change="Add a validation step.",
                    suggested_markdown_fragment="# SKILL.md\nDo everything.",
                ),
            ),
        )


def test_model_advice_rejects_duplicate_evidence_case_ids() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        ModelAdvice(
            summary="Observed a failed query.",
            suggestions=(
                AdviceSuggestion(
                    evidence_case_ids=("case-1", "case-1"),
                    observation="The query failed.",
                    suggested_change="Add a validation step.",
                    suggested_markdown_fragment="- Validate the grouping before submission.",
                ),
            ),
        )
