from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skill_evolve.candidate import CandidateService, LocalCandidateStore
from skill_evolve.optimizer.coding_agent.launcher import CodexLaunchSettings
from skill_evolve.optimizer.coding_agent.materialization import materialize_coding_agent_edits
from skill_evolve.optimizer.coding_agent.service import edit_with_coding_agent
from skill_evolve.registry import RunDirectory
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import canonical_json_bytes
from skill_evolve.trajectory.contracts import (
    EvaluatedSkill,
    EvolutionAgentInputV3,
    EvolutionProjectionCaseV3,
    ProjectionCandidateOutput,
    ProjectionEvaluation,
    ProjectionEvidence,
)

PARENT = (
    b"# SQL Skill\n\n"
    b"## Runtime Contract\n\n"
    b"Use one read-only statement.\n\n"
    b"## Core Strategy\n\n"
    b"Inspect the schema.\n"
)


def make_subject() -> RunSubject:
    return RunSubject(
        subject_id="demo/parent",
        content_sha256=hashlib.sha256(PARENT).hexdigest(),
        content=PARENT,
    )


def write_evolution_input(run: RunDirectory, subject: RunSubject) -> None:
    payload = EvolutionAgentInputV3(
        projection_rule_version="evolution-projection-v3.1",
        environment_fingerprint="e" * 64,
        evaluated_skill=EvaluatedSkill(
            subject_id=subject.subject_id,
            content_sha256=subject.content_sha256,
        ),
        skill=subject.content.decode(),
        trajectories=(
            EvolutionProjectionCaseV3(
                case_id="case-1",
                prompt="Count customers.",
                task_contract="Submit SQL.",
                difficulty_tier="Simple",
                context=(),
                evidence=(
                    ProjectionEvidence(kind="tool_failure", priority="P0", outcome="failed"),
                ),
                candidate_output=ProjectionCandidateOutput(sql=None),
                evaluation=ProjectionEvaluation(
                    slice="evolution", verdict="no_sql_generated", usage={}
                ),
            ),
        ),
    )
    payload_bytes = canonical_json_bytes(payload.model_dump(mode="json"))
    run.write_bytes("agent-input/trajectory.json", payload_bytes + b"\n")
    run.write_json(
        "agent-input/manifest.json",
        {
            "schema_version": "evolution-coding-agent-input-v3",
            "exposed_for_optimization": True,
            "manifest_sha256": "a" * 64,
            "environment_fingerprint": payload.environment_fingerprint,
            "projection_rule_version": payload.projection_rule_version,
            "agent_input_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "included_case_count": 1,
            "omitted_case_count": 0,
        },
    )


def successful_codex_runner(
    args: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    if args[-1] == "--version":
        return subprocess.CompletedProcess(args, 0, "codex-cli 0.153.1\n", "")
    workspace = Path(kwargs["cwd"])
    evidence = json.loads((workspace / "input/evidence.json").read_text(encoding="utf-8"))
    evidence_ref = evidence["cases"][0]["evidence"][0]["evidence_ref"]
    replacement = "## Core Strategy\n\nInspect every referenced column before submission.\n"
    child = PARENT.replace(b"## Core Strategy\n\nInspect the schema.\n", replacement.encode())
    (workspace / "output/SKILL.md").write_bytes(child)
    (workspace / "output/atomic-edits.json").write_text(
        json.dumps(
            {
                "schema_version": "atomic-skill-edit-declaration-v1",
                "edits": [
                    {
                        "operation": "replace_block",
                        "target_block_id": "b1.2",
                        "replacement_markdown": replacement,
                        "hypothesis": "Validate referenced columns before submission",
                        "evidence_refs": [evidence_ref],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(args, 0, "", "")


def test_materialize_creates_full_and_singleton_draft_siblings(tmp_path: Path) -> None:
    candidate_root = tmp_path / "candidates"
    candidate_service = CandidateService(LocalCandidateStore(candidate_root))
    parent_source = tmp_path / "parent.md"
    parent_source.write_bytes(PARENT)
    candidate_service.initialise("demo", parent_source, candidate_name="parent")

    run = RunDirectory("run-1", tmp_path / "runs/run-1")
    run.path.mkdir(parents=True)
    subject = make_subject()
    write_evolution_input(run, subject)
    workspace_root = tmp_path / "agent-workspaces"
    workspace_root.mkdir()
    edit_result = edit_with_coding_agent(
        run,
        subject,
        run_status="task_failure",
        repository_root=tmp_path / "repo",
        candidate_store_root=candidate_root,
        runs_root=tmp_path / "runs",
        workspace_root=workspace_root,
        settings=CodexLaunchSettings(binary=sys.executable),
        runner=successful_codex_runner,
    )
    assert edit_result.receipt.status == "validated"

    result = materialize_coding_agent_edits(
        run,
        subject,
        candidate_service,
        "agent-001",
    )

    assert result.receipt.status == "created"
    assert result.receipt.created_candidate_ids == (
        "demo/agent-001-full",
        "demo/agent-001-edit-001",
    )
    full = candidate_service.verify("demo/agent-001-full")
    singleton = candidate_service.verify("demo/agent-001-edit-001")
    assert full.skill_bytes == singleton.skill_bytes
    assert full.metadata.status.value == "draft"
    assert full.metadata.parent_candidate_id == "demo/parent"
    assert full.metadata.origin.value == "coding_agent"
    assert full.metadata.coding_agent_provenance is not None
    assert full.metadata.coding_agent_provenance.candidate_role == "full"
    assert singleton.metadata.coding_agent_provenance is not None
    assert singleton.metadata.coding_agent_provenance.edit_id == "edit-001"
    metadata_path = candidate_root / "candidates/demo/agent-001-full/metadata.json"
    public = json.loads(metadata_path.read_text())
    assert public["optimizer_provenance"] is None

    with pytest.raises(ValueError, match="already exists"):
        materialize_coding_agent_edits(run, subject, candidate_service, "agent-002")


def test_materialize_rejects_full_skill_hash_mismatch(tmp_path: Path) -> None:
    candidate_root = tmp_path / "candidates"
    candidate_service = CandidateService(LocalCandidateStore(candidate_root))
    parent_source = tmp_path / "parent.md"
    parent_source.write_bytes(PARENT)
    candidate_service.initialise("demo", parent_source, candidate_name="parent")
    run = RunDirectory("run-1", tmp_path / "runs/run-1")
    run.path.mkdir(parents=True)
    subject = make_subject()
    write_evolution_input(run, subject)
    workspace_root = tmp_path / "agent-workspaces"
    workspace_root.mkdir()
    edit_with_coding_agent(
        run,
        subject,
        run_status="task_failure",
        repository_root=tmp_path / "repo",
        candidate_store_root=candidate_root,
        runs_root=tmp_path / "runs",
        workspace_root=workspace_root,
        settings=CodexLaunchSettings(binary=sys.executable),
        runner=successful_codex_runner,
    )
    receipt_path = run.path / "optimizer/coding-agent/receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["full_skill_sha256"] = "f" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    result = materialize_coding_agent_edits(
        run,
        subject,
        candidate_service,
        "agent-001",
    )

    assert result.receipt.status == "invalid_binding"
    assert candidate_service.list_candidates("demo")[-1].candidate_id == "demo/parent"
