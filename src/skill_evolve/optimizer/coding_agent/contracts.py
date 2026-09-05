"""Strict contracts for the isolated Codex coding-agent optimizer path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BLOCK_ID_PATTERN = re.compile(r"^b[1-9][0-9]*(?:\.[1-9][0-9]*)*$")
_EVIDENCE_REF_PATTERN = re.compile(r"^evidence-[0-9]{3}-[0-9]{3}-[0-9a-f]{12}$")
_EDIT_ID_PATTERN = re.compile(r"^edit-[0-9]{3}$")
_SAFE_ID_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/-]{0,255}$")

CodingAgentStatus = Literal[
    "validated",
    "unavailable",
    "skipped_invalid_evidence",
    "skipped_no_failed_trajectories",
    "agent_failed",
    "invalid_output",
]
CodingAgentErrorCode = Literal[
    "workspace_root_missing",
    "unsafe_workspace_root",
    "codex_not_found",
    "codex_version_unsupported",
    "codex_version_invalid",
    "request_timeout",
    "process_failed",
    "input_tampered",
    "missing_output",
    "unsafe_output",
    "invalid_declaration",
    "invalid_skill",
    "invalid_binding",
    "locked_target",
    "overlapping_edits",
    "undeclared_change",
    "unexpected_error",
]


class StrictCodingAgentModel(BaseModel):
    """Reject coercion and unknown persisted fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _validate_hash(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("value must be a lowercase SHA-256 digest")
    return value


def _validate_text(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("text must be non-empty and contain no NUL")
    return value


class AtomicEditDeclaration(StrictCodingAgentModel):
    operation: Literal["replace_block"]
    target_block_id: str
    replacement_markdown: str = Field(min_length=1, max_length=131_072)
    hypothesis: str = Field(min_length=1, max_length=2_048)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("target_block_id")
    @classmethod
    def validate_block_id(cls, value: str) -> str:
        if _BLOCK_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("target_block_id has an invalid format")
        return value

    @field_validator("replacement_markdown", "hypothesis")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("evidence_refs must be unique")
        if any(_EVIDENCE_REF_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("evidence_ref has an invalid format")
        return values


class AtomicSkillEditDeclaration(StrictCodingAgentModel):
    schema_version: Literal["atomic-skill-edit-declaration-v1"] = (
        "atomic-skill-edit-declaration-v1"
    )
    edits: tuple[AtomicEditDeclaration, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_targets(self) -> Self:
        targets = [edit.target_block_id for edit in self.edits]
        if len(targets) != len(set(targets)):
            raise ValueError("atomic edits must not repeat a target block")
        return self


class AtomicEditSource(StrictCodingAgentModel):
    run_id: str
    parent_candidate_id: str
    parent_skill_sha256: str
    skill_map_sha256: str
    evidence_bundle_sha256: str
    locator_policy_version: Literal["skill-map-policy-v3"] = "skill-map-policy-v3"
    replay_policy_version: Literal["atomic-skill-replay-policy-v1"] = (
        "atomic-skill-replay-policy-v1"
    )
    invocation_receipt_sha256: str

    @field_validator("run_id", "parent_candidate_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if _SAFE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("source identifier has an invalid format")
        return value

    @field_validator(
        "parent_skill_sha256",
        "skill_map_sha256",
        "evidence_bundle_sha256",
        "invocation_receipt_sha256",
    )
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        return _validate_hash(value)


class BoundAtomicEdit(StrictCodingAgentModel):
    edit_id: str
    operation: Literal["replace_block"]
    target_block_id: str
    resolved_start_byte: int = Field(ge=0)
    resolved_end_byte: int = Field(gt=0)
    expected_before_sha256: str
    replacement_markdown: str = Field(min_length=1, max_length=131_072)
    replacement_sha256: str
    hypothesis: str = Field(min_length=1, max_length=2_048)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("edit_id")
    @classmethod
    def validate_edit_id(cls, value: str) -> str:
        if _EDIT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("edit_id has an invalid format")
        return value

    @field_validator("target_block_id")
    @classmethod
    def validate_block_id(cls, value: str) -> str:
        if _BLOCK_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("target_block_id has an invalid format")
        return value

    @field_validator("expected_before_sha256", "replacement_sha256")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        return _validate_hash(value)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.resolved_end_byte <= self.resolved_start_byte:
            raise ValueError("resolved byte range must be non-empty")
        if self.replacement_sha256 != _sha256(self.replacement_markdown.encode("utf-8")):
            raise ValueError("replacement_sha256 does not match replacement_markdown")
        return self


class BoundAtomicSkillEdits(StrictCodingAgentModel):
    schema_version: Literal["atomic-skill-edits-v1"] = "atomic-skill-edits-v1"
    source: AtomicEditSource
    edits: tuple[BoundAtomicEdit, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_edits(self) -> Self:
        expected_ids = tuple(f"edit-{index:03d}" for index in range(1, len(self.edits) + 1))
        if tuple(edit.edit_id for edit in self.edits) != expected_ids:
            raise ValueError("bound edit IDs must be canonical and ordered")
        targets = [edit.target_block_id for edit in self.edits]
        if len(targets) != len(set(targets)):
            raise ValueError("bound edits must not repeat a target block")
        return self


class CodingAgentInvocationReceipt(StrictCodingAgentModel):
    schema_version: Literal["coding-agent-invocation-receipt-v1"] = (
        "coding-agent-invocation-receipt-v1"
    )
    provider: Literal["openai-codex"] = "openai-codex"
    codex_cli_version: str
    model: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    input_sha256: str

    @field_validator("input_sha256")
    @classmethod
    def validate_input_hash(cls, value: str) -> str:
        return _validate_hash(value)

    @field_validator("codex_cli_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if re.fullmatch(r"\d+\.\d+\.\d+", value) is None:
            raise ValueError("codex_cli_version must be a semantic version")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value
        ) is None:
            raise ValueError("model has an invalid format")
        return value


class CodingAgentEditReceipt(StrictCodingAgentModel):
    schema_version: Literal["coding-agent-edit-receipt-v1"] = (
        "coding-agent-edit-receipt-v1"
    )
    status: CodingAgentStatus
    source: AtomicEditSource | None = None
    error_code: CodingAgentErrorCode | None = None
    atomic_edits_sha256: str | None = None
    full_skill_sha256: str | None = None
    edit_count: int = Field(default=0, ge=0, le=3)

    @field_validator("atomic_edits_sha256", "full_skill_sha256")
    @classmethod
    def validate_optional_hashes(cls, value: str | None) -> str | None:
        return _validate_hash(value) if value is not None else None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status == "validated":
            if (
                self.source is None
                or self.error_code is not None
                or self.atomic_edits_sha256 is None
                or self.full_skill_sha256 is None
                or self.edit_count < 1
            ):
                raise ValueError("validated receipt requires complete success bindings")
        elif self.error_code is None and self.status not in {
            "skipped_invalid_evidence",
            "skipped_no_failed_trajectories",
        }:
            raise ValueError("terminal failure receipt requires an error_code")
        return self


@dataclass(frozen=True)
class CodingAgentEditResult:
    receipt: CodingAgentEditReceipt
    atomic_edits_path: str | None = None


class CodingAgentMaterializationReceipt(StrictCodingAgentModel):
    schema_version: Literal["coding-agent-materialization-receipt-v1"] = (
        "coding-agent-materialization-receipt-v1"
    )
    status: Literal["created", "partial", "invalid_binding", "candidate_failed"]
    parent_candidate_id: str | None = None
    atomic_edits_sha256: str | None = None
    created_candidate_ids: tuple[str, ...] = ()
    error_code: Literal["invalid_binding", "candidate_failed"] | None = None

    @field_validator("atomic_edits_sha256")
    @classmethod
    def validate_atomic_hash(cls, value: str | None) -> str | None:
        return _validate_hash(value) if value is not None else None

    @model_validator(mode="after")
    def validate_materialization_status(self) -> Self:
        if self.status == "created" and (
            self.parent_candidate_id is None
            or self.atomic_edits_sha256 is None
            or not self.created_candidate_ids
            or self.error_code is not None
        ):
            raise ValueError("created materialization requires complete bindings")
        if self.status == "partial" and (
            not self.created_candidate_ids or self.error_code != "candidate_failed"
        ):
            raise ValueError("partial materialization requires created IDs and failure code")
        if self.status in {"invalid_binding", "candidate_failed"} and self.error_code is None:
            raise ValueError("failed materialization requires an error code")
        return self


@dataclass(frozen=True)
class CodingAgentMaterializationResult:
    receipt: CodingAgentMaterializationReceipt


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()
