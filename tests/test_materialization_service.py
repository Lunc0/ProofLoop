"""Offline materialization coverage for v2 title-section plans."""

from __future__ import annotations

import hashlib

from skill_evolve.candidate import CandidateService, LocalCandidateStore, subject_from_candidate
from skill_evolve.optimizer.edit_plan_contracts import (
    BehaviorAssertion,
    BlockEdit,
    DefectCluster,
    EditPlanSource,
    SkillEditPlanV2,
)
from skill_evolve.optimizer.materialization_service import materialize_after_evolution
from skill_evolve.optimizer.skill_map import build_skill_map
from skill_evolve.registry.store import RunStore
from skill_evolve.trajectory.compressor import canonical_json_bytes


def test_materialization_creates_a_v3_provenance_child(tmp_path) -> None:
    service, subject, run = _prepared(tmp_path)
    _write_plan(run, _plan(run.run_id, subject.content, subject.subject_id))

    result = materialize_after_evolution(run, subject, service, "001-validation")

    assert result.receipt.status == "created"
    child = service.verify("sql-analyst/001-validation")
    assert child.metadata.optimizer_provenance is not None
    assert (
        child.metadata.optimizer_provenance.render_policy_version
        == "skill-edit-render-policy-v3"
    )
    assert child.metadata.optimizer_provenance.patch_sha256 == result.receipt.patch_sha256


def test_materialization_skips_findings_only_v2_plan(tmp_path) -> None:
    service, subject, run = _prepared(tmp_path)
    plan = _plan(run.run_id, subject.content, subject.subject_id).model_copy(
        update={"status": "findings_only", "edits": ()}
    )
    _write_plan(run, plan)

    result = materialize_after_evolution(run, subject, service, "001-skipped")

    assert result.receipt.status == "skipped_no_actionable_plan"


def test_materialization_rejects_a_plan_for_the_wrong_parent(tmp_path) -> None:
    service, subject, run = _prepared(tmp_path)
    plan = _plan(run.run_id, subject.content, subject.subject_id)
    payload = plan.model_dump(mode="json")
    payload["source"]["parent_candidate_id"] = "sql-analyst/999-absent"
    run.write_bytes("optimizer/edit-plan/edit-plan.json", canonical_json_bytes(payload))
    run.write_json("optimizer/edit-plan/receipt.json", {"status": "proposed"})

    result = materialize_after_evolution(run, subject, service, "001-rejected")

    assert result.receipt.status == "invalid_binding"


def _prepared(tmp_path):
    service = CandidateService(LocalCandidateStore(tmp_path / ".local" / "skill-evolve"))
    parent_source = tmp_path / "parent.md"
    parent_source.write_bytes(_skill())
    parent = service.initialise("sql-analyst", parent_source)
    return service, subject_from_candidate(parent), RunStore(tmp_path / "runs").create()


def _skill() -> bytes:
    return (
        b"# SQL Skill\n\n"
        b"## Runtime Contract\n\n"
        b"Use read-only SQL.\n\n"
        b"## Core Strategy\n\n"
        b"### Validation\n\n"
        b"Check the draft answer before submission.\n"
    )


def _write_plan(run, plan: SkillEditPlanV2) -> None:
    payload = plan.model_dump(mode="json")
    run.write_bytes("optimizer/edit-plan/edit-plan.json", canonical_json_bytes(payload))
    run.write_json("optimizer/edit-plan/receipt.json", {"status": payload["status"]})


def _plan(run_id: str, parent: bytes, parent_candidate_id: str) -> SkillEditPlanV2:
    skill_map = build_skill_map(parent)
    cluster = DefectCluster(
        cluster_id="cluster-001",
        primary_module="validation",
        defect_type="missing_guidance",
        scope="failure_causal_hypothesis",
        statement="The observed path lacks a final validation action.",
        case_refs=("case-001-0123456789ab",),
        evidence_refs=("evidence-001-001-0123456789ab",),
    )
    return SkillEditPlanV2(
        status="proposed",
        source=EditPlanSource(
            run_id=run_id,
            parent_candidate_id=parent_candidate_id,
            parent_skill_sha256=hashlib.sha256(parent).hexdigest(),
            agent_input_sha256="a" * 64,
            target_contract_sha256="b" * 64,
            skill_map_sha256=hashlib.sha256(
                canonical_json_bytes(skill_map.model_dump(mode="json"))
            ).hexdigest(),
        ),
        summary="Replace one complete validation section.",
        clusters=(cluster,),
        edits=(
            BlockEdit(
                edit_id="edit-001",
                cluster_ids=(cluster.cluster_id,),
                operation="replace_block",
                primary_module="validation",
                target_block_id="b1.2",
                replacement_markdown=(
                    "## Improved Strategy\n\n"
                    "### Validation\n\n"
                    "Check the final SQL before submission.\n"
                ),
                intent="Replace the complete strategy section.",
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
                regression_risk="The section changes as one unit.",
                falsifiable_validation="The patch is replayable.",
            ),
        ),
    )
