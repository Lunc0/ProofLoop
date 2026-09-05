"""Strict persisted contracts for the v0.9 optimizer advice path."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_evolve.optimizer.contracts import AdviceSuggestion, ModelAdvice, OptimizerReceipt


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


def test_receipt_accepts_historical_request_failed_code() -> None:
    receipt = OptimizerReceipt(
        status="model_failed",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="skill-optimization-advice-prompt-v1",
        error_code="request_failed",
    )

    assert receipt.error_code == "request_failed"
