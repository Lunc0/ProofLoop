"""Offline deterministic rendering coverage for title-section edit plans."""

from __future__ import annotations

import hashlib

import pytest

from skill_evolve.optimizer.edit_plan_contracts import (
    BehaviorAssertion,
    BlockEdit,
    DefectCluster,
    EditPlanSource,
    SemanticEdit,
    SkillEditPlan,
    SkillEditPlanV2,
    WorkflowRule,
)
from skill_evolve.optimizer.edit_plan_renderer import (
    EditPlanRenderError,
    apply_unified_diff,
    render_edit_plan,
)
from skill_evolve.optimizer.skill_map import build_legacy_skill_map, build_skill_map
from skill_evolve.trajectory.compressor import canonical_json_bytes


def test_replace_section_is_stable_and_preserves_next_sibling() -> None:
    parent = _skill()
    plan = _v2_plan(parent, _replace_edit())

    first = render_edit_plan(parent, plan)
    second = render_edit_plan(parent, plan)

    assert first == second
    assert b"## Improved Strategy" in first.child_bytes
    assert b"### Updated Validation" in first.child_bytes
    assert b"## Notes" in first.child_bytes
    assert b"Old strategy text." not in first.child_bytes
    assert apply_unified_diff(parent, first.patch_bytes) == first.child_bytes
    assert first.changed_edit_ids == ("edit-001",)
    assert first.render_policy_version == "skill-edit-render-policy-v3"


def test_add_block_supports_after_and_append_positions() -> None:
    parent = _skill()
    after_plan = _v2_plan(
        parent,
        _add_edit(after_block_id="b1.2.1", heading="### Added After First"),
    )
    appended_plan = _v2_plan(
        parent,
        _add_edit(after_block_id=None, heading="### Added At End"),
    )

    after = render_edit_plan(parent, after_plan).child_bytes.decode("utf-8")
    appended = render_edit_plan(parent, appended_plan).child_bytes.decode("utf-8")

    assert (
        after.index("### First")
        < after.index("### Added After First")
        < after.index("### Second")
    )
    assert (
        appended.index("### Second")
        < appended.index("### Added At End")
        < appended.index("## Notes")
    )


def test_v2_renderer_rejects_locked_nesting_bad_fragment_and_map_mismatch() -> None:
    parent = _skill()
    locked_parent = _v2_plan(
        parent,
        _replace_edit(target_block_id="b1", replacement_markdown="# Replacement\n"),
    )
    with pytest.raises(EditPlanRenderError, match="locked"):
        render_edit_plan(parent, locked_parent)

    bad_fragment = _v2_plan(
        parent,
        _replace_edit(replacement_markdown="# Escapes Parent\n"),
    )
    with pytest.raises(EditPlanRenderError, match="expected heading level"):
        render_edit_plan(parent, bad_fragment)

    plan = _v2_plan(parent, _replace_edit())
    mismatched = plan.model_copy(
        update={"source": plan.source.model_copy(update={"skill_map_sha256": "a" * 64})}
    )
    with pytest.raises(EditPlanRenderError, match="map hash"):
        render_edit_plan(parent, mismatched)


def test_legacy_plan_still_uses_legacy_renderer_policy() -> None:
    parent = _skill()
    legacy_map = build_legacy_skill_map(parent)
    target = next(block for block in legacy_map.blocks if block.permission == "evolvable")
    cluster = _cluster()
    plan = SkillEditPlan(
        status="proposed",
        source=_source(parent, legacy_map),
        summary="Legacy paragraph edit remains readable.",
        clusters=(cluster,),
        edits=(
            SemanticEdit(
                edit_id="edit-001",
                cluster_ids=(cluster.cluster_id,),
                operation="correct_rule",
                primary_module="validation",
                target_block_ids=(target.block_id,),
                target_layers=(target.layer,),
                target_permissions=(target.permission,),
                intent="Replace one legacy paragraph.",
                rule=WorkflowRule(
                    rule_kind="workflow",
                    trigger="A draft is ready.",
                    action="Validate it.",
                    validation="Record the result.",
                    exit_or_next="Submit only after success.",
                ),
                behavior_assertion=_behavior(),
                generalization_scope="Legacy tasks.",
                regression_risk="The legacy section changes.",
                falsifiable_validation="The legacy patch is replayable.",
            ),
        ),
    )

    rendered = render_edit_plan(parent, plan)

    assert rendered.render_policy_version == "skill-edit-render-policy-v2"
    assert apply_unified_diff(parent, rendered.patch_bytes) == rendered.child_bytes


