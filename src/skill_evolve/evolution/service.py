"""Sequential, fail-closed orchestration for an explicit local Evolution cycle."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from skill_evolve.benchmark.livesqlbench_rotation import (
    LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
    RotationError,
    create_rotation_manifest,
    initialise_rotation_reserve,
    load_rotation_reserve,
    load_rotation_suite,
    resolve_rotation_custody,
)
from skill_evolve.benchmark.livesqlbench_rotation_run import (
    meets_smoke_gate_policy,
    run_rotation_dsh_suite,
)
from skill_evolve.candidate import (
    CandidateService,
    CandidateStoreError,
    ResolvedCandidate,
    subject_from_candidate,
)
from skill_evolve.candidate.contracts import split_candidate_id
from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.config.models import DshConfig, ModelConfig, OptimizerConfig
from skill_evolve.evolution.contracts import (
    CycleStopReason,
    EvolutionCycleError,
    EvolutionCycleReceipt,
    EvolutionCycleRequest,
    EvolutionGenerationReceipt,
)
from skill_evolve.optimizer.contracts import OptimizationResult
from skill_evolve.optimizer.edit_plan_contracts import EditPlanResult
from skill_evolve.optimizer.edit_plan_service import plan_after_evolution
from skill_evolve.optimizer.materialization_contracts import MaterializationResult
from skill_evolve.optimizer.materialization_service import materialize_after_evolution
from skill_evolve.optimizer.service import advise_after_evolution
from skill_evolve.registry import RunDirectory


def run_evolution_cycle(
    request: EvolutionCycleRequest,
    data_root: Path,
    dsh_manifest: Path,
    *,
    candidate_service: CandidateService,
    repository_root: Path,
    candidate_store_root: Path,
    runs_root: Path,
    cycle_receipts_root: Path,
) -> EvolutionCycleReceipt:
    """Run at most ``max_generations`` without implicit retries or promotion."""

    try:
        resolved_manifest = load_manifest(dsh_manifest)
        disabled_optimizer = resolved_manifest.manifest.environment.optimizer
        if disabled_optimizer.enabled:
            raise EvolutionCycleError(
                "evolution cycle requires environment.optimizer.enabled: false"
            )
        initial_candidate = candidate_service.verify(request.candidate_id)
        _preflight_child_names(request, candidate_service)
        _ensure_rotation_reserve(data_root, repository_root, candidate_store_root)
    except (CandidateStoreError, ManifestError, RotationError, ValueError) as error:
        if isinstance(error, EvolutionCycleError):
            raise
        raise EvolutionCycleError(str(error)) from error

    cycle_id = _cycle_id()
    current_candidate = initial_candidate
    generations: list[EvolutionGenerationReceipt] = []
    enabled_optimizer = disabled_optimizer.model_copy(update={"enabled": True})

    for generation in range(1, request.max_generations + 1):
        receipt = _run_generation(
            generation,
            current_candidate,
            request,
            data_root,
            resolved_manifest.profile_patch,
            resolved_manifest.manifest.environment.dsh,
            resolved_manifest.manifest.environment.model,
            disabled_optimizer,
            enabled_optimizer,
            candidate_service,
            repository_root,
            candidate_store_root,
            runs_root,
        )
        generations.append(receipt)
        if receipt.child_candidate_id is None:
            return _finish_cycle(
                cycle_id,
                request.candidate_id,
                current_candidate.candidate_id,
                _stop_reason_for(receipt),
                tuple(generations),
                cycle_receipts_root,
            )
        try:
            current_candidate = candidate_service.verify(receipt.child_candidate_id)
        except (CandidateStoreError, ValueError) as error:
            raise EvolutionCycleError(
                "materialized child candidate could not be verified"
            ) from error

    return _finish_cycle(
        cycle_id,
        request.candidate_id,
        current_candidate.candidate_id,
        "completed",
        tuple(generations),
        cycle_receipts_root,
    )


def _run_generation(
    generation: int,
    candidate: ResolvedCandidate,
    request: EvolutionCycleRequest,
    data_root: Path,
    profile_patch: Path | None,
    dsh_config: DshConfig,
    model: ModelConfig,
    disabled_optimizer: OptimizerConfig,
    enabled_optimizer: OptimizerConfig,
    candidate_service: CandidateService,
    repository_root: Path,
    candidate_store_root: Path,
    runs_root: Path,
) -> EvolutionGenerationReceipt:
    subject = subject_from_candidate(candidate)
    baseline_exempt = generation == 1 and candidate.metadata.parent_candidate_id is None
    receipt = EvolutionGenerationReceipt(generation=generation, candidate_id=candidate.candidate_id)

    if not baseline_exempt:
        smoke_manifest = create_rotation_manifest(
            data_root,
            "smoke",
            repository_root=repository_root,
            candidate_store_root=candidate_store_root,
        )
        smoke_suite = load_rotation_suite(
            data_root,
            smoke_manifest.manifest_id,
            repository_root=repository_root,
            candidate_store_root=candidate_store_root,
        )
        smoke_outcome = run_rotation_dsh_suite(
            smoke_suite,
            dsh_config,
            profile_patch,
            model,
            runs_root,
            subject,
            disabled_optimizer,
        )
        receipt = replace(
            receipt,
            smoke_manifest_sha256=smoke_manifest.manifest_sha256,
            smoke_run_id=smoke_outcome.run.run_id,
            smoke_status=smoke_outcome.status,
        )
        if not meets_smoke_gate_policy(smoke_outcome.status, smoke_outcome.evaluations):
            return receipt

    evolution_manifest = create_rotation_manifest(
        data_root,
        "evolution",
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
    )
    evolution_suite = load_rotation_suite(
        data_root,
        evolution_manifest.manifest_id,
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
    )
    evolution_outcome = run_rotation_dsh_suite(
        evolution_suite,
        dsh_config,
        profile_patch,
        model,
        runs_root,
        subject,
        disabled_optimizer,
        allow_ungated_evolution=baseline_exempt,
    )
    receipt = replace(
        receipt,
        evolution_manifest_sha256=evolution_manifest.manifest_sha256,
        evolution_run_id=evolution_outcome.run.run_id,
        evolution_status=evolution_outcome.status,
        evolution_evaluable_case_count=getattr(evolution_outcome, "evaluable_case_count", None),
        evolution_discarded_evaluator_invalid_count=getattr(
            evolution_outcome, "discarded_evaluator_invalid_count", None
        ),
    )
    if evolution_outcome.status == "evaluator_invalid":
        return receipt
    if evolution_outcome.status == "partial_evaluator_invalid" and getattr(
        evolution_outcome, "optimizable_failure_count", 0
    ) == 0:
        return receipt

    advice = advise_after_evolution(
        evolution_outcome.run,
        subject,
        enabled_optimizer,
        run_status=evolution_outcome.status,
    )
    _record_advice(evolution_outcome.run, advice)
    receipt = replace(receipt, advice_status=advice.receipt.status)
    if advice.receipt.status != "proposed":
        return receipt

    plan = plan_after_evolution(
        evolution_outcome.run,
        subject,
        enabled_optimizer,
        run_status=evolution_outcome.status,
        adapter_id=LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
    )
    _record_plan(evolution_outcome.run, plan)
    receipt = replace(receipt, plan_status=plan.receipt.status)
    if plan.receipt.status != "proposed":
        return receipt

    materialization = materialize_after_evolution(
        evolution_outcome.run,
        subject,
        candidate_service,
        request.child_name_for_generation(generation),
    )
    _record_materialization(evolution_outcome.run, materialization)
    return replace(
        receipt,
        materialization_status=materialization.receipt.status,
        child_candidate_id=materialization.receipt.child_candidate_id,
    )


def _ensure_rotation_reserve(
    data_root: Path, repository_root: Path, candidate_store_root: Path
) -> None:
    custody = resolve_rotation_custody(
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
        require_write=True,
    )
    if os.path.lexists(custody.reserve_path):
        load_rotation_reserve(custody)
        return
    initialise_rotation_reserve(
        data_root,
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
    )


def _preflight_child_names(
    request: EvolutionCycleRequest, candidate_service: CandidateService
) -> None:
    skill_id, _ = split_candidate_id(request.candidate_id)
    existing_ids = {
        candidate.candidate_id for candidate in candidate_service.list_candidates(skill_id)
    }
    for generation in range(1, request.max_generations + 1):
        child_id = f"{skill_id}/{request.child_name_for_generation(generation)}"
        if child_id in existing_ids:
            raise EvolutionCycleError(f"candidate already exists: {child_id}")


def _stop_reason_for(receipt: EvolutionGenerationReceipt) -> CycleStopReason:
    if receipt.smoke_status is not None and receipt.evolution_run_id is None:
        return "smoke_gate_not_met"
    if receipt.evolution_status == "evaluator_invalid":
        return "evolution_invalid"
    if receipt.evolution_status == "partial_evaluator_invalid" and receipt.advice_status is None:
        return "no_optimizable_failure"
    if receipt.advice_status != "proposed":
        return "advice_not_proposed"
    if receipt.plan_status != "proposed":
        return "plan_not_proposed"
    return "materialization_not_created"


def _record_advice(run: RunDirectory, advice: OptimizationResult) -> None:
    result = _read_result(run)
    receipt = advice.receipt
    result["optimizer"] = {
        "status": receipt.status,
        "advice_path": advice.advice_path,
        "provider": receipt.provider,
        "model": receipt.model,
        "prompt_version": receipt.prompt_version,
    }
    run.write_json("result.json", result)


def _record_plan(run: RunDirectory, plan: EditPlanResult) -> None:
    result = _read_result(run)
    receipt = plan.receipt
    result["optimizer_edit_plan"] = {
        "status": receipt.status,
        "plan_path": plan.plan_path,
        "skill_map_path": plan.skill_map_path,
        "provider": receipt.provider,
        "model": receipt.model,
        "prompt_version": receipt.prompt_version,
        "plan_sha256": receipt.plan_sha256,
        "skill_map_sha256": receipt.skill_map_sha256,
        "cluster_count": receipt.cluster_count,
        "edit_count": receipt.edit_count,
    }
    run.write_json("result.json", result)


def _record_materialization(run: RunDirectory, materialization: MaterializationResult) -> None:
    result = _read_result(run)
    receipt = materialization.receipt
    result["optimizer_materialization"] = {
        "status": receipt.status,
        "patch_path": materialization.patch_path,
        "parent_candidate_id": receipt.source.parent_candidate_id if receipt.source else None,
        "child_candidate_id": receipt.child_candidate_id,
        "child_skill_sha256": receipt.child_skill_sha256,
        "patch_sha256": receipt.patch_sha256,
        "edit_count": receipt.edit_count,
    }
    run.write_json("result.json", result)


def _read_result(run: RunDirectory) -> dict[str, object]:
    try:
        result = json.loads((run.path / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvolutionCycleError("Evolution result could not be read") from error
    if not isinstance(result, dict):
        raise EvolutionCycleError("Evolution result must be a JSON object")
    return cast(dict[str, object], result)


def _finish_cycle(
    cycle_id: str,
    initial_candidate_id: str,
    final_candidate_id: str,
    stop_reason: CycleStopReason,
    generations: tuple[EvolutionGenerationReceipt, ...],
    cycle_receipts_root: Path,
) -> EvolutionCycleReceipt:
    receipt = EvolutionCycleReceipt(
        cycle_id=cycle_id,
        initial_candidate_id=initial_candidate_id,
        final_candidate_id=final_candidate_id,
        stop_reason=stop_reason,
        generations=generations,
    )
    _persist_receipt(cycle_receipts_root, receipt)
    return receipt


def _persist_receipt(root: Path, receipt: EvolutionCycleReceipt) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise EvolutionCycleError("cycle receipt root is unsafe")
    destination = root / f"{receipt.cycle_id}.json"
    if destination.exists() or destination.is_symlink():
        raise EvolutionCycleError("cycle receipt already exists")
    payload = json.dumps(receipt.public_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=root, prefix=".cycle-", delete=False
    ) as temporary:
        temporary.write(payload)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, destination)
    except OSError as error:
        temporary_path.unlink(missing_ok=True)
        raise EvolutionCycleError("cycle receipt could not be persisted") from error


def _cycle_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"
