"""Public, GT-safe contracts for explicitly registered PostgreSQL benchmarks.

The objects in this module deliberately expose only command-boundary data.
Adapters may retain benchmark-specific case objects and Gold material in local
process memory, but those types never cross the registry or CLI boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias

from skill_evolve.config.models import DshConfig, ModelConfig
from skill_evolve.runner.contracts import RunnerOutput

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class BenchmarkAdapterError(ValueError):
    """A registered benchmark cannot safely process the supplied input."""


class BenchmarkFixtureError(RuntimeError):
    """A disposable local fixture could not provide evaluation evidence."""


class BenchmarkSuite(Protocol):
    """The public shape of a loaded suite.

    Concrete suites can retain private Gold data, but the contract offers only
    an adapter identity, public metadata, and digests of local source files.
    """

    @property
    def adapter_id(self) -> str: ...

    def public_summary(self) -> JsonObject: ...

    def source_digests(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class BenchmarkRunResult:
    """Public terminal metadata for an adapter-owned DSH run."""

    run_id: str
    status: str
    attribution: str
    report_path: Path
    redacted_session_logs_path: Path
    exit_code: int


class BenchmarkAdapter(Protocol):
    """Trusted repository-code adapter invoked by the public benchmark CLI."""

    adapter_id: str

    def load_suite(self, data_root: Path) -> BenchmarkSuite: ...

    def public_summary(self, suite: BenchmarkSuite) -> JsonObject: ...

    def fixture_check(self, suite: BenchmarkSuite, image: str) -> JsonObject: ...

    def evaluate(
        self, suite: BenchmarkSuite, runner_output: RunnerOutput, image: str
    ) -> JsonObject: ...

    def run_dsh(
        self,
        suite: BenchmarkSuite,
        dsh_config: DshConfig,
        profile_patch: Path | None,
        model: ModelConfig,
        runs_root: Path,
    ) -> BenchmarkRunResult: ...
