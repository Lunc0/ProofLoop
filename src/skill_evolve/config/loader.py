"""Load manifests and confine their file references to the workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from skill_evolve.config.models import CaseConfig, ExperimentManifest
from skill_evolve.config.runtime import resolve_dsh_config

SENSITIVE_KEY_FRAGMENTS = ("api_key", "password", "secret", "token", "authorization")
SAFE_SECRET_REFERENCE_PATHS = frozenset(
    {
        ("environment", "optimizer", "api_key_env"),
        ("environment", "optimizer", "max_output_tokens"),
    }
)


class ManifestError(ValueError):
    """A manifest cannot safely be used for an experiment."""


@dataclass(frozen=True)
class ResolvedManifest:
    """A parsed manifest plus validated, workspace-confined artifact paths."""

    path: Path
    workspace: Path
    manifest: ExperimentManifest
    candidate_digest_file: Path | None
    profile_patch: Path | None
    fake_output: Path | None
    schema_file: Path
    seed_file: Path
    tool_manifests: tuple[tuple[str, Path], ...]
    case_context_files: tuple[tuple[str, tuple[Path, ...]], ...]

    def select_cases(self, requested_slice: str | None) -> tuple[CaseConfig, ...]:
        if requested_slice is None:
            return self.manifest.cases
        selected = tuple(case for case in self.manifest.cases if case.slice == requested_slice)
        if not selected:
            raise ManifestError(f"suite {requested_slice!r} contains no cases")
        return selected

    def context_files_for(self, case_id: str) -> tuple[Path, ...]:
        for configured_case_id, paths in self.case_context_files:
            if configured_case_id == case_id:
                return paths
        raise ManifestError(f"case {case_id!r} has no resolved context files")


def load_manifest(path: Path) -> ResolvedManifest:
    """Parse and validate a manifest without executing any runner or SQL."""

    manifest_path = path.resolve(strict=True)
    if not manifest_path.is_file():
        raise ManifestError(f"manifest is not a file: {path}")
    workspace = manifest_path.parent
    raw = _read_yaml(manifest_path)
    _reject_secrets(raw)
    try:
        manifest = ExperimentManifest.model_validate(raw)
    except ValidationError as error:
        raise ManifestError(f"invalid manifest: {error}") from error
    try:
        manifest = manifest.model_copy(
            update={
                "environment": manifest.environment.model_copy(
                    update={"dsh": resolve_dsh_config(manifest.environment.dsh, workspace)}
                )
            }
        )
    except ValidationError as error:
        raise ManifestError(f"invalid DSH runtime override: {error}") from error

    candidate_file = _resolve_optional(
        workspace, manifest.candidate.digest_file, "candidate.digest_file"
    )
    profile_patch = _resolve_optional(
        workspace, manifest.environment.dsh.profile_patch, "dsh.profile_patch"
    )
    fake_output = _resolve_optional(
        workspace, manifest.environment.dsh.fake_output, "dsh.fake_output"
    )
    schema_file = _resolve_required(
        workspace, manifest.environment.database.schema_file, "database.schema"
    )
    seed_file = _resolve_required(workspace, manifest.environment.database.seed, "database.seed")
    tool_manifests = tuple(
        (tool.id, _resolve_required(workspace, tool.manifest, f"tools[{tool.id}].manifest"))
        for tool in manifest.environment.tools
    )
    case_context_files = tuple(
        (
            case.id,
            tuple(
                _resolve_required(workspace, path, f"cases[{case.id}].context_files")
                for path in case.context_files
            ),
        )
        for case in manifest.cases
    )
    return ResolvedManifest(
        path=manifest_path,
        workspace=workspace,
        manifest=manifest,
        candidate_digest_file=candidate_file,
        profile_patch=profile_patch,
        fake_output=fake_output,
        schema_file=schema_file,
        seed_file=seed_file,
        tool_manifests=tool_manifests,
        case_context_files=case_context_files,
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ManifestError(f"invalid YAML in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ManifestError("manifest root must be a mapping")
    return value


def _reject_secrets(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise ManifestError("manifest keys must be strings")
            key_path = (*path, key)
            key_normalized = key.lower()
            if (
                key_path not in SAFE_SECRET_REFERENCE_PATHS
                and any(fragment in key_normalized for fragment in SENSITIVE_KEY_FRAGMENTS)
            ):
                dotted_path = ".".join(key_path)
                raise ManifestError(f"manifest must not contain secrets: {dotted_path}")
            _reject_secrets(nested_value, key_path)
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            _reject_secrets(nested_value, (*path, str(index)))


def _resolve_optional(workspace: Path, declared_path: str | None, label: str) -> Path | None:
    if declared_path is None:
        return None
    return _resolve_required(workspace, declared_path, label)


def _resolve_required(workspace: Path, declared_path: str, label: str) -> Path:
    candidate = (workspace / declared_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as error:
        raise ManifestError(f"{label} must remain inside the manifest workspace") from error
    if not candidate.is_file():
        raise ManifestError(f"{label} must reference an existing file: {declared_path}")
    return candidate
