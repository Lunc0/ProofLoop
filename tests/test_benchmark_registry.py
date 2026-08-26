"""Coverage for the fixed, repository-owned benchmark adapter registry."""

from __future__ import annotations

import pytest

from skill_evolve.benchmark.contracts import BenchmarkAdapterError
from skill_evolve.benchmark.livesqlbench_adapter import LiveSQLBenchBaseLiteAdapter
from skill_evolve.benchmark.registry import get_adapter, registered_adapter_ids


def test_registry_resolves_only_the_static_livesqlbench_adapter() -> None:
    adapter = get_adapter("livesqlbench-base-lite")

    assert isinstance(adapter, LiveSQLBenchBaseLiteAdapter)
    assert registered_adapter_ids() == ("livesqlbench-base-lite",)


@pytest.mark.parametrize("adapter_id", ("LiveSQLBench", "bad_adapter", "unknown"))
def test_registry_rejects_invalid_and_unknown_ids(adapter_id: str) -> None:
    with pytest.raises(BenchmarkAdapterError):
        get_adapter(adapter_id)
