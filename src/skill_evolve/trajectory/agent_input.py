"""Build bounded Evolution-only inputs for a future Coding Agent invocation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from skill_evolve.runner.dsh import task_contract
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import (
    TrajectoryCompressionError,
    _bounded_evidence,
    _safe_text,
)
from skill_evolve.trajectory.contracts import (
    CompressedTrajectory,
    EvolutionAgentInput,
    EvolutionCaseInput,
    TrustedContext,
)
from skill_evolve.trajectory.policy import DEFAULT_COMPRESSOR_POLICY, CompressorPolicy

if TYPE_CHECKING:
    from skill_evolve.benchmark.livesqlbench import LiveSQLBenchCase

_CONTEXT_ROLES = ("schema", "knowledge", "column_meaning")


def build_evolution_agent_input(
    manifest_sha256: str,
    environment_fingerprint: str,
    subject: RunSubject,
    cases: tuple[tuple[LiveSQLBenchCase, CompressedTrajectory], ...],
    omitted_cases: tuple[dict[str, str], ...] = (),
    *,
    policy: CompressorPolicy = DEFAULT_COMPRESSOR_POLICY,
) -> EvolutionAgentInput:
    """Assemble already-compressed Evolution evidence without model invocation.

    ``LiveSQLBenchCase.public_context_files`` is the sole source for task
    context. Neither evaluator fields nor runner workspaces are read here.
    """

    if not manifest_sha256 or not environment_fingerprint:
        raise TrajectoryCompressionError(
            "Evolution manifest and environment fingerprints are required"
        )
    try:
        skill_text = subject.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TrajectoryCompressionError("evaluated subject is not valid UTF-8") from error
    truncated_fields: list[str] = []
    skill = _bounded_evidence(skill_text, policy.max_skill_bytes, "skill", truncated_fields)
    if skill.truncated:
        raise TrajectoryCompressionError("evaluated Skill exceeds agent-input size limit")
    ordered_cases = sorted(cases, key=_case_sort_key)
    agent_cases = tuple(_case_input(case, trajectory, policy) for case, trajectory in ordered_cases)
    return EvolutionAgentInput(
        manifest_sha256=manifest_sha256,
        environment_fingerprint=environment_fingerprint,
        subject=subject.public_binding(),
        skill=skill,
        trajectories=agent_cases,
        omitted_cases=tuple(sorted(omitted_cases, key=lambda item: item["case_id"])),
        policy_sha256=policy.sha256,
    )


def _case_sort_key(item: tuple[LiveSQLBenchCase, CompressedTrajectory]) -> str:
    return item[0].instance_id


def _case_input(
    case: LiveSQLBenchCase, trajectory: CompressedTrajectory, policy: CompressorPolicy
) -> EvolutionCaseInput:
    if case.instance_id != trajectory.evaluation.case_id:
        raise TrajectoryCompressionError("case and trajectory identifiers do not match")
    context = tuple(
        _trusted_context(role, path, policy)
        for role, path in zip(_CONTEXT_ROLES, case.public_context_files, strict=True)
    )
    return EvolutionCaseInput(
        case_id=case.instance_id,
        prompt=_safe_text(case.prompt),
        task_contract=task_contract(True, subject_path="SKILL.md"),
        difficulty_tier=case.difficulty_tier,
        context=context,
        trajectory=trajectory,
    )


def _trusted_context(role: str, path: Path, policy: CompressorPolicy) -> TrustedContext:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as error:
        raise TrajectoryCompressionError(f"could not read public {role} context") from error
    safe = _safe_text(text)
    encoded = safe.encode("utf-8")
    visible = _truncate_utf8(safe, policy.max_context_bytes)
    return TrustedContext(
        role=cast(Literal["schema", "knowledge", "column_meaning"], role),
        sha256=hashlib.sha256(encoded).hexdigest(),
        text=visible,
        original_bytes=len(encoded),
        truncated=len(visible.encode("utf-8")) < len(encoded),
    )


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "\n…<truncated>"
    prefix = encoded[: max(0, limit - len(marker.encode("utf-8")))].decode(
        "utf-8", errors="ignore"
    )
    return prefix + marker
