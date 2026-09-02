"""Trusted, non-secret optimizer target contracts."""

from __future__ import annotations

import hashlib

from skill_evolve.benchmark.livesqlbench_rotation import (
    LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
)
from skill_evolve.optimizer.edit_plan_contracts import SkillTargetContract
from skill_evolve.trajectory.compressor import canonical_json_bytes


def target_contract_for_adapter(adapter_id: str) -> tuple[SkillTargetContract, str]:
    """Return a fixed trusted contract for an explicitly supported adapter."""

    if adapter_id != LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID:
        raise ValueError("edit-plan target contract is unavailable for this adapter")
    contract = SkillTargetContract(
        adapter_id=adapter_id,
        available_capabilities=("sql-readonly",),
        sql_mutability="read_only",
        max_statements_per_call=1,
        task_context_sources=("schema", "knowledge", "column_meaning"),
        final_submission_contract="one_final_sql_statement",
    )
    contract_bytes = canonical_json_bytes(contract.model_dump(mode="json"))
    return contract, hashlib.sha256(contract_bytes).hexdigest()
