"""Strict local contracts for deterministic EditPlan materialization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
MaterializationStatus = Literal[
    "created",
    "skipped_no_actionable_plan",
    "invalid_binding",
    "render_failed",
    "candidate_failed",
]


class StrictMaterializationModel(BaseModel):
    """Persist only fully typed, non-secret materialization evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MaterializationSource(StrictMaterializationModel):
    run_id: str
    parent_candidate_id: str
    parent_skill_sha256: str
    edit_plan_sha256: str
    target_contract_sha256: str

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if _RUN_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("run_id must be a safe local run identifier")
        return value

    @field_validator("parent_candidate_id")
    @classmethod
    def validate_parent_candidate_id(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("parent_candidate_id is invalid")
        return value

    @field_validator("parent_skill_sha256", "edit_plan_sha256", "target_contract_sha256")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("materialization hash must be a lowercase SHA-256 digest")
        return value


class MaterializationReceipt(StrictMaterializationModel):
    schema_version: Literal["skill-materialization-receipt-v1"] = "skill-materialization-receipt-v1"
    status: MaterializationStatus
    source: MaterializationSource | None = None
    child_candidate_id: str | None = None
    child_skill_sha256: str | None = None
    patch_sha256: str | None = None
    edit_count: int = Field(default=0, ge=0, le=3)
    error_code: str | None = None

    @field_validator("child_skill_sha256", "patch_sha256")
    @classmethod
    def validate_optional_hashes(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("materialization hash must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_terminal_state(self) -> MaterializationReceipt:
        if self.status == "created":
            if (
                self.source is None
                or self.child_candidate_id is None
                or self.child_skill_sha256 is None
                or self.patch_sha256 is None
                or self.edit_count < 1
                or self.error_code is not None
            ):
                raise ValueError("created receipt requires complete child evidence")
        elif (
            self.child_candidate_id is not None
            or self.child_skill_sha256 is not None
            or self.patch_sha256 is not None
            or self.edit_count != 0
        ):
            raise ValueError("failed receipt must not claim a created child")
        return self


@dataclass(frozen=True)
class MaterializationResult:
    receipt: MaterializationReceipt
    patch_path: str | None
