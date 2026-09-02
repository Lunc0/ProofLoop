"""Offline materialization service coverage with synthetic run artifacts."""

from __future__ import annotations

import hashlib

from skill_evolve.candidate import CandidateService, LocalCandidateStore, subject_from_candidate
from skill_evolve.optimizer.edit_plan_contracts import (
    BehaviorAssertion,
    DefectCluster,
    EditPlanSource,
    SemanticEdit,
    SkillEditPlan,
    WorkflowRule,
)
from skill_evolve.optimizer.materialization_service import materialize_after_evolution
from skill_evolve.optimizer.skill_map import build_skill_map
from skill_evolve.registry.store import RunStore
from skill_evolve.trajectory.compressor import canonical_json_bytes


def test_materialization_creates_a_verified_optimizer_child(tmp_path) -> None:
    service = CandidateService(LocalCandidateStore(tmp_path / ".local" / "skill-evolve"))
    parent_source = tmp_path / "parent.md"
    parent_source.write_bytes(_skill())
    parent = service.initialise("sql-analyst", parent_source)
    subject = subject_from_candidate(parent)
    run = RunStore(tmp_path / "runs").create()
    _write_plan(run, _plan(run.run_id, subject.content, subject.subject_id))

    result = materialize_after_evolution(run, subject, service, "001-validation")

    assert result.receipt.status == "created"
    assert result.patch_path == "optimizer/materialization/rendered.patch"
    child = service.verify("sql-analyst/001-validation")
    assert child.metadata.optimizer_provenance is not None
    assert child.metadata.optimizer_provenance.patch_sha256 == result.receipt.patch_sha256
    assert {item.name for item in (run.path / "optimizer" / "materialization").iterdir()} == {
        "rendered.patch",
        "receipt.json",
    }


def test_materialization_reads_historical_guarded_status_without_approval(tmp_path) -> None:
    service = CandidateService(LocalCandidateStore(tmp_path / ".local" / "skill-evolve"))
    parent_source = tmp_path / "parent.md"
    parent_source.write_bytes(_skill())
    parent = service.initialise("sql-analyst", parent_source)
    subject = subject_from_candidate(parent)
    run = RunStore(tmp_path / "runs").create()
    _write_plan(
        run,
        _plan(
            run.run_id,
            subject.content,
            subject.subject_id,
            historical_guarded_status=True,
        ),
    )

    result = materialize_after_evolution(run, subject, service, "001-legacy")

    assert result.receipt.status == "created"


def test_materialization_allows_a_guarded_target_without_approval(tmp_path) -> None:
    service = CandidateService(LocalCandidateStore(tmp_path / ".local" / "skill-evolve"))
    parent_source = tmp_path / "parent.md"
    parent_source.write_bytes(_skill())
    parent = service.initialise("sql-analyst", parent_source)
    subject = subject_from_candidate(parent)
    run = RunStore(tmp_path / "runs").create()
    _write_plan(
        run,
        _plan(
            run.run_id,
            subject.content,
            subject.subject_id,
            target_permission="guarded",
        ),
    )

    result = materialize_after_evolution(run, subject, service, "001-guarded")

    assert result.receipt.status == "created"


def test_materialization_rejects_a_plan_for_the_wrong_parent(tmp_path) -> None:
    service = CandidateService(LocalCandidateStore(tmp_path / ".local" / "skill-evolve"))
    parent_source = tmp_path / "parent.md"
    parent_source.write_bytes(_skill())
    parent = service.initialise("sql-analyst", parent_source)
    subject = subject_from_candidate(parent)
    run = RunStore(tmp_path / "runs").create()
    plan = _plan(run.run_id, subject.content, subject.subject_id)
    payload = plan.model_dump(mode="json")
    payload["source"]["parent_candidate_id"] = "sql-analyst/999-absent"
    run.write_bytes("optimizer/edit-plan/edit-plan.json", canonical_json_bytes(payload))

    result = materialize_after_evolution(run, subject, service, "001-rejected")

    assert result.receipt.status == "invalid_binding"
    rejected = (
        tmp_path / ".local" / "skill-evolve" / "candidates" / "sql-analyst" / "001-rejected"
    )
    assert not rejected.exists()


def _skill() -> bytes:
    return (
        b"# Purpose and Scope\n\n"
        b"Use this Skill for read-only SQL tasks.\n\n"
        b"## Runtime Contract\n\n"
        b"Use read-only SQL.\n\n"
        b"## Core Strategy\n\n"
        b"### Validation\n\n"
        b"Check the draft answer before submission.\n"
    )


def _write_plan(run, plan: SkillEditPlan) -> None:
    payload = plan.model_dump(mode="json")
    if plan.summary == "legacy":
        payload["status"] = "requires_guarded_approval"
    run.write_bytes("optimizer/edit-plan/edit-plan.json", canonical_json_bytes(payload))
    run.write_json("optimizer/edit-plan/receipt.json", {"status": payload["status"]})


def _plan(
    run_id: str,
    parent: bytes,
    parent_candidate_id: str,
    *,
    historical_guarded_status: bool = False,
    target_permission: str = "evolvable",
) -> SkillEditPlan:
    skill_map = build_skill_map(parent)
    target = next(
        block
        for block in skill_map.blocks
        if block.permission == target_permission and block.block_type == "paragraph"
    )
    cluster = DefectCluster(
        cluster_id="cluster-001",
        primary_module="validation",
        defect_type="missing_guidance",
        scope="failure_causal_hypothesis",
        statement="The observed path lacks a final validation action.",
        case_refs=("case-001-0123456789ab",),
        evidence_refs=("evidence-001-001-0123456789ab",),
    )
    return SkillEditPlan(
        status="proposed",
        source=EditPlanSource(
            run_id=run_id,
            parent_candidate_id=parent_candidate_id,
            parent_skill_sha256=hashlib.sha256(parent).hexdigest(),
            agent_input_sha256="a" * 64,
            target_contract_sha256="b" * 64,
            skill_map_sha256="c" * 64,
        ),
        summary="legacy" if historical_guarded_status else "Add one validation rule.",
        clusters=(cluster,),
        edits=(
            SemanticEdit(
                edit_id="edit-001",
                cluster_ids=(cluster.cluster_id,),
                operation="add_rule",
                primary_module="validation",
                placement_after_block_id=target.block_id,
                target_layers=(target.layer,),
                target_permissions=(target.permission,),
                intent="Add a bounded validation behavior.",
                rule=WorkflowRule(
                    rule_kind="workflow",
                    trigger="A draft answer is ready.",
                    action="Check the draft against task requirements.",
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
            ),
        ),
    )
