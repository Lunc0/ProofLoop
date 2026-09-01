"""Strict, local-only contracts for Evolution optimization advice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

OptimizerStatus = Literal[
    "proposed",
    "skipped_no_failed_trajectories",
    "skipped_invalid_evidence",
    "model_failed",
    "invalid_response",
    "unavailable",
]
OptimizerErrorCode = Literal["missing_api_key", "request_failed", "invalid_response"]


class StrictOptimizerModel(BaseModel):
    """Reject unexpected fields in every persisted optimizer artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AdviceSource(StrictOptimizerModel):
    run_id: str = Field(min_length=1, max_length=128)
    agent_input_sha256: str
    evaluated_skill_sha256: str

    @field_validator("agent_input_sha256", "evaluated_skill_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 digest")
        return value


class EvolutionAgentInputManifest(StrictOptimizerModel):
    """The complete, optimizer-approved v3 manifest written by an Evolution run."""

    schema_version: Literal["evolution-coding-agent-input-v3"]
    exposed_for_optimization: Literal[True]
    manifest_sha256: str
    environment_fingerprint: str
    projection_rule_version: str
    agent_input_sha256: str
    included_case_count: int = Field(ge=0)
    omitted_case_count: int = Field(ge=0)

    @field_validator("manifest_sha256", "agent_input_sha256")
    @classmethod
    def validate_manifest_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 digest")
        return value


class AdviceSuggestion(StrictOptimizerModel):
    evidence_case_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    observation: str = Field(min_length=1, max_length=2_048)
    suggested_change: str = Field(min_length=1, max_length=2_048)
    suggested_markdown_fragment: str = Field(min_length=1, max_length=2_048)

    @field_validator("evidence_case_ids")
    @classmethod
    def validate_case_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("evidence_case_ids must not contain duplicates")
        if any(_CASE_ID_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("evidence_case_ids contain an invalid value")
        return values

    @field_validator("observation", "suggested_change", "suggested_markdown_fragment")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("advice text must not contain NUL")
        return value

    @field_validator("suggested_markdown_fragment")
    @classmethod
    def reject_full_skill_document(cls, value: str) -> str:
        if value.lstrip().startswith("# SKILL"):
            raise ValueError("suggested_markdown_fragment must not contain a full SKILL document")
        return value


class ModelAdvice(StrictOptimizerModel):
    """The limited JSON body that a model may return."""

    schema_version: Literal["skill-optimization-advice-v1"] = "skill-optimization-advice-v1"
    summary: str = Field(min_length=1, max_length=2_048)
    suggestions: tuple[AdviceSuggestion, ...] = Field(min_length=1, max_length=16)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("summary must not contain NUL")
        return value


class OptimizationAdvice(StrictOptimizerModel):
    """Trusted local binding plus a validated, still-untrusted model suggestion."""

    schema_version: Literal["skill-optimization-advice-v1"] = "skill-optimization-advice-v1"
    source: AdviceSource
    summary: str = Field(min_length=1, max_length=2_048)
    suggestions: tuple[AdviceSuggestion, ...] = Field(min_length=1, max_length=16)
    model_output_untrusted: Literal[True] = True


class ProviderUsage(StrictOptimizerModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class OptimizerReceipt(StrictOptimizerModel):
    """Minimal non-secret record of one automatic optimizer terminal state."""

    schema_version: Literal["skill-optimizer-receipt-v1"] = "skill-optimizer-receipt-v1"
    status: OptimizerStatus
    provider: Literal["deepseek"]
    model: Literal["deepseek-v4-flash"]
    prompt_version: Literal["skill-optimization-advice-prompt-v1"]
    source: AdviceSource | None = None
    advice_sha256: str | None = None
    usage: ProviderUsage | None = None
    error_code: OptimizerErrorCode | None = None

    @field_validator("advice_sha256")
    @classmethod
    def validate_optional_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("advice_sha256 must be a lowercase SHA-256 digest")
        return value


@dataclass(frozen=True)
class OptimizationResult:
    """In-memory result used by the benchmark orchestration layer."""

    receipt: OptimizerReceipt
    advice_path: str | None
