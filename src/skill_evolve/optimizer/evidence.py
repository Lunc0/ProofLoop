"""Shared fail-closed loading for optimizer-safe Evolution evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from skill_evolve.config.models import OptimizerConfig
from skill_evolve.optimizer.contracts import AdviceSource, EvolutionAgentInputManifest
from skill_evolve.registry.store import RunDirectory
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import canonical_json_bytes
from skill_evolve.trajectory.contracts import EvolutionAgentInputV3


@dataclass(frozen=True)
class PreparedEvolutionInput:
    """One validated v3 payload bound to the exact materialized Skill subject."""

    source: AdviceSource
    payload: EvolutionAgentInputV3


def load_prepared_evolution_input(
    run: RunDirectory, subject: RunSubject, optimizer: OptimizerConfig
) -> PreparedEvolutionInput | None:
    """Load canonical v3 evidence without following any artifact-provided instruction."""

    manifest_path = optimizer_artifact_path(run, "agent-input/manifest.json")
    payload_path = optimizer_artifact_path(run, "agent-input/trajectory.json")
    if manifest_path is None or payload_path is None:
        return None
    try:
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > 64 * 1024:
            return None
        manifest = EvolutionAgentInputManifest.model_validate_json(manifest_bytes)
        payload_bytes = payload_path.read_bytes()
        if not payload_bytes or len(payload_bytes) > optimizer.max_input_bytes + 1:
            return None
        payload = EvolutionAgentInputV3.model_validate_json(payload_bytes)
        canonical_payload = canonical_json_bytes(payload.model_dump(mode="json"))
        subject_text = subject.content.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        return None
    if payload_bytes not in {canonical_payload, canonical_payload + b"\n"}:
        return None
    if hashlib.sha256(canonical_payload).hexdigest() != manifest.agent_input_sha256:
        return None
    if len(canonical_payload) > optimizer.max_input_bytes:
        return None
    if manifest.environment_fingerprint != payload.environment_fingerprint:
        return None
    if manifest.projection_rule_version != payload.projection_rule_version:
        return None
    if manifest.included_case_count != len(payload.trajectories):
        return None
    if payload.evaluated_skill.subject_id != subject.subject_id:
        return None
    if payload.evaluated_skill.content_sha256 != subject.content_sha256:
        return None
    if payload.skill != subject_text:
        return None
    return PreparedEvolutionInput(
        source=AdviceSource(
            run_id=run.run_id,
            agent_input_sha256=manifest.agent_input_sha256,
            evaluated_skill_sha256=subject.content_sha256,
        ),
        payload=payload,
    )


def optimizer_artifact_path(run: RunDirectory, relative_path: str) -> Path | None:
    """Resolve one non-symlink regular file inside a run directory."""

    candidate = run.path / relative_path
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(run.path.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved
