"""Coverage for the fixed, repository-owned benchmark adapter registry."""

from __future__ import annotations

import pytest

from skill_evolve.benchmark.contracts import BenchmarkAdapterError
from skill_evolve.benchmark.livesqlbench_adapter import LiveSQLBenchBaseLiteAdapter
from skill_evolve.benchmark.livesqlbench_heldout_adapter import (
    LiveSQLBenchBaseLiteHeldOutAdapter,
)
from skill_evolve.benchmark.livesqlbench_rotation_adapter import (
    LiveSQLBenchBaseLiteEvolutionAdapter,
    LiveSQLBenchBaseLiteSmokeRotationAdapter,
)
from skill_evolve.benchmark.registry import get_adapter, registered_adapter_ids


def test_registry_resolves_only_static_repository_owned_adapters() -> None:
    smoke_adapter = get_adapter("livesqlbench-base-lite")
    heldout_adapter = get_adapter("livesqlbench-base-lite-query-final-v1")
    rolling_smoke_adapter = get_adapter("livesqlbench-base-lite-smoke-rotation-v1")
    evolution_adapter = get_adapter("livesqlbench-base-lite-evolution-v1")

    assert isinstance(smoke_adapter, LiveSQLBenchBaseLiteAdapter)
    assert isinstance(heldout_adapter, LiveSQLBenchBaseLiteHeldOutAdapter)
    assert isinstance(rolling_smoke_adapter, LiveSQLBenchBaseLiteSmokeRotationAdapter)
    assert isinstance(evolution_adapter, LiveSQLBenchBaseLiteEvolutionAdapter)
    assert registered_adapter_ids() == (
        "livesqlbench-base-lite",
        "livesqlbench-base-lite-query-final-v1",
        "livesqlbench-base-lite-smoke-rotation-v1",
        "livesqlbench-base-lite-evolution-v1",
    )


@pytest.mark.parametrize("adapter_id", ("LiveSQLBench", "bad_adapter", "unknown"))
def test_registry_rejects_invalid_and_unknown_ids(adapter_id: str) -> None:
    with pytest.raises(BenchmarkAdapterError):
        get_adapter(adapter_id)
