"""Build bounded v2 Evolution inputs for a future Coding Agent invocation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from skill_evolve.runner.dsh import task_contract
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import (
    TrajectoryCompressionError,
    _safe_text,
    canonical_json_bytes,
)
from skill_evolve.trajectory.contracts import (
    CompressedTrajectory,
    EvolutionAgentInputV2,
    EvolutionProjectionCase,
)
from skill_evolve.trajectory.policy import DEFAULT_COMPRESSOR_POLICY, CompressorPolicy
from skill_evolve.trajectory.projection import (
    ProjectionContextSource,
    project_candidate_output,
    project_context,
    project_evaluation,
    project_evidence,
    project_final_submission_attempt,
)

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
) -> EvolutionAgentInputV2:
    """Assemble only failed-case v2 evidence without model invocation.

    ``manifest_sha256`` is validated at the custody boundary but intentionally
    omitted from the optimizer-facing payload.  ``omitted_cases`` remains an
    input compatibility parameter for the rolling runner; it is not exposed.
    """

    del omitted_cases
    if not manifest_sha256 or not environment_fingerprint:
        raise TrajectoryCompressionError(
            "Evolution manifest and environment fingerprints are required"
        )
    try:
        skill_text = subject.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TrajectoryCompressionError("evaluated subject is not valid UTF-8") from error
    skill = _bounded_text(skill_text, policy.max_skill_bytes)
    if len(_safe_text(skill_text).encode("utf-8")) > policy.max_skill_bytes:
        raise TrajectoryCompressionError("evaluated Skill exceeds agent-input size limit")
    ordered_cases = sorted(cases, key=_case_sort_key)
    agent_cases = tuple(_case_input(case, trajectory, policy) for case, trajectory in ordered_cases)
    agent_input = EvolutionAgentInputV2(
        projection_rule_version=policy.projection_rule_version,
        environment_fingerprint=environment_fingerprint,
        subject_id=subject.subject_id,
        skill=skill,
        trajectories=agent_cases,
    )
    agent_input_bytes = canonical_json_bytes(agent_input.model_dump(mode="json"))
    if len(agent_input_bytes) > policy.max_agent_input_bytes:
        raise TrajectoryCompressionError("Evolution agent input exceeds size limit")
    return agent_input


def _case_sort_key(item: tuple[LiveSQLBenchCase, CompressedTrajectory]) -> str:
    return item[0].instance_id


def _case_input(
    case: LiveSQLBenchCase, trajectory: CompressedTrajectory, policy: CompressorPolicy
) -> EvolutionProjectionCase:
    if case.instance_id != trajectory.evaluation.case_id:
        raise TrajectoryCompressionError("case and trajectory identifiers do not match")
    if trajectory.evaluation.verdict == "pass":
        raise TrajectoryCompressionError("passed cases must not enter Evolution optimizer input")
    sources = tuple(
        _public_context_source(role, path, policy)
        for role, path in zip(_CONTEXT_ROLES, case.public_context_files, strict=True)
    )
    return EvolutionProjectionCase(
        case_id=case.instance_id,
        prompt=_bounded_text(case.prompt, policy.max_prompt_bytes),
        task_contract=task_contract(True, subject_path="SKILL.md"),
        difficulty_tier=case.difficulty_tier,
        context=project_context(sources, trajectory, policy),
        evidence=project_evidence(trajectory, policy),
        candidate_output=project_candidate_output(trajectory),
        final_submission_attempt=project_final_submission_attempt(trajectory, policy),
        evaluation=project_evaluation(trajectory, policy),
    )


def _public_context_source(
    role: str, path: Path, policy: CompressorPolicy
) -> ProjectionContextSource:
    try:
        with path.open("rb") as stream:
            raw = stream.read(policy.max_context_source_bytes + 1)
        if len(raw) > policy.max_context_source_bytes:
            raise TrajectoryCompressionError(f"public {role} context exceeds source size limit")
        text = raw.decode("utf-8", errors="strict")
    except TrajectoryCompressionError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise TrajectoryCompressionError(f"could not read public {role} context") from error
    return ProjectionContextSource(
        role=cast(Literal["schema", "knowledge", "column_meaning"], role),
        text=_safe_text(text),
    )


def _bounded_text(value: str, limit: int) -> str:
    safe = _safe_text(value)
    encoded = safe.encode("utf-8")
    if len(encoded) <= limit:
        return safe
    marker = "\n…<truncated>"
    prefix = encoded[: max(0, limit - len(marker.encode("utf-8")))].decode(
        "utf-8", errors="ignore"
    )
    return prefix + marker
