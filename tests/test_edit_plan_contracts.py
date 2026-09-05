"""Strict contract coverage for stage 13 edit plans."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_evolve.optimizer.edit_plan_contracts import (
    BehaviorAssertion,
    DefectCluster,
    EditPlanReceipt,
    HeuristicRule,
    ModelBlockEdit,
    ModelDefectCluster,
    ModelEditPlan,
    ModelEditPlanV2,
    ModelSemanticEdit,
    WorkflowRule,
    normalise_historical_edit_plan_status,
)

_CASE_REF = "case-001-0123456789ab"
_EVIDENCE_REF = "evidence-001-001-0123456789ab"
_BLOCK_1 = "b01"
_BLOCK_2 = "b02"
_HEADING_BLOCK = "b1.2"


def test_clusters_do_not_require_defect_type_specific_block_evidence() -> None:
    assert _cluster(defect_type="incorrect_guidance").block_ids == ()
    assert _cluster(defect_type="contradiction", block_ids=(_BLOCK_1,)).block_ids == (
        _BLOCK_1,
    )


def test_model_clusters_reject_retired_contract_fields() -> None:
    payload = _cluster(defect_type="incorrect_guidance", block_ids=(_BLOCK_1,)).model_dump(
        mode="json"
    )
    payload["contract_fields"] = ["max_statements_per_call"]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelDefectCluster.model_validate(payload)


def test_bound_cluster_discards_historical_contract_fields() -> None:
    payload = _cluster(defect_type="incorrect_guidance", block_ids=(_BLOCK_1,)).model_dump(
        mode="json"
    )
    payload.update(cluster_id="cluster-001", contract_fields=["max_statements_per_call"])

    assert "contract_fields" not in DefectCluster.model_validate(payload).model_dump(
        mode="json"
    )


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


@pytest.mark.parametrize(
    ("operation", "target_block_ids", "placement_after_block_id", "error"),
    [
        ("add_rule", (_BLOCK_1,), None, "add_rule must not include target_block_ids"),
        ("add_rule", (), None, "add_rule requires placement_after_block_id"),
        (
            "clarify_rule",
            (_BLOCK_1,),
            _BLOCK_2,
            "clarify_rule must not include placement_after_block_id",
        ),
        (
            "clarify_rule",
            (),
            None,
            "clarify_rule requires exactly one target_block_id",
        ),
        (
            "merge_rules",
            (_BLOCK_1,),
            None,
            "merge_rules requires at least two target_block_ids",
        ),
    ],
)
def test_model_edit_requires_operation_specific_location_shape(
    operation: str,
    target_block_ids: tuple[str, ...],
    placement_after_block_id: str | None,
    error: str,
) -> None:
    payload = _edit(1).model_dump(mode="json")
    payload.update(
        operation=operation,
        target_block_ids=target_block_ids,
        placement_after_block_id=placement_after_block_id,
    )

    with pytest.raises(ValidationError, match=error):
        ModelSemanticEdit.model_validate(payload)


def test_model_edit_accepts_operation_specific_location_shapes() -> None:
    add_rule = _edit(1)
    clarify_payload = _edit(1).model_dump(mode="json")
    clarify_payload.update(
        operation="clarify_rule",
        target_block_ids=(_BLOCK_1,),
        placement_after_block_id=None,
    )
    merge_payload = _edit(1).model_dump(mode="json")
    merge_payload.update(
        operation="merge_rules",
        target_block_ids=(_BLOCK_1, _BLOCK_2),
        placement_after_block_id=None,
    )

    assert add_rule.placement_after_block_id == _BLOCK_1
    assert ModelSemanticEdit.model_validate(clarify_payload).target_block_ids == (_BLOCK_1,)
    assert ModelSemanticEdit.model_validate(merge_payload).target_block_ids == (
        _BLOCK_1,
        _BLOCK_2,
    )


def test_reorder_workflow_requires_a_workflow_rule() -> None:
    payload = _edit(1).model_dump(mode="json")
    payload.update(
        operation="reorder_workflow",
        target_block_ids=(_BLOCK_1,),
        placement_after_block_id=None,
        rule=HeuristicRule(
            rule_kind="heuristic",
            applicability="When a workflow is available.",
            recommendation="Use the concise workflow.",
            boundary="Only for supported workflows.",
        ).model_dump(mode="json"),
    )

    with pytest.raises(ValidationError, match="reorder_workflow requires a workflow rule"):
        ModelSemanticEdit.model_validate(payload)


def test_semantic_text_rejects_patch_and_full_skill() -> None:
    with pytest.raises(ValidationError, match="mechanical patch"):
        _edit(1, intent="*** Begin Patch\nreplace everything")
    with pytest.raises(ValidationError, match="full Skill"):
        _edit(1, intent="# SKILL.md\nRewrite everything")


def test_v2_block_edits_allow_only_whole_section_replace_or_child_add() -> None:
    cluster = _cluster(defect_type="missing_guidance")
    replace = _block_edit("replace_block")
    add = _block_edit("add_block")

    assert ModelEditPlanV2(
        summary="Replace one complete section.", clusters=(cluster,), edits=(replace, add)
    ).schema_version == "skill-edit-plan-model-v2"

    legacy_location = replace.model_dump(mode="json")
    legacy_location["placement_after_block_id"] = "b01"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelBlockEdit.model_validate(legacy_location)

    invalid_add = add.model_dump(mode="json")
    invalid_add["target_block_id"] = _HEADING_BLOCK
    with pytest.raises(ValidationError, match="must not include replace_block"):
        ModelBlockEdit.model_validate(invalid_add)


def test_historical_guarded_status_normalises_but_unknown_status_is_rejected() -> None:
    assert normalise_historical_edit_plan_status("requires_guarded_approval") == "proposed"
    assert normalise_historical_edit_plan_status("findings_only") == "findings_only"
    with pytest.raises(ValueError, match="not materializable"):
        normalise_historical_edit_plan_status("unknown")


def test_receipt_keeps_prior_prompts_readable_and_defaults_new_plans_to_v3() -> None:
    historical_v1 = EditPlanReceipt(
        status="unavailable",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="skill-edit-plan-prompt-v1",
    )
    historical_v2 = EditPlanReceipt(
        status="unavailable",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="skill-edit-plan-prompt-v2",
    )
    historical_v3 = EditPlanReceipt(
        status="unavailable",
        provider="deepseek",
        model="deepseek-v4-flash-0731",
        prompt_version="skill-edit-plan-prompt-v3",
    )
    current = EditPlanReceipt(
        status="unavailable",
        provider="deepseek",
        model="deepseek-v4-flash-0731",
    )

    assert historical_v1.prompt_version == "skill-edit-plan-prompt-v1"
    assert historical_v2.prompt_version == "skill-edit-plan-prompt-v2"
    assert historical_v3.prompt_version == "skill-edit-plan-prompt-v3"
    assert current.model == "deepseek-v4-flash-0731"
    assert current.prompt_version == "skill-edit-plan-prompt-v6"


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


def _block_edit(operation: str) -> ModelBlockEdit:
    common = {
        "cluster_indices": (1,),
        "operation": operation,
        "primary_module": "validation",
        "intent": "Replace a complete reusable validation section.",
        "behavior_assertion": BehaviorAssertion(
            trigger_event="A candidate SQL statement is ready.",
            observed_before="Submission occurred without a final check.",
            expected_action="Run the final validation checklist.",
            required_before_submission="A validation result must be observed.",
            prohibited_early_behavior="Do not submit before validation.",
            success_or_stop_condition="Submit after success or stop with an explicit failure.",
            validation_mode="automatic",
        ),
        "generalization_scope": "All SQL tasks before final submission.",
        "regression_risk": "One section may become more specific.",
        "falsifiable_validation": "A fresh trajectory must show validation before submission.",
    }
    if operation == "replace_block":
        return ModelBlockEdit(
            **common,
            target_block_id=_HEADING_BLOCK,
            replacement_markdown="## Validation\n\nCheck the final SQL.\n",
        )
    return ModelBlockEdit(
        **common,
        parent_block_id="b1",
        after_block_id=_HEADING_BLOCK,
        new_block_markdown="## Added Validation\n\nCheck the final SQL.\n",
    )
