"""Strict contracts for behavior-oriented Skill edit plans."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skill_evolve.optimizer.contracts import ProviderUsage

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BLOCK_ID_PATTERN = re.compile(r"^block-[0-9a-f]{16}$")
_LOCAL_REF_PATTERN = re.compile(r"^(?:case|evidence)-[0-9]{3}(?:-[0-9]{3})?-[0-9a-f]{12}$")
_LOCAL_ID_PATTERN = re.compile(r"^(?:cluster|edit)-[0-9]{3}$")
_CONTRACT_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")

SkillPermission = Literal["locked", "guarded", "evolvable", "experimental"]
SkillLayer = Literal["hard_contract", "core_strategy", "heuristics", "unknown"]
SkillBlockType = Literal[
    "frontmatter", "heading", "paragraph", "list", "fenced_code", "table"
]
FunctionalModule = Literal[
    "activation_and_scope",
    "task_and_context_parsing",
    "planning_and_order",
    "domain_reasoning",
    "tool_execution",
    "safety_constraints",
    "validation",
    "recovery",
    "final_submission",
]
DefectScope = Literal[
    "intrinsic_quality",
    "target_compatibility",
    "failure_causal_hypothesis",
    "candidate_generalization",
]
DefectType = Literal[
    "missing_guidance",
    "underspecified_guidance",
    "incorrect_guidance",
    "workflow_order_issue",
    "missing_recovery_strategy",
    "priority_conflict",
    "contradiction",
    "redundancy",
    "activation_issue",
]
SemanticOperation = Literal[
    "add_rule",
    "clarify_rule",
    "correct_rule",
    "set_precedence",
    "generalize_rule",
    "merge_rules",
    "remove_rule",
    "reorder_workflow",
]
ValidationMode = Literal["automatic", "review_only"]
EditPlanStatus = Literal[
    "proposed",
    "findings_only",
    "skipped_no_failed_trajectories",
    "skipped_invalid_evidence",
    "model_failed",
    "invalid_response",
    "unavailable",
]
EditPlanErrorCode = Literal["missing_api_key", "request_failed", "invalid_response"]
_LEGACY_GUARDED_APPROVAL_STATUS = "requires_guarded_approval"


class StrictEditPlanModel(BaseModel):
    """Reject unknown fields in every edit-plan artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_sha256(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _validate_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text must not contain NUL")
    if "*** Begin Patch" in value or "diff --git " in value:
        raise ValueError("semantic edit text must not contain a mechanical patch")
    if value.lstrip().lower().startswith(("# skill", "---\nname:")):
        raise ValueError("semantic edit text must not contain a full Skill document")
    return value


def normalise_historical_edit_plan_status(value: object) -> Literal["proposed", "findings_only"]:
    """Read the one retired v0.11 status without allowing new code to write it."""

    if value in {"proposed", _LEGACY_GUARDED_APPROVAL_STATUS}:
        return "proposed"
    if value == "findings_only":
        return "findings_only"
    raise ValueError("edit plan status is not materializable")


class SkillBlock(StrictEditPlanModel):
    block_id: str
    heading_path: tuple[str, ...] = Field(max_length=16)
    block_type: SkillBlockType
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_sha256: str
    layer: SkillLayer
    permission: SkillPermission

    @field_validator("block_id")
    @classmethod
    def validate_block_id(cls, value: str) -> str:
        if _BLOCK_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("block_id has an invalid format")
        return value

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def validate_line_range(self) -> SkillBlock:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class SkillMap(StrictEditPlanModel):
    schema_version: Literal["skill-map-v1"] = "skill-map-v1"
    parser_policy_version: Literal["skill-map-policy-v1"] = "skill-map-policy-v1"
    skill_sha256: str
    line_count: int = Field(ge=0)
    blocks: tuple[SkillBlock, ...] = Field(min_length=1, max_length=4096)

    @field_validator("skill_sha256")
    @classmethod
    def validate_skill_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @model_validator(mode="after")
    def validate_blocks(self) -> SkillMap:
        ids = [block.block_id for block in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("Skill map block IDs must be unique")
        previous_end = 0
        for block in self.blocks:
            if block.start_line <= previous_end:
                raise ValueError("Skill map blocks must be ordered and non-overlapping")
            if block.end_line > self.line_count:
                raise ValueError("Skill map block exceeds line_count")
            previous_end = block.end_line
        return self


class SkillTargetContract(StrictEditPlanModel):
    schema_version: Literal["skill-target-contract-v1"] = "skill-target-contract-v1"
    adapter_id: str = Field(min_length=1, max_length=128)
    available_capabilities: tuple[str, ...] = Field(min_length=1, max_length=16)
    sql_mutability: Literal["read_only"]
    max_statements_per_call: int = Field(ge=1, le=16)
    task_context_sources: tuple[Literal["schema", "knowledge", "column_meaning"], ...]
    final_submission_contract: Literal["one_final_sql_statement"]

    @field_validator("available_capabilities")
    @classmethod
    def validate_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("available_capabilities must be unique")
        return values

    @field_validator("task_context_sources")
    @classmethod
    def validate_context_sources(
        cls,
        values: tuple[Literal["schema", "knowledge", "column_meaning"], ...],
    ) -> tuple[Literal["schema", "knowledge", "column_meaning"], ...]:
        if len(values) != len(set(values)):
            raise ValueError("task_context_sources must be unique")
        return values


class ModelDefectCluster(StrictEditPlanModel):
    primary_module: FunctionalModule
    secondary_modules: tuple[FunctionalModule, ...] = Field(default=(), max_length=2)
    defect_type: DefectType
    scope: DefectScope
    statement: str = Field(min_length=1, max_length=2048)
    case_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=128)
    block_ids: tuple[str, ...] = Field(default=(), max_length=32)
    contract_fields: tuple[str, ...] = Field(default=(), max_length=16)
    missing_elements: tuple[str, ...] = Field(default=(), max_length=8)
    workflow_actions: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        return _validate_text(value)

    @field_validator("case_refs")
    @classmethod
    def validate_case_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("case_refs must be unique")
        if any(
            _LOCAL_REF_PATTERN.fullmatch(value) is None or not value.startswith("case-")
            for value in values
        ):
            raise ValueError("case_ref has an invalid format")
        return values

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("evidence_refs must be unique")
        if any(
            _LOCAL_REF_PATTERN.fullmatch(value) is None or not value.startswith("evidence-")
            for value in values
        ):
            raise ValueError("evidence_ref has an invalid format")
        return values

    @field_validator("block_ids")
    @classmethod
    def validate_block_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("block_ids must be unique")
        if any(_BLOCK_ID_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("block_id has an invalid format")
        return values

    @field_validator("contract_fields")
    @classmethod
    def validate_contract_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("contract_fields must be unique")
        if any(_CONTRACT_FIELD_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("contract field has an invalid format")
        return values

    @field_validator("missing_elements", "workflow_actions")
    @classmethod
    def validate_short_texts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("behavior elements must be unique")
        return tuple(_validate_text(value) for value in values)

    @model_validator(mode="after")
    def validate_type_specific_evidence(self) -> ModelDefectCluster:
        if self.primary_module in self.secondary_modules:
            raise ValueError("primary_module must not be repeated as a secondary module")
        if len(self.secondary_modules) != len(set(self.secondary_modules)):
            raise ValueError("secondary_modules must be unique")
        if self.defect_type == "incorrect_guidance" and (
            not self.block_ids or not self.contract_fields
        ):
            raise ValueError("incorrect_guidance requires a Skill block and contract field")
        if self.defect_type in {"contradiction", "redundancy", "priority_conflict"} and len(
            self.block_ids
        ) < 2:
            raise ValueError(f"{self.defect_type} requires at least two Skill blocks")
        if self.defect_type == "underspecified_guidance" and (
            not self.block_ids or not self.missing_elements
        ):
            raise ValueError("underspecified_guidance requires a block and missing elements")
        if self.defect_type == "workflow_order_issue" and len(self.workflow_actions) < 2:
            raise ValueError("workflow_order_issue requires at least two workflow actions")
        if self.defect_type == "missing_recovery_strategy" and not self.block_ids:
            raise ValueError("missing_recovery_strategy requires a related Skill block")
        if self.scope == "target_compatibility" and not self.contract_fields:
            raise ValueError("target_compatibility requires a contract field")
        return self


class WorkflowRule(StrictEditPlanModel):
    rule_kind: Literal["workflow"]
    trigger: str = Field(min_length=1, max_length=1024)
    action: str = Field(min_length=1, max_length=2048)
    validation: str = Field(min_length=1, max_length=1024)
    exit_or_next: str = Field(min_length=1, max_length=1024)

    @field_validator("trigger", "action", "validation", "exit_or_next")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)


class RecoveryRule(StrictEditPlanModel):
    rule_kind: Literal["recovery"]
    failure_signal: str = Field(min_length=1, max_length=1024)
    diagnosis: str = Field(min_length=1, max_length=1024)
    recovery_action: str = Field(min_length=1, max_length=2048)
    retry_or_alternative: str = Field(min_length=1, max_length=1024)
    validation: str = Field(min_length=1, max_length=1024)
    stop_condition: str = Field(min_length=1, max_length=1024)

    @field_validator(
        "failure_signal",
        "diagnosis",
        "recovery_action",
        "retry_or_alternative",
        "validation",
        "stop_condition",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)


class ConstraintRule(StrictEditPlanModel):
    rule_kind: Literal["constraint"]
    scope: str = Field(min_length=1, max_length=1024)
    requirement_or_prohibition: str = Field(min_length=1, max_length=2048)
    precedence: str = Field(min_length=1, max_length=1024)
    violation_handling: str = Field(min_length=1, max_length=1024)

    @field_validator("scope", "requirement_or_prohibition", "precedence", "violation_handling")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)


class HeuristicRule(StrictEditPlanModel):
    rule_kind: Literal["heuristic"]
    applicability: str = Field(min_length=1, max_length=1024)
    recommendation: str = Field(min_length=1, max_length=2048)
    boundary: str = Field(min_length=1, max_length=1024)

    @field_validator("applicability", "recommendation", "boundary")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)


