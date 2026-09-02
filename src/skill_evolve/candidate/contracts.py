"""Strict, non-executable contracts for local SQL Skill candidates."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_CANDIDATE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")


class CandidateContractError(ValueError):
    """Candidate metadata violates its public, local-storage contract."""


class StrictCandidateModel(BaseModel):
    """Reject implicit coercion and unknown persisted fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CandidateStatus(StrEnum):
    DRAFT = "draft"
    EVALUATED = "evaluated"
    SELECTED = "selected"
    RETIRED = "retired"


class CandidateOrigin(StrEnum):
    MANUAL = "manual"
    EXTERNAL_AGENT = "external_agent"
    OPTIMIZER = "optimizer"


class ExternalSource(StrictCandidateModel):
    """Immutable provenance required for a candidate imported from outside this project."""

    canonical_url: str
    source_repository: str
    commit: str
    file_sha256: str
    license_id: str

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("canonical_url must be an https URL without query or fragment")
        return value

    @field_validator("source_repository")
    @classmethod
    def validate_source_repository(cls, value: str) -> str:
        parts = value.split("/")
        if len(parts) != 2 or not all(_SKILL_ID_PATTERN.fullmatch(part) for part in parts):
            raise ValueError("source_repository must be a safe owner/repository identifier")
        return value

    @field_validator("commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if not _COMMIT_PATTERN.fullmatch(value):
            raise ValueError("commit must be a lowercase 40-character Git SHA")
        return value

    @field_validator("file_sha256")
    @classmethod
    def validate_file_sha256(cls, value: str) -> str:
        _validate_sha256(value, "file_sha256")
        return value

    @field_validator("license_id")
    @classmethod
    def validate_license_id(cls, value: str) -> str:
        if value != "MIT":
            raise ValueError("only the MIT license is supported for external candidate sources")
        return value


class OptimizerProvenance(StrictCandidateModel):
    """Non-secret lineage for a child rendered from one semantic edit plan."""

    edit_plan_sha256: str
    patch_sha256: str
    parent_skill_sha256: str
    render_policy_version: str

    @field_validator("edit_plan_sha256", "patch_sha256", "parent_skill_sha256")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        _validate_sha256(value, "optimizer provenance hash")
        return value

    @field_validator("render_policy_version")
    @classmethod
    def validate_render_policy(cls, value: str) -> str:
        if value != "skill-edit-render-policy-v1":
            raise ValueError("unsupported optimizer render policy")
        return value


class CandidateMetadata(StrictCandidateModel):
    """Immutable metadata written beside one candidate's ``SKILL.md``."""

    schema_version: str = "v1"
    candidate_id: str
    skill_id: str
    parent_candidate_id: str | None = None
    created_at: datetime
    source_run_id: str | None = None
    skill_sha256: str
    origin: CandidateOrigin
    status: CandidateStatus = CandidateStatus.DRAFT
    external_source: ExternalSource | None = None
    optimizer_provenance: OptimizerProvenance | None = None

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "v1":
            raise ValueError("unsupported candidate metadata schema_version")
        return value

    @field_validator("skill_id")
    @classmethod
    def validate_skill_id(cls, value: str) -> str:
        validate_skill_id(value)
        return value

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        split_candidate_id(value)
        return value

    @field_validator("parent_candidate_id")
    @classmethod
    def validate_parent_candidate_id(cls, value: str | None) -> str | None:
        if value is not None:
            split_candidate_id(value)
        return value

    @field_validator("source_run_id")
    @classmethod
    def validate_source_run_id(cls, value: str | None) -> str | None:
        if value is not None and not _RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("source_run_id must be a safe local run identifier")
        return value

    @field_validator("skill_sha256")
    @classmethod
    def validate_skill_sha256(cls, value: str) -> str:
        _validate_sha256(value, "skill_sha256")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        candidate_skill_id, _ = split_candidate_id(self.candidate_id)
        if candidate_skill_id != self.skill_id:
            raise ValueError("candidate_id skill segment must match skill_id")
        if self.parent_candidate_id is not None:
            parent_skill_id, _ = split_candidate_id(self.parent_candidate_id)
            if parent_skill_id != self.skill_id:
                raise ValueError("parent_candidate_id must have the same skill_id")
        if (
            self.external_source is not None
            and self.external_source.file_sha256 != self.skill_sha256
        ):
            raise ValueError("external_source.file_sha256 must match skill_sha256")
        if self.origin is CandidateOrigin.OPTIMIZER:
            if (
                self.optimizer_provenance is None
                or self.parent_candidate_id is None
                or self.source_run_id is None
                or self.external_source is not None
            ):
                raise ValueError("optimizer candidates require local optimizer provenance")
        elif self.optimizer_provenance is not None:
            raise ValueError("optimizer provenance requires optimizer origin")
        return self


class CatalogEntry(StrictCandidateModel):
    """Mutable catalog index entry; candidate files themselves remain immutable."""

    candidate_id: str
    skill_id: str
    parent_candidate_id: str | None = None
    created_at: datetime
    source_run_id: str | None = None
    skill_sha256: str
    status: CandidateStatus

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        split_candidate_id(value)
        return value

    @field_validator("skill_id")
    @classmethod
    def validate_skill_id(cls, value: str) -> str:
        validate_skill_id(value)
        return value

    @field_validator("parent_candidate_id")
    @classmethod
    def validate_parent_candidate_id(cls, value: str | None) -> str | None:
        if value is not None:
            split_candidate_id(value)
        return value

    @field_validator("source_run_id")
    @classmethod
    def validate_source_run_id(cls, value: str | None) -> str | None:
        if value is not None and not _RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("source_run_id must be a safe local run identifier")
        return value

    @field_validator("skill_sha256")
    @classmethod
    def validate_skill_sha256(cls, value: str) -> str:
        _validate_sha256(value, "skill_sha256")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        candidate_skill_id, _ = split_candidate_id(self.candidate_id)
        if candidate_skill_id != self.skill_id:
            raise ValueError("candidate_id skill segment must match skill_id")
        if self.parent_candidate_id is not None:
            parent_skill_id, _ = split_candidate_id(self.parent_candidate_id)
            if parent_skill_id != self.skill_id:
                raise ValueError("parent_candidate_id must have the same skill_id")
        return self


class CandidateCatalog(StrictCandidateModel):
    """Validated candidate index stored as `.local/skill-evolve/catalog.json`."""

    schema_version: str = "v1"
    entries: tuple[CatalogEntry, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "v1":
            raise ValueError("unsupported candidate catalog schema_version")
        return value

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        candidate_ids = [entry.candidate_id for entry in self.entries]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate catalog contains duplicate candidate_id values")
        selected_skill_ids = [
            entry.skill_id
            for entry in self.entries
            if entry.status is CandidateStatus.SELECTED
        ]
        if len(selected_skill_ids) != len(set(selected_skill_ids)):
            raise ValueError("candidate catalog has more than one selected candidate per skill")
        return self


@dataclass(frozen=True)
class ResolvedCandidate:
    """Verified candidate bytes that are safe to copy, but never execute."""

    metadata: CandidateMetadata
    catalog_entry: CatalogEntry
    skill_bytes: bytes

    def __post_init__(self) -> None:
        if hashlib.sha256(self.skill_bytes).hexdigest() != self.metadata.skill_sha256:
            raise CandidateContractError("resolved candidate bytes do not match metadata hash")
        if self.catalog_entry.skill_sha256 != self.metadata.skill_sha256:
            raise CandidateContractError("resolved candidate catalog hash does not match metadata")

    @property
    def candidate_id(self) -> str:
        return self.metadata.candidate_id

    @property
    def skill_sha256(self) -> str:
        return self.metadata.skill_sha256

    def public_binding(self) -> dict[str, str]:
        return {"candidate_id": self.candidate_id, "skill_sha256": self.skill_sha256}


def validate_skill_id(value: str) -> None:
    if not _SKILL_ID_PATTERN.fullmatch(value):
        raise CandidateContractError("skill_id must be a safe lowercase identifier")


def split_candidate_id(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2:
        raise CandidateContractError("candidate_id must be exactly skill-id/candidate-name")
    skill_id, candidate_name = parts
    validate_skill_id(skill_id)
    if not _CANDIDATE_NAME_PATTERN.fullmatch(candidate_name):
        raise CandidateContractError("candidate name must be a safe lowercase identifier")
    return skill_id, candidate_name


def _validate_sha256(value: str, label: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
