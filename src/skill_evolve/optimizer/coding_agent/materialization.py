"""Materialize validated coding-agent edits as full and singleton draft siblings."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from skill_evolve.candidate import CandidateOrigin, CandidateService, CodingAgentProvenance
from skill_evolve.candidate.contracts import split_candidate_id
from skill_evolve.candidate.store import CandidateStoreError
from skill_evolve.optimizer.coding_agent.contracts import (
    BoundAtomicSkillEdits,
    CodingAgentEditReceipt,
    CodingAgentInvocationReceipt,
    CodingAgentMaterializationReceipt,
    CodingAgentMaterializationResult,
)
from skill_evolve.optimizer.coding_agent.replay import AtomicReplayError, replay_bound_edits
from skill_evolve.optimizer.evidence import optimizer_artifact_path
from skill_evolve.registry.store import RunDirectory
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import canonical_json_bytes

_ARTIFACT_ROOT = "optimizer/coding-agent"
_ModelT = TypeVar("_ModelT", bound=BaseModel)


def materialize_coding_agent_edits(
    run: RunDirectory,
    subject: RunSubject,
    candidate_service: CandidateService,
    candidate_name_prefix: str,
) -> CodingAgentMaterializationResult:
    """Create one full and N singleton candidates without invoking a model or benchmark."""

    _ensure_materialization_receipt_absent(run)
    try:
        edit_receipt = _read_model(run, "receipt.json", CodingAgentEditReceipt)
        if edit_receipt.status != "validated":
            raise ValueError("coding-agent edit receipt is not validated")
        plan_path = optimizer_artifact_path(run, f"{_ARTIFACT_ROOT}/bound/atomic-edits.json")
        invocation = _read_model(
            run, "invocation-receipt.json", CodingAgentInvocationReceipt
        )
        if plan_path is None:
            raise ValueError("bound atomic edits are missing")
        raw_plan = plan_path.read_bytes()
        plan = BoundAtomicSkillEdits.model_validate_json(raw_plan)
        canonical_plan = canonical_json_bytes(plan.model_dump(mode="json"))
        if edit_receipt.atomic_edits_sha256 != _sha256(canonical_plan):
            raise ValueError("bound atomic edits hash does not match receipt")
        if (
            plan.source.run_id != run.run_id
            or plan.source.parent_candidate_id != subject.subject_id
        ):
            raise ValueError("bound atomic edits source does not match run")
        invocation_bytes = canonical_json_bytes(invocation.model_dump(mode="json"))
        if plan.source.invocation_receipt_sha256 != _sha256(invocation_bytes):
            raise ValueError("invocation receipt hash does not match bound edits")
        parent = candidate_service.verify(plan.source.parent_candidate_id)
        if parent.skill_bytes != subject.content:
            raise ValueError("candidate parent does not match run subject")
        replay = replay_bound_edits(parent.skill_bytes, plan)
        if edit_receipt.full_skill_sha256 != _sha256(replay.full_skill_bytes):
            raise ValueError("replayed full Skill hash does not match edit receipt")
        roles: list[tuple[Literal["full", "singleton"], str | None, bytes, bytes]] = [
            ("full", None, replay.full_skill_bytes, replay.full_patch_bytes)
        ]
        roles.extend(
            (
                "singleton",
                edit.edit_id,
                replay.singleton_skill_bytes[edit.edit_id],
                replay.singleton_patch_bytes[edit.edit_id],
            )
            for edit in plan.edits
        )
        skill_id, _ = split_candidate_id(plan.source.parent_candidate_id)
        names = [
            f"{candidate_name_prefix}-full"
            if role == "full"
            else f"{candidate_name_prefix}-{edit_id}"
            for role, edit_id, _, _ in roles
        ]
        existing = {
            candidate.candidate_id for candidate in candidate_service.list_candidates(skill_id)
        }
        if any(f"{skill_id}/{name}" in existing for name in names):
            raise ValueError("one or more coding-agent candidate names already exist")
    except (OSError, ValueError, json.JSONDecodeError, ValidationError, CandidateStoreError):
        return _materialization_terminal(run, "invalid_binding", error_code="invalid_binding")
    except AtomicReplayError:
        return _materialization_terminal(run, "invalid_binding", error_code="invalid_binding")

    created: list[str] = []
    atomic_hash = _sha256(canonical_plan)
    for (role, edit_id, skill_bytes, patch_bytes), name in zip(roles, names, strict=True):
        try:
            with tempfile.TemporaryDirectory(prefix=".coding-agent-", dir=run.path) as temporary:
                source = Path(temporary) / "SKILL.md"
                source.write_bytes(skill_bytes)
                candidate = candidate_service.import_candidate(
                    skill_id,
                    name,
                    source,
                    parent_candidate_id=plan.source.parent_candidate_id,
                    source_run_id=run.run_id,
                    origin=CandidateOrigin.CODING_AGENT,
                    coding_agent_provenance=CodingAgentProvenance(
                        atomic_edits_sha256=atomic_hash,
                        patch_sha256=_sha256(patch_bytes),
                        parent_skill_sha256=plan.source.parent_skill_sha256,
                        skill_map_sha256=plan.source.skill_map_sha256,
                        evidence_bundle_sha256=plan.source.evidence_bundle_sha256,
                        candidate_role=role,
                        edit_id=edit_id,
                        codex_cli_version=invocation.codex_cli_version,
                        model=invocation.model,
                    ),
                )
                created.append(candidate.candidate_id)
        except (OSError, ValueError, CandidateStoreError):
            status = "partial" if created else "candidate_failed"
            return _materialization_terminal(
                run,
                status,
                error_code="candidate_failed",
                parent_candidate_id=plan.source.parent_candidate_id,
                atomic_edits_sha256=atomic_hash,
                created_candidate_ids=tuple(created),
            )

    receipt = CodingAgentMaterializationReceipt(
        status="created",
        parent_candidate_id=plan.source.parent_candidate_id,
        atomic_edits_sha256=atomic_hash,
        created_candidate_ids=tuple(created),
    )
    _write_materialization_receipt(run, receipt)
    return CodingAgentMaterializationResult(receipt=receipt)


def _read_model(run: RunDirectory, name: str, model: type[_ModelT]) -> _ModelT:
    path = optimizer_artifact_path(run, f"{_ARTIFACT_ROOT}/{name}")
    if path is None:
        raise ValueError("coding-agent artifact is missing")
    return model.model_validate_json(path.read_bytes())


def _materialization_terminal(
    run: RunDirectory,
    status: str,
    *,
    error_code: str,
    parent_candidate_id: str | None = None,
    atomic_edits_sha256: str | None = None,
    created_candidate_ids: tuple[str, ...] = (),
) -> CodingAgentMaterializationResult:
    receipt = CodingAgentMaterializationReceipt.model_validate(
        {
            "status": status,
            "error_code": error_code,
            "parent_candidate_id": parent_candidate_id,
            "atomic_edits_sha256": atomic_edits_sha256,
            "created_candidate_ids": created_candidate_ids,
        }
    )
    _write_materialization_receipt(run, receipt)
    return CodingAgentMaterializationResult(receipt=receipt)


def _write_materialization_receipt(
    run: RunDirectory, receipt: CodingAgentMaterializationReceipt
) -> None:
    destination = _materialization_receipt_path(run)
    with destination.open("xb") as output:
        output.write(canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n")


def _ensure_materialization_receipt_absent(run: RunDirectory) -> None:
    destination = run.path / _ARTIFACT_ROOT / "materialization-receipt.json"
    if os.path.lexists(destination):
        raise ValueError("coding-agent materialization receipt already exists")


def _materialization_receipt_path(run: RunDirectory) -> Path:
    run_root = run.path.resolve(strict=True)
    artifact_root = run.path / _ARTIFACT_ROOT
    if artifact_root.is_symlink():
        raise ValueError("unsafe coding-agent artifact root")
    resolved_root = artifact_root.resolve(strict=True)
    resolved_root.relative_to(run_root)
    if not resolved_root.is_dir():
        raise ValueError("coding-agent artifact root is not a directory")
    return resolved_root / "materialization-receipt.json"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