RuleDefinition = Annotated[
    WorkflowRule | RecoveryRule | ConstraintRule | HeuristicRule,
    Field(discriminator="rule_kind"),
]


class BehaviorAssertion(StrictEditPlanModel):
    trigger_event: str = Field(min_length=1, max_length=1024)
    observed_before: str = Field(min_length=1, max_length=2048)
    expected_action: str = Field(min_length=1, max_length=2048)
    required_before_submission: str = Field(min_length=1, max_length=1024)
    prohibited_early_behavior: str = Field(min_length=1, max_length=1024)
    success_or_stop_condition: str = Field(min_length=1, max_length=1024)
    validation_mode: ValidationMode

    @field_validator(
        "trigger_event",
        "observed_before",
        "expected_action",
        "required_before_submission",
        "prohibited_early_behavior",
        "success_or_stop_condition",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)


class ModelSemanticEdit(StrictEditPlanModel):
    cluster_indices: tuple[int, ...] = Field(min_length=1, max_length=8)
    operation: SemanticOperation
    primary_module: FunctionalModule
    target_block_ids: tuple[str, ...] = Field(default=(), max_length=16)
    placement_after_block_id: str | None = None
    intent: str = Field(min_length=1, max_length=2048)
    rule: RuleDefinition
    behavior_assertion: BehaviorAssertion
    generalization_scope: str = Field(min_length=1, max_length=1024)
    regression_risk: str = Field(min_length=1, max_length=1024)
    falsifiable_validation: str = Field(min_length=1, max_length=2048)

    @field_validator("cluster_indices")
    @classmethod
    def validate_cluster_indices(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < 1 for value in values) or len(values) != len(set(values)):
            raise ValueError("cluster_indices must contain unique positive values")
        return values

    @field_validator("target_block_ids")
    @classmethod
    def validate_target_blocks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("target_block_ids must be unique")
        if any(_BLOCK_ID_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("target_block_id has an invalid format")
        return values

    @field_validator("placement_after_block_id")
    @classmethod
    def validate_placement_block(cls, value: str | None) -> str | None:
        if value is not None and _BLOCK_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("placement_after_block_id has an invalid format")
        return value

    @field_validator("intent", "generalization_scope", "regression_risk", "falsifiable_validation")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)

    @model_validator(mode="after")
    def validate_target(self) -> ModelSemanticEdit:
        if not self.target_block_ids and self.placement_after_block_id is None:
            raise ValueError("an edit requires a target block or placement anchor")
        if (
            self.placement_after_block_id is not None
            and self.placement_after_block_id in self.target_block_ids
        ):
            raise ValueError("placement anchor must not duplicate a target block")
        return self


