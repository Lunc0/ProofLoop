"""Fixed registry of trusted benchmark adapters.

This registry deliberately imports only repository-owned adapter code.  A data
directory is parsed as untrusted data by its adapter; it can never select an
import path, command, database URL, or runner argv.
"""

from __future__ import annotations

import re
from types import MappingProxyType

from skill_evolve.benchmark.contracts import BenchmarkAdapter, BenchmarkAdapterError
from skill_evolve.benchmark.livesqlbench_adapter import LiveSQLBenchBaseLiteAdapter

_ADAPTER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ADAPTERS: MappingProxyType[str, BenchmarkAdapter] = MappingProxyType(
    {LiveSQLBenchBaseLiteAdapter.adapter_id: LiveSQLBenchBaseLiteAdapter()}
)


def get_adapter(adapter_id: str) -> BenchmarkAdapter:
    """Return one explicitly registered adapter, or raise a CLI-safe error."""

    if _ADAPTER_ID_PATTERN.fullmatch(adapter_id) is None:
        raise BenchmarkAdapterError(
            "benchmark adapter ID must use lowercase letters, digits, and hyphens"
        )
    try:
        return _ADAPTERS[adapter_id]
    except KeyError as error:
        available = ", ".join(_ADAPTERS)
        raise BenchmarkAdapterError(
            f"unknown benchmark adapter {adapter_id!r}; available adapters: {available}"
        ) from error


def registered_adapter_ids() -> tuple[str, ...]:
    """Return the stable, explicitly registered adapter IDs."""

    return tuple(_ADAPTERS)
