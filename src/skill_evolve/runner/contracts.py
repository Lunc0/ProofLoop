"""Stable boundary between an agent harness and the evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from skill_evolve.config.models import CaseConfig


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
class RunInput:
    run_id: str
    workspace: Path
    environment_fingerprint: str
    candidate_ref: str | None
    cases: tuple[CaseConfig, ...]


@dataclass(frozen=True)
class RunnerExecution:
    output: RunnerOutput
    raw_output: bytes
    stderr: str = ""


class Runner(Protocol):
    """A runner must provide evidence for every requested case."""

    def run(self, input: RunInput) -> RunnerExecution: ...