class ModelEditPlan(StrictEditPlanModel):
    schema_version: Literal["skill-edit-plan-model-v1"] = "skill-edit-plan-model-v1"
    summary: str = Field(min_length=1, max_length=2048)
    clusters: tuple[ModelDefectCluster, ...] = Field(min_length=1, max_length=8)
    edits: tuple[ModelSemanticEdit, ...] = Field(default=(), max_length=3)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_text(value)

    @model_validator(mode="after")
    def validate_cluster_indices(self) -> ModelEditPlan:
        for edit in self.edits:
            if any(index > len(self.clusters) for index in edit.cluster_indices):
                raise ValueError("edit refers to an unknown cluster index")
        return self


class EditPlanSource(StrictEditPlanModel):
    run_id: str = Field(min_length=1, max_length=128)
    parent_candidate_id: str = Field(min_length=1, max_length=256)
    parent_skill_sha256: str
    agent_input_sha256: str
    target_contract_sha256: str
    skill_map_sha256: str

    @field_validator(
        "parent_skill_sha256",
        "agent_input_sha256",
        "target_contract_sha256",
        "skill_map_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("run_id", "parent_candidate_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("source identifiers must not contain NUL")
        return value


class DefectCluster(ModelDefectCluster):
    cluster_id: str

    @field_validator("cluster_id")
    @classmethod
    def validate_cluster_id(cls, value: str) -> str:
        if _LOCAL_ID_PATTERN.fullmatch(value) is None or not value.startswith("cluster-"):
            raise ValueError("cluster_id has an invalid format")
        return value


class SemanticEdit(StrictEditPlanModel):
    edit_id: str
    cluster_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    operation: SemanticOperation
    primary_module: FunctionalModule
    target_block_ids: tuple[str, ...] = Field(default=(), max_length=16)
    placement_after_block_id: str | None = None
    target_layers: tuple[SkillLayer, ...] = Field(min_length=1, max_length=4)
    target_permissions: tuple[SkillPermission, ...] = Field(min_length=1, max_length=4)
    intent: str = Field(min_length=1, max_length=2048)
    rule: RuleDefinition
    behavior_assertion: BehaviorAssertion
    generalization_scope: str = Field(min_length=1, max_length=1024)
    regression_risk: str = Field(min_length=1, max_length=1024)
    falsifiable_validation: str = Field(min_length=1, max_length=2048)

    @field_validator("edit_id")
    @classmethod
    def validate_edit_id(cls, value: str) -> str:
        if _LOCAL_ID_PATTERN.fullmatch(value) is None or not value.startswith("edit-"):
            raise ValueError("edit_id has an invalid format")
        return value

    @field_validator("cluster_ids")
    @classmethod
    def validate_cluster_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("cluster_ids must be unique")
        if any(
            _LOCAL_ID_PATTERN.fullmatch(value) is None or not value.startswith("cluster-")
            for value in values
        ):
            raise ValueError("cluster_id has an invalid format")
        return values

    @field_validator("target_block_ids")
    @classmethod
    def validate_target_blocks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("target_block_ids must be unique")
        if any(_BLOCK_ID_PATTERN.fullmatch(value) is None for value in values):
            raise ValueError("target_block_id has an invalid format")
        return values

    @field_validator("placement_after_block_id")
    @classmethod
    def validate_placement_block(cls, value: str | None) -> str | None:
        if value is not None and _BLOCK_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("placement_after_block_id has an invalid format")
        return value

    @field_validator("target_layers", "target_permissions")
    @classmethod
    def validate_unique_targets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("target classifications must be unique")
        return values

    @field_validator("intent", "generalization_scope", "regression_risk", "falsifiable_validation")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)

    @model_validator(mode="after")
    def validate_target(self) -> SemanticEdit:
        if not self.target_block_ids and self.placement_after_block_id is None:
            raise ValueError("an edit requires a target block or placement anchor")
        if (
            self.placement_after_block_id is not None
            and self.placement_after_block_id in self.target_block_ids
        ):
            raise ValueError("placement anchor must not duplicate a target block")
        return self


