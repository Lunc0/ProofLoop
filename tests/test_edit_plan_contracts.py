"""Strict contract coverage for stage 13 edit plans."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_evolve.optimizer.edit_plan_contracts import (
    BehaviorAssertion,
    ModelDefectCluster,
    ModelEditPlan,
    ModelSemanticEdit,
    WorkflowRule,
)

_CASE_REF = "case-001-0123456789ab"
_EVIDENCE_REF = "evidence-001-001-0123456789ab"
_BLOCK_1 = "block-0123456789abcdef"
_BLOCK_2 = "block-fedcba9876543210"


def test_incorrect_guidance_requires_block_and_contract_field() -> None:
    with pytest.raises(ValidationError, match="requires a Skill block and contract field"):
        _cluster(defect_type="incorrect_guidance")


def test_contradiction_requires_two_distinct_skill_blocks() -> None:
    with pytest.raises(ValidationError, match="at least two Skill blocks"):
        _cluster(defect_type="contradiction", block_ids=(_BLOCK_1,))

    cluster = _cluster(
        defect_type="contradiction",
        block_ids=(_BLOCK_1, _BLOCK_2),
    )
    assert cluster.defect_type == "contradiction"


def test_model_plan_limits_edits_and_rejects_unknown_cluster_indices() -> None:
    cluster = _cluster(defect_type="missing_guidance")
    with pytest.raises(ValidationError, match="unknown cluster index"):
        ModelEditPlan(summary="Bounded change.", clusters=(cluster,), edits=(_edit(2),))

    with pytest.raises(ValidationError, match="at most 3 items"):
        ModelEditPlan(
            summary="Too many changes.",
            clusters=(cluster,),
            edits=(_edit(1), _edit(1), _edit(1), _edit(1)),
        )


def test_semantic_text_rejects_patch_and_full_skill() -> None:
    with pytest.raises(ValidationError, match="mechanical patch"):
        _edit(1, intent="*** Begin Patch\nreplace everything")
    with pytest.raises(ValidationError, match="full Skill"):
        _edit(1, intent="# SKILL.md\nRewrite everything")


def _cluster(
    *,
    defect_type: str,
    block_ids: tuple[str, ...] = (),
) -> ModelDefectCluster:
    return ModelDefectCluster.model_validate(
        {
            "primary_module": "validation",
            "defect_type": defect_type,
            "scope": "failure_causal_hypothesis",
            "statement": "The observed path lacks a bounded validation action.",
            "case_refs": [_CASE_REF],
            "evidence_refs": [_EVIDENCE_REF],
            "block_ids": list(block_ids),
        }
    )


def _edit(
    cluster_index: int, *, intent: str = "Add one reusable validation rule."
) -> ModelSemanticEdit:
    return ModelSemanticEdit(
        cluster_indices=(cluster_index,),
        operation="add_rule",
        primary_module="validation",
        placement_after_block_id=_BLOCK_1,
        intent=intent,
        rule=WorkflowRule(
            rule_kind="workflow",
            trigger="Before final submission.",
            action="Check the candidate against all task requirements.",
            validation="Confirm the check completed.",
            exit_or_next="Submit only after validation succeeds.",
        ),
        behavior_assertion=BehaviorAssertion(
            trigger_event="A candidate SQL statement is ready.",
            observed_before="Submission occurred without a final check.",
            expected_action="Run the final validation checklist.",
            required_before_submission="A validation result must be observed.",
            prohibited_early_behavior="Do not submit before validation.",
            success_or_stop_condition="Submit after success or stop with an explicit failure.",
            validation_mode="automatic",
        ),
        generalization_scope="All SQL tasks before final submission.",
        regression_risk="An unnecessary check could add one extra step.",
        falsifiable_validation="A fresh trajectory must show validation before submission.",
    )
