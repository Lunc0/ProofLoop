"""Strict receipt contracts for no-model child materialization."""

import pytest
from pydantic import ValidationError

from skill_evolve.optimizer.materialization_contracts import (
    MaterializationReceipt,
    MaterializationSource,
)


def test_created_receipt_requires_complete_child_evidence() -> None:
    source = MaterializationSource(
        run_id="20260902T000000Z-12345678",
        parent_candidate_id="sql-analyst/000-baseline",
        parent_skill_sha256="a" * 64,
        edit_plan_sha256="b" * 64,
        target_contract_sha256="c" * 64,
    )
    with pytest.raises(ValidationError, match="requires complete child evidence"):
        MaterializationReceipt(status="created", source=source)

    receipt = MaterializationReceipt(
        status="created",
        source=source,
        child_candidate_id="sql-analyst/001-child",
        child_skill_sha256="d" * 64,
        patch_sha256="e" * 64,
        edit_count=1,
    )
    assert receipt.status == "created"


def test_failed_receipt_cannot_claim_a_child() -> None:
    with pytest.raises(ValidationError, match="must not claim"):
        MaterializationReceipt(
            status="render_failed",
            child_candidate_id="sql-analyst/001-child",
        )
