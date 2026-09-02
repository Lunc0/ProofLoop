"""Trusted optimizer target-contract coverage."""

from __future__ import annotations

import hashlib

import pytest

from skill_evolve.benchmark import LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID
from skill_evolve.optimizer.target_contract import target_contract_for_adapter
from skill_evolve.trajectory.compressor import canonical_json_bytes


def test_evolution_target_contract_is_fixed_and_hash_bound() -> None:
    contract, digest = target_contract_for_adapter(
        LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID
    )

    assert contract.available_capabilities == ("sql-readonly",)
    assert contract.sql_mutability == "read_only"
    assert contract.max_statements_per_call == 1
    assert digest == hashlib.sha256(
        canonical_json_bytes(contract.model_dump(mode="json"))
    ).hexdigest()
    serialized = contract.model_dump_json()
    assert "endpoint" not in serialized
    assert "credential" not in serialized
    assert "path" not in serialized


def test_unknown_adapter_has_no_target_contract() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        target_contract_for_adapter("other-adapter")
