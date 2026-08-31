"""Strict public contracts for compressed DSH evidence and Evolution input."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictTrajectoryModel(BaseModel):
    """Reject unknown fields in persisted trajectory artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceText(StrictTrajectoryModel):
    """Bounded text from an untrusted session/tool source."""

    text: str
    sha256: str
    original_bytes: int = Field(ge=0)
    truncated: bool
    untrusted_evidence: Literal[True] = True


class SourceLocation(StrictTrajectoryModel):
    turn: int | None = Field(default=None, ge=0)
    step: int | None = Field(default=None, ge=0)
    seq: int = Field(ge=0)
    event_type: str
    observed_time_ms: int | None = Field(default=None, ge=0)


class DirectFailure(StrictTrajectoryModel):
    category: str
    direct_cause: str
    confidence: Literal["high"]
    evidence_fields: tuple[str, ...]


class CompressedToolCall(StrictTrajectoryModel):
    call_id: str
    name: str
    adapter: str
    call: SourceLocation
    result: SourceLocation | None = None
    arguments: EvidenceText
    output: EvidenceText | None = None
    status: Literal["succeeded", "failed", "unknown"]
    direct_failure: DirectFailure | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class CompressedStep(StrictTrajectoryModel):
    turn: int = Field(ge=0)
    step: int = Field(ge=0)
    public_assistant_text: tuple[EvidenceText, ...] = ()
    tool_calls: tuple[CompressedToolCall, ...] = ()


class OrphanToolResult(StrictTrajectoryModel):
    location: SourceLocation
    call_id: str
    output: EvidenceText | None = None


class TraceState(StrictTrajectoryModel):
    trace_completeness: Literal["complete", "partial_actionable", "partial_insufficient"]
    runner_outcome: str
    deadline_ms: int | None = Field(default=None, ge=0)
    last_observed: SourceLocation | None = None
    missing_terminal_events: tuple[str, ...] = ()


class TrajectorySignal(StrictTrajectoryModel):
    kind: str
    count: int = Field(ge=1)
    call_ids: tuple[str, ...] = ()
    detail: dict[str, Any] = Field(default_factory=dict)


class CandidateOutput(StrictTrajectoryModel):
    sql: EvidenceText | None = None
    final_message: EvidenceText | None = None


class PublicEvaluation(StrictTrajectoryModel):
    case_id: str
    slice: str
    verdict: str
    candidate_sql: EvidenceText | None = None
    detail: EvidenceText | None = None
    usage: dict[str, int | float | None]


class CompressionStats(StrictTrajectoryModel):
    raw_event_count: int = Field(ge=0)
    raw_bytes: int = Field(ge=0)
    compressed_bytes: int = Field(ge=0)
    artifact_sha256: str
    truncated_fields: tuple[str, ...] = ()
    unhandled_event_type_counts: dict[str, int] = Field(default_factory=dict)
    orphan_result_count: int = Field(default=0, ge=0)
    missing_result_count: int = Field(default=0, ge=0)


class TrajectorySource(StrictTrajectoryModel):
    redacted_session_sha256: str
    session_version: int = Field(ge=0)
    compressor_rule_version: str
    compressor_policy_sha256: str
    evidence_ref: str


class CompressedTrajectory(StrictTrajectoryModel):
    schema_version: Literal["dsh-trajectory-compressed-v1"] = "dsh-trajectory-compressed-v1"
    source: TrajectorySource
    trace: TraceState
    trajectory: tuple[CompressedStep, ...]
    orphan_tool_results: tuple[OrphanToolResult, ...] = ()
    signals: tuple[TrajectorySignal, ...] = ()
    candidate_output: CandidateOutput
    evaluation: PublicEvaluation
    compression: CompressionStats


class TrustedContext(StrictTrajectoryModel):
    role: Literal["schema", "knowledge", "column_meaning"]
    sha256: str
    text: str
    original_bytes: int = Field(ge=0)
    truncated: bool


class TrustedText(StrictTrajectoryModel):
    """Bounded text supplied by a benchmark-controlled public input."""

    text: str
    sha256: str
    original_bytes: int = Field(ge=0)
    truncated: bool


class EvolutionCaseInput(StrictTrajectoryModel):
    case_id: str
    prompt: TrustedText
    task_contract: str
    difficulty_tier: str
    context: tuple[TrustedContext, ...]
    trajectory: CompressedTrajectory


class EvolutionAgentInput(StrictTrajectoryModel):
    schema_version: Literal["evolution-coding-agent-input-v1"] = "evolution-coding-agent-input-v1"
    exposed_for_optimization: Literal[True] = True
    manifest_sha256: str
    environment_fingerprint: str
    subject: dict[str, str]
    skill: EvidenceText
    trajectories: tuple[EvolutionCaseInput, ...]
    omitted_cases: tuple[dict[str, str], ...] = ()
    policy_sha256: str
