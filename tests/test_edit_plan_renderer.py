"""Offline deterministic rendering coverage for stage 14."""

from __future__ import annotations

import hashlib

import pytest

from skill_evolve.optimizer.edit_plan_contracts import (
    BehaviorAssertion,
    DefectCluster,
    EditPlanSource,
    SemanticEdit,
    SkillEditPlan,
    WorkflowRule,
)
from skill_evolve.optimizer.edit_plan_renderer import (
    EditPlanRenderError,
    apply_unified_diff,
    render_edit_plan,
)
from skill_evolve.optimizer.skill_map import build_skill_map


def test_add_rule_renders_stable_patch_and_round_trips() -> None:
    parent = _skill()
    plan = _plan(parent, permission="evolvable")

    first = render_edit_plan(parent, plan)
    second = render_edit_plan(parent, plan)

    assert first == second
    assert apply_unified_diff(parent, first.patch_bytes) == first.child_bytes
    assert b"validation rule" in first.child_bytes
    assert first.changed_edit_ids == ("edit-001",)


def test_locked_target_and_unsupported_reorder_fail_closed() -> None:
    parent = _skill()
    with pytest.raises(EditPlanRenderError, match="locked"):
        render_edit_plan(parent, _plan(parent, permission="locked"))

    plan = _plan(parent, permission="evolvable", operation="reorder_workflow")
    with pytest.raises(EditPlanRenderError, match="not renderable"):
        render_edit_plan(parent, plan)


def test_merge_rejects_non_contiguous_targets() -> None:
    parent = (
        b"# Core Strategy\n\n"
        b"First validation rule.\n\n"
        b"An unrelated paragraph stays here.\n\n"
        b"Second validation rule.\n"
    )
    skill_map = build_skill_map(parent)
    paragraphs = [block for block in skill_map.blocks if block.block_type == "paragraph"]
    plan = _plan_with_targets(parent, (paragraphs[0], paragraphs[2]), operation="merge_rules")

    with pytest.raises(EditPlanRenderError, match="contiguous"):
        render_edit_plan(parent, plan)


def _skill() -> bytes:
    return (
        b"# Purpose and Scope\n\n"
        b"## Runtime Contract\n\n"
        b"Use read-only SQL.\n\n"
        b"## Core Strategy\n\n"
        b"### Validation\n\n"
        b"Check the draft answer before submission.\n"
    )


def _plan(parent: bytes, *, permission: str, operation: str = "add_rule") -> SkillEditPlan:
    skill_map = build_skill_map(parent)
    target = next(
        block
        for block in skill_map.blocks
        if block.permission == permission and block.block_type == "paragraph"
    )
    return _plan_with_targets(
        parent,
        (target,),
        operation=operation,
        add_rule=operation == "add_rule",
    )


def _plan_with_targets(
    parent: bytes,
    targets,
    *,
    operation: str,
    add_rule: bool = False,
) -> SkillEditPlan:
    source = EditPlanSource(
        run_id="20260902T000000Z-12345678",
        parent_candidate_id="sql-analyst/000-baseline",
        parent_skill_sha256=hashlib.sha256(parent).hexdigest(),
        agent_input_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        skill_map_sha256="c" * 64,
    )
    cluster = DefectCluster(
        cluster_id="cluster-001",
        primary_module="validation",
        defect_type="missing_guidance",
        scope="failure_causal_hypothesis",
        statement="The observed path has no final validation action.",
        case_refs=("case-001-0123456789ab",),
        evidence_refs=("evidence-001-001-0123456789ab",),
    )
    edit = SemanticEdit(
        edit_id="edit-001",
        cluster_ids=(cluster.cluster_id,),
        operation=operation,
        primary_module="validation",
        target_block_ids=() if add_rule else tuple(target.block_id for target in targets),
        placement_after_block_id=targets[0].block_id if add_rule else None,
        target_layers=tuple(sorted({target.layer for target in targets})),
        target_permissions=tuple(sorted({target.permission for target in targets})),
        intent="Add a bounded validation behavior.",
        rule=WorkflowRule(
            rule_kind="workflow",
            trigger="A draft answer is ready.",
            action="Check the draft against the task requirements.",
            validation="Record whether the check passed.",
            exit_or_next="Submit only after the check succeeds.",
        ),
        behavior_assertion=BehaviorAssertion(
            trigger_event="A draft answer is ready.",
            observed_before="The draft is submitted without validation.",
            expected_action="Run a final validation check.",
            required_before_submission="A validation result is available.",
            prohibited_early_behavior="Do not submit before checking.",
            success_or_stop_condition="Submit after success or stop explicitly.",
            validation_mode="automatic",
        ),
        generalization_scope="All tasks with a draft answer.",
        regression_risk="The check can add one step.",
        falsifiable_validation="A fresh trajectory shows validation before submission.",
    )
    return SkillEditPlan(
        status="proposed",
        source=source,
        summary="Add one shared validation rule.",
        clusters=(cluster,),
        edits=(edit,),
    )
