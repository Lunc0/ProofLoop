"""Create stable, inspectable environment fingerprints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from skill_evolve.candidate.contracts import ResolvedCandidate
from skill_evolve.config.loader import ResolvedManifest
from skill_evolve.runner.dsh import sql_harness_policy_identity


@dataclass(frozen=True)
class FileDigest:
    name: str
    path: str
    sha256: str


@dataclass(frozen=True)
class EnvironmentSnapshot:
    fingerprint: str
    manifest: dict[str, Any]
    artifacts: tuple[FileDigest, ...]
    candidate: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "environment_fingerprint": self.fingerprint,
            "manifest": self.manifest,
            "artifacts": [asdict(artifact) for artifact in self.artifacts],
        }
        if self.candidate is not None:
            result["candidate"] = self.candidate
        return result


def create_snapshot(
    resolved: ResolvedManifest, candidate: ResolvedCandidate | None = None
) -> EnvironmentSnapshot:
    """Hash all declared, non-secret experiment inputs."""

    artifacts = _collect_artifacts(resolved)
    canonical_manifest = _canonical_manifest(
        resolved.manifest.model_dump(mode="json", by_alias=True)
    )
    payload = {
        "manifest": canonical_manifest,
        "artifacts": [{"name": artifact.name, "sha256": artifact.sha256} for artifact in artifacts],
    }
    if candidate is not None:
        payload["candidate"] = candidate.public_binding()
    fingerprint = _sha256_text(_canonical_json(payload))
    return EnvironmentSnapshot(
        fingerprint=fingerprint,
        manifest=canonical_manifest,
        artifacts=artifacts,
        candidate=candidate.public_binding() if candidate is not None else None,
    )


def _collect_artifacts(resolved: ResolvedManifest) -> tuple[FileDigest, ...]:
    declared: list[tuple[str, Path]] = [
        ("database.schema", resolved.schema_file),
        ("database.seed", resolved.seed_file),
    ]
    if resolved.candidate_digest_file is not None:
        declared.append(("candidate.digest_file", resolved.candidate_digest_file))
    if resolved.profile_patch is not None:
        declared.append(("dsh.profile_patch", resolved.profile_patch))
    if resolved.fake_output is not None:
        declared.append(("dsh.fake_output", resolved.fake_output))
    declared.extend(
        (f"tools.{tool_id}.manifest", path) for tool_id, path in resolved.tool_manifests
    )
    declared.extend(
        (f"cases.{case_id}.context[{index}]", path)
        for case_id, paths in resolved.case_context_files
        for index, path in enumerate(paths)
    )
    artifacts = tuple(
        FileDigest(
            name=name,
            path=path.relative_to(resolved.workspace).as_posix(),
            sha256=_sha256_file(path),
        )
        for name, path in declared
    )
    if resolved.manifest.environment.dsh.runner != "dsh":
        return artifacts
    identity = sql_harness_policy_identity()
    return (
        *artifacts,
        FileDigest(
            name="dsh.sql_harness_policy",
            path="package://skill_evolve/runner/dsh_plugins/sql_harness_policy.mjs",
            sha256=identity["plugin_sha256"],
        ),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_manifest(value: Any) -> dict[str, Any]:
    """Remove manifest-local artifact path spellings from the fingerprint input.

    Declared input content is represented by FileDigest entries. Replacing the path
    spelling makes a persisted, copied resolved manifest fingerprint-identical to
    the original manifest when its artifact bytes are unchanged.
    """

    artifact_path_keys = {
        "digest_file",
        "profile_patch",
        "fake_output",
        "manifest",
        "schema",
        "seed",
        "context_files",
    }

    def normalise(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "<artifact>"
                if key in artifact_path_keys and nested is not None
                else normalise(nested)
                for key, nested in item.items()
            }
        if isinstance(item, list):
            return [normalise(nested) for nested in item]
        return item

    normalised = normalise(value)
    assert isinstance(normalised, dict)
    return normalised