def _skill() -> bytes:
    return (
        b"# SQL Skill\n\n"
        b"## Runtime Contract\n\n"
        b"Use read-only SQL.\n\n"
        b"## Core Strategy\n\n"
        b"Old strategy text.\n\n"
        b"### First\n\n"
        b"First child text.\n\n"
        b"### Second\n\n"
        b"Second child text.\n\n"
        b"## Notes\n\n"
        b"Unrelated note.\n"
    )


def _v2_plan(parent: bytes, edit: BlockEdit) -> SkillEditPlanV2:
    cluster = _cluster()
    return SkillEditPlanV2(
        status="proposed",
        source=_source(parent, build_skill_map(parent)),
        summary="One bounded title-section edit.",
        clusters=(cluster,),
        edits=(edit.model_copy(update={"cluster_ids": (cluster.cluster_id,)}),),
    )


def _replace_edit(
    *,
    target_block_id: str = "b1.2",
    replacement_markdown: str = (
        "## Improved Strategy\n\n"
        "Use the supplied schema before querying.\n\n"
        "### Updated Validation\n\n"
        "Check the draft query once.\n"
    ),
) -> BlockEdit:
    return BlockEdit(
        edit_id="edit-001",
        cluster_ids=("cluster-001",),
        operation="replace_block",
        primary_module="planning_and_order",
        target_block_id=target_block_id,
        replacement_markdown=replacement_markdown,
        intent="Replace the complete strategy section.",
        behavior_assertion=_behavior(),
        generalization_scope="All SQL tasks.",
        regression_risk="The section changes as one unit.",
        falsifiable_validation="The new section is rendered and replayed exactly.",
    )


def _add_edit(*, after_block_id: str | None, heading: str) -> BlockEdit:
    return BlockEdit(
        edit_id="edit-001",
        cluster_ids=("cluster-001",),
        operation="add_block",
        primary_module="validation",
        parent_block_id="b1.2",
        after_block_id=after_block_id,
        new_block_markdown=f"{heading}\n\nAdded child text.\n",
        intent="Add a complete child validation section.",
        behavior_assertion=_behavior(),
        generalization_scope="All SQL tasks.",
        regression_risk="One child section is added.",
        falsifiable_validation="The added section has a deterministic location.",
    )


def _cluster() -> DefectCluster:
    return DefectCluster(
        cluster_id="cluster-001",
        primary_module="validation",
        defect_type="missing_guidance",
        scope="failure_causal_hypothesis",
        statement="The observed path lacks a final validation action.",
        case_refs=("case-001-0123456789ab",),
        evidence_refs=("evidence-001-001-0123456789ab",),
    )


def _behavior() -> BehaviorAssertion:
    return BehaviorAssertion(
        trigger_event="A draft answer is ready.",
        observed_before="The draft is submitted without validation.",
        expected_action="Run a final validation check.",
        required_before_submission="A validation result is available.",
        prohibited_early_behavior="Do not submit before checking.",
        success_or_stop_condition="Submit after success or stop explicitly.",
        validation_mode="automatic",
    )


def _source(parent: bytes, skill_map) -> EditPlanSource:
    return EditPlanSource(
        run_id="20260902T000000Z-12345678",
        parent_candidate_id="sql-analyst/000-baseline",
        parent_skill_sha256=hashlib.sha256(parent).hexdigest(),
        agent_input_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        skill_map_sha256=hashlib.sha256(
            canonical_json_bytes(skill_map.model_dump(mode="json"))
        ).hexdigest(),
    )
