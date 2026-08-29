"""Stable boundary between an agent harness and the evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from skill_evolve.config.models import CaseConfig
from skill_evolve.subject import RunSubject


class RunnerError(RuntimeError):
    """The harness did not provide trustworthy case-level evidence."""


class StrictRunnerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrajectoryEvent(StrictRunnerModel):
    timestamp: str
    type: str
    payload: Any


class Usage(StrictRunnerModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class AgentExecution(StrictRunnerModel):
    status: Literal["ok", "error"]
    result: Any | None = None
    error: str | None = None


class RunnerCaseOutput(StrictRunnerModel):
    case_id: str
    status: Literal["completed", "runner_error"]
    final_message: str | None = None
    candidate_sql: str | None = None
    trajectory: tuple[TrajectoryEvent, ...]
    agent_execution: AgentExecution | None = None
    usage: Usage = Field(default_factory=Usage)


class RunnerOutput(StrictRunnerModel):
    schema_version: Literal["v1"]
    cases: tuple[RunnerCaseOutput, ...]


@dataclass(frozen=True)
class ReadOnlyDatabaseAccess:
    """Non-secret capability for DSH to inspect a disposable exploration database."""

    query_endpoint: str


@dataclass(frozen=True)
class RunInput:
    run_id: str
    workspace: Path
    environment_fingerprint: str
    subject_ref: str | None
    cases: tuple[CaseConfig, ...]
    schema_file: Path | None = None
    subject: RunSubject | None = None
    case_context_files: tuple[tuple[str, tuple[Path, ...]], ...] = ()
    agent_workspace_root: Path | None = None
    database_access: ReadOnlyDatabaseAccess | None = None

    def __post_init__(self) -> None:
        if self.subject is None:
            if self.subject_ref is not None:
                raise ValueError("subject_ref requires a RunSubject")
            return
        if self.subject_ref != self.subject.subject_id:
            raise ValueError("subject_ref must match RunSubject.subject_id")

    def context_files_for(self, case_id: str) -> tuple[Path, ...]:
        for configured_case_id, paths in self.case_context_files:
            if configured_case_id == case_id:
                return paths
        return ()


@dataclass(frozen=True)
class RawArtifact:
    """A redacted, runner-produced file to retain below a run's ``raw/`` directory."""

    relative_path: str
    content: bytes

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or "\\" in self.relative_path
            or path.is_absolute()
            or ".." in path.parts
            or path == PurePosixPath(".")
        ):
            raise ValueError("raw artifact path must be a non-empty relative POSIX path")


@dataclass(frozen=True)
class RunnerExecution:
    output: RunnerOutput
    raw_output: bytes
    stderr: str = ""
    raw_artifacts: tuple[RawArtifact, ...] = ()


class Runner(Protocol):
    """A runner must provide evidence for every requested case."""

    def run(self, input: RunInput) -> RunnerExecution: ...