class SkillEditPlan(StrictEditPlanModel):
    schema_version: Literal["skill-edit-plan-v1"] = "skill-edit-plan-v1"
    status: Literal["proposed", "findings_only"]
    source: EditPlanSource
    summary: str = Field(min_length=1, max_length=2048)
    clusters: tuple[DefectCluster, ...] = Field(min_length=1, max_length=8)
    edits: tuple[SemanticEdit, ...] = Field(default=(), max_length=3)
    model_output_untrusted: Literal[True] = True

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_text(value)

    @model_validator(mode="after")
    def validate_local_bindings(self) -> SkillEditPlan:
        cluster_ids = {cluster.cluster_id for cluster in self.clusters}
        if len(cluster_ids) != len(self.clusters):
            raise ValueError("cluster IDs must be unique")
        edit_ids = {edit.edit_id for edit in self.edits}
        if len(edit_ids) != len(self.edits):
            raise ValueError("edit IDs must be unique")
        for edit in self.edits:
            if not set(edit.cluster_ids).issubset(cluster_ids):
                raise ValueError("edit refers to an unknown cluster ID")
        return self


class EditPlanReceipt(StrictEditPlanModel):
    schema_version: Literal["skill-edit-plan-receipt-v1"] = "skill-edit-plan-receipt-v1"
    status: EditPlanStatus
    provider: Literal["deepseek"]
    model: Literal["deepseek-v4-flash"]
    prompt_version: Literal["skill-edit-plan-prompt-v1"] = "skill-edit-plan-prompt-v1"
    source: EditPlanSource | None = None
    plan_sha256: str | None = None
    skill_map_sha256: str | None = None
    cluster_count: int = Field(default=0, ge=0, le=8)
    edit_count: int = Field(default=0, ge=0, le=3)
    usage: ProviderUsage | None = None
    error_code: EditPlanErrorCode | None = None

    @field_validator("plan_sha256", "skill_map_sha256")
    @classmethod
    def validate_optional_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_sha256(value)
        return value

    @model_validator(mode="after")
    def validate_terminal_state(self) -> EditPlanReceipt:
        successful = self.status in {
            "proposed",
            "findings_only",
        }
        if successful and (
            self.source is None
            or self.plan_sha256 is None
            or self.skill_map_sha256 is None
            or self.cluster_count < 1
            or self.error_code is not None
        ):
            raise ValueError("successful receipt requires complete source and artifact bindings")
        if self.status == "proposed" and self.edit_count < 1:
            raise ValueError("an actionable receipt requires at least one edit")
        if self.status == "findings_only" and self.edit_count != 0:
            raise ValueError("findings_only receipt must not claim an edit")
        if not successful and (
            self.plan_sha256 is not None or self.cluster_count != 0 or self.edit_count != 0
        ):
            raise ValueError("non-success receipt must not claim a completed plan")
        if (
            self.source is not None
            and self.skill_map_sha256 is not None
            and self.source.skill_map_sha256 != self.skill_map_sha256
        ):
            raise ValueError("receipt Skill map hash does not match its source")
        return self


@dataclass(frozen=True)
class EditPlanResult:
    """In-memory result returned to the explicit CLI."""

    receipt: EditPlanReceipt
    plan_path: str | None
    skill_map_path: str | None
