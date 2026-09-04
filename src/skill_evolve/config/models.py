"""Typed, non-secret experiment manifest models."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
OPTIMIZER_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class StrictModel(BaseModel):
    """Reject unexpected configuration rather than silently ignoring it."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class CandidateConfig(StrictModel):
    ref: str | None = None
    digest_file: str | None = None


class ModelConfig(StrictModel):
    provider: str
    name: str
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)


class DshConfig(StrictModel):
    version: str
    profile: str
    profile_patch: str | None = None
    runner: Literal["fake", "dsh"] = "fake"
    fake_output: str | None = None
    binary: str = "dsh"
    session_log_root: str | None = None
    runner_args: tuple[str, ...] = ()
    timeout_ms: int = Field(default=300_000, ge=1_000, le=3_600_000)

    @field_validator("binary")
    @classmethod
    def validate_binary(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("dsh.binary must be a non-empty executable path or command name")
        return value

    @field_validator("runner_args")
    @classmethod
    def validate_runner_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        forbidden_fragments = ("api_key", "password", "secret", "token", "authorization")
        for argument in value:
            normalized = argument.lower().replace("-", "_")
            if not argument or "\x00" in argument:
                raise ValueError("dsh.runner_args must contain non-empty strings without NUL bytes")
            if any(fragment in normalized for fragment in forbidden_fragments):
                raise ValueError("dsh.runner_args must not contain credential options or values")
        return value

    @model_validator(mode="after")
    def validate_runner_config(self) -> DshConfig:
        if self.runner == "fake" and self.fake_output is None:
            raise ValueError("dsh.fake_output is required when dsh.runner is fake")
        if self.runner == "dsh" and self.fake_output is not None:
            raise ValueError("dsh.fake_output is only valid when dsh.runner is fake")
        return self


class ToolConfig(StrictModel):
    id: str
    version: str
    manifest: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("tool id must be a lowercase identifier")
        return value


class DatabaseConfig(StrictModel):
    kind: Literal["postgresql"]
    image: str
    image_digest: str
    schema_file: str = Field(alias="schema")
    seed: str

    @field_validator("image_digest")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        if not IMAGE_DIGEST_PATTERN.fullmatch(value):
            raise ValueError(
                "image_digest must be a sha256 digest with 64 lowercase hex characters"
            )
        return value


class EvaluatorConfig(StrictModel):
    version: str
    result_order: Literal["ignore", "strict"] = "ignore"
    numeric_tolerance: float = Field(default=0, ge=0)
    max_rows: int = Field(default=1000, ge=1, le=100_000)
    timeout_ms: int = Field(default=5000, ge=1, le=300_000)


class OptimizerConfig(StrictModel):
    """Non-secret settings for the optional Evolution-only advice call."""

    enabled: bool = False
    provider: Literal["deepseek"] = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    api_key_env: Literal["DEEPSEEK_API_KEY"] = "DEEPSEEK_API_KEY"
    timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    max_output_tokens: int | None = Field(default=None, ge=64)
    max_input_bytes: int = Field(default=524_288, ge=1_024, le=2_097_152)
    prompt_version: Literal["skill-optimization-advice-prompt-v1"] = (
        "skill-optimization-advice-prompt-v1"
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("optimizer.base_url must be an HTTPS origin or base path")
        path_parts = tuple(part for part in parsed.path.split("/") if part)
        if any(part in {".", ".."} for part in path_parts):
            raise ValueError("optimizer.base_url must not contain relative path segments")
        return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if OPTIMIZER_MODEL_PATTERN.fullmatch(value) is None:
            raise ValueError("optimizer.model must be a safe provider model identifier")
        return value


class EnvironmentConfig(StrictModel):
    model: ModelConfig
    dsh: DshConfig
    tools: tuple[ToolConfig, ...]
    database: DatabaseConfig
    evaluator: EvaluatorConfig
    optimizer: OptimizerConfig = Field(default_factory=OptimizerConfig)

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, value: tuple[ToolConfig, ...]) -> tuple[ToolConfig, ...]:
        if not value:
            raise ValueError("at least one tool is required")
        ids = [tool.id for tool in value]
        if len(ids) != len(set(ids)):
            raise ValueError("tool ids must be unique")
        return value


class CaseConfig(StrictModel):
    id: str
    slice: Literal["fast", "dev", "held_out"]
    prompt: str = Field(min_length=1)
    expected_query: str = Field(min_length=1)
    context_files: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("case id must be a lowercase identifier")
        return value

    @field_validator("context_files")
    @classmethod
    def validate_context_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            pure_path = PurePosixPath(path)
            if not path or "\\" in path or pure_path.is_absolute() or ".." in pure_path.parts:
                raise ValueError("case context_files must be relative POSIX paths")
        if len(value) != len(set(value)):
            raise ValueError("case context_files must not contain duplicates")
        return value


class ExperimentManifest(StrictModel):
    schema_version: Literal["v1"]
    experiment: str
    candidate: CandidateConfig = Field(default_factory=CandidateConfig)
    environment: EnvironmentConfig
    cases: tuple[CaseConfig, ...]

    @field_validator("experiment")
    @classmethod
    def validate_experiment(cls, value: str) -> str:
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("experiment must be a lowercase identifier")
        return value

    @model_validator(mode="after")
    def validate_cases(self) -> ExperimentManifest:
        if not self.cases:
            raise ValueError("at least one case is required")
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case ids must be unique")
        return self
