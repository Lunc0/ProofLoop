"""Explicit, no-model conversion of one EditPlan into an immutable child Skill."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from pydantic import ValidationError

from skill_evolve.candidate import (
    CandidateOrigin,
    CandidateService,
    OptimizerProvenance,
)
from skill_evolve.candidate.contracts import split_candidate_id
from skill_evolve.candidate.store import CandidateStoreError
from skill_evolve.optimizer.edit_plan_contracts import (
    SkillEditPlan,
    SkillEditPlanV2,
    normalise_historical_edit_plan_status,
)
from skill_evolve.optimizer.edit_plan_renderer import (
    EditPlanRenderError,
    render_edit_plan,
)
from skill_evolve.optimizer.materialization_contracts import (
    MaterializationReceipt,
    MaterializationResult,
    MaterializationSource,
    MaterializationStatus,
)
from skill_evolve.registry.store import RunDirectory
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import canonical_json_bytes

_ARTIFACT_DIRECTORY = "optimizer/materialization"


def materialize_after_evolution(
    run: RunDirectory,
    subject: RunSubject,
    candidate_service: CandidateService,
    candidate_name: str,
) -> MaterializationResult:
    """Materialize one valid local plan without rerunning any benchmark or model."""

    source: MaterializationSource | None = None
    try:
        raw_plan, raw_plan_bytes = _read_plan(run)
        source = _source_for_plan(raw_plan, raw_plan_bytes, run, subject)
        plan = _normalise_plan(raw_plan)
        if plan.status == "findings_only" or not plan.edits:
            return _terminal(run, "skipped_no_actionable_plan", source=source)
        parent = candidate_service.verify(plan.source.parent_candidate_id)
        if (
            parent.skill_bytes != subject.content
            or parent.metadata.skill_sha256 != subject.content_sha256
        ):
            return _terminal(run, "invalid_binding", source=source, error_code="parent_mismatch")
        rendered = render_edit_plan(parent.skill_bytes, plan)
    except EditPlanRenderError as error:
        return _terminal(run, "render_failed", source=source, error_code=_safe_error_code(error))
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        ValidationError,
        CandidateStoreError,
    ) as error:
        return _terminal(run, "invalid_binding", source=source, error_code=_safe_error_code(error))

    skill_id, _ = split_candidate_id(plan.source.parent_candidate_id)
    patch_sha256 = _sha256(rendered.patch_bytes)
    try:
        with tempfile.TemporaryDirectory(prefix=".materialize-", dir=run.path) as temporary:
            skill_source = Path(temporary) / "SKILL.md"
            skill_source.write_bytes(rendered.child_bytes)
            child = candidate_service.import_candidate(
                skill_id,
                candidate_name,
                skill_source,
                parent_candidate_id=plan.source.parent_candidate_id,
                source_run_id=run.run_id,
                origin=CandidateOrigin.OPTIMIZER,
                optimizer_provenance=OptimizerProvenance(
                    edit_plan_sha256=source.edit_plan_sha256,
                    patch_sha256=patch_sha256,
                    parent_skill_sha256=plan.source.parent_skill_sha256,
                    render_policy_version=rendered.render_policy_version,
                ),
            )
    except (OSError, ValueError, CandidateStoreError) as error:
        return _terminal(run, "candidate_failed", source=source, error_code=_safe_error_code(error))

    receipt = MaterializationReceipt(
        status="created",
        source=source,
        child_candidate_id=child.metadata.candidate_id,
        child_skill_sha256=child.metadata.skill_sha256,
        patch_sha256=patch_sha256,
        edit_count=len(plan.edits),
    )
    try:
        _publish_artifacts(
            run,
            {
                "rendered.patch": rendered.patch_bytes,
                "receipt.json": canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n",
            },
        )
    except (OSError, ValueError) as error:
        raise RuntimeError(
            f"child was created but materialization evidence could not be published: {error}"
        ) from error
    return MaterializationResult(
        receipt=receipt,
        patch_path=f"{_ARTIFACT_DIRECTORY}/rendered.patch",
    )


def _read_plan(run: RunDirectory) -> tuple[dict[str, object], bytes]:
    path = _run_artifact_path(run, "optimizer/edit-plan/edit-plan.json")
    raw_bytes = path.read_bytes()
    value = json.loads(raw_bytes)
    if not isinstance(value, dict):
        raise ValueError("edit plan must be a JSON object")
    receipt_value = json.loads(
        _run_artifact_path(run, "optimizer/edit-plan/receipt.json").read_bytes()
    )
    if not isinstance(receipt_value, dict) or not isinstance(receipt_value.get("status"), str):
        raise ValueError("edit plan receipt is invalid")
    return value, raw_bytes


def _source_for_plan(
    raw_plan: dict[str, object], raw_plan_bytes: bytes, run: RunDirectory, subject: RunSubject
) -> MaterializationSource:
    source_value = raw_plan.get("source")
    if not isinstance(source_value, dict):
        raise ValueError("edit plan source is invalid")
    edit_plan_sha256 = _sha256(canonical_json_bytes(raw_plan))
    source = MaterializationSource(
        run_id=_required_string(source_value, "run_id"),
        parent_candidate_id=_required_string(source_value, "parent_candidate_id"),
        parent_skill_sha256=_required_string(source_value, "parent_skill_sha256"),
        edit_plan_sha256=edit_plan_sha256,
        target_contract_sha256=_required_string(source_value, "target_contract_sha256"),
    )
    if source.run_id != run.run_id:
        raise ValueError("edit plan run ID does not match the requested run")
    if source.parent_candidate_id != subject.subject_id:
        raise ValueError("edit plan parent candidate does not match the run subject")
    if source.parent_skill_sha256 != subject.content_sha256:
        raise ValueError("edit plan parent Skill hash does not match the run subject")
    if not raw_plan_bytes:
        raise ValueError("edit plan must not be empty")
    return source


def _normalise_plan(raw_plan: dict[str, object]) -> SkillEditPlan | SkillEditPlanV2:
    if raw_plan.get("schema_version") == "skill-edit-plan-v2":
        return SkillEditPlanV2.model_validate(raw_plan)
    normalised = dict(raw_plan)
    normalised["status"] = normalise_historical_edit_plan_status(normalised.get("status"))
    return SkillEditPlan.model_validate(normalised)


def _terminal(
    run: RunDirectory,
    status: MaterializationStatus,
    *,
    source: MaterializationSource | None,
    error_code: str | None = None,
) -> MaterializationResult:
    receipt = MaterializationReceipt(
        status=status,
        source=source,
        error_code=error_code,
    )
    _publish_terminal_receipt(run, receipt)
    return MaterializationResult(receipt=receipt, patch_path=None)


def _publish_terminal_receipt(run: RunDirectory, receipt: MaterializationReceipt) -> None:
    try:
        _publish_artifacts(
            run,
            {"receipt.json": canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"},
        )
    except (OSError, ValueError):
        return


def _publish_artifacts(run: RunDirectory, files: dict[str, bytes]) -> None:
    optimizer_path = run.path / "optimizer"
    if optimizer_path.is_symlink():
        raise ValueError("optimizer artifact directory must not be a symlink")
    optimizer_path.mkdir(exist_ok=True)
    target = optimizer_path / "materialization"
    if target.exists() or target.is_symlink():
        raise ValueError("materialization artifacts already exist")
    temporary = optimizer_path / f".materialization-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        for name, content in files.items():
            if Path(name).name != name:
                raise ValueError("materialization artifact names must be single path components")
            destination = temporary / name
            destination.write_bytes(content)
            if destination.read_bytes() != content:
                raise OSError("materialization artifact verification failed")
        os.replace(temporary, target)
    except Exception:
        if temporary.exists():
            for child in temporary.iterdir():
                child.unlink()
            temporary.rmdir()
        raise


def _run_artifact_path(run: RunDirectory, relative_path: str) -> Path:
    path = run.path / relative_path
    if path.is_symlink():
        raise ValueError(f"{relative_path} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(run.path.resolve())
    except (OSError, ValueError) as error:
        raise ValueError(f"{relative_path} must be a local run artifact") from error
    if not resolved.is_file():
        raise ValueError(f"{relative_path} must be a file")
    return resolved


def _safe_error_code(error: Exception) -> str:
    return type(error).__name__.lower()


def _required_string(value: dict[object, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str):
        raise ValueError(f"edit plan source {key} is invalid")
    return field


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
