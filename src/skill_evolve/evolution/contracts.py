"""Public-safe contracts for one bounded, local Evolution cycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from skill_evolve.candidate.contracts import CandidateContractError, split_candidate_id

_CYCLE_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CHILD_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,58}$")
MAX_CYCLE_GENERATIONS = 100

CycleStopReason = Literal[
    "completed",
    "smoke_gate_not_met",
    "evolution_invalid",
    "no_optimizable_failure",
    "advice_not_proposed",
    "plan_not_proposed",
    "materialization_not_created",
]


class EvolutionCycleError(ValueError):
    """A requested local Evolution cycle is unsafe or cannot start."""


@dataclass(frozen=True)
class EvolutionCycleRequest:
    """Validated, bounded input for a user-authorised local cycle."""

    candidate_id: str
    max_generations: int
    child_name_prefix: str

    def __post_init__(self) -> None:
        try:
            split_candidate_id(self.candidate_id)
        except CandidateContractError as error:
            raise EvolutionCycleError(str(error)) from error
        if (
            not isinstance(self.max_generations, int)
            or isinstance(self.max_generations, bool)
            or self.max_generations < 1
            or self.max_generations > MAX_CYCLE_GENERATIONS
        ):
            raise EvolutionCycleError(
                f"max_generations must be between 1 and {MAX_CYCLE_GENERATIONS}"
            )
        if _CHILD_PREFIX_PATTERN.fullmatch(self.child_name_prefix) is None:
            raise EvolutionCycleError("child_name_prefix must be a safe candidate name prefix")

    def child_name_for_generation(self, generation: int) -> str:
        if generation < 1:
            raise EvolutionCycleError("generation must be positive")
        candidate_name = f"{self.child_name_prefix}-{generation:03d}"
        skill_id, _ = split_candidate_id(self.candidate_id)
        try:
            split_candidate_id(f"{skill_id}/{candidate_name}")
        except CandidateContractError as error:
            raise EvolutionCycleError("generated child candidate name is invalid") from error
        return candidate_name


@dataclass(frozen=True)
class EvolutionGenerationReceipt:
    """One public-safe generation summary without benchmark case material."""

    generation: int
    candidate_id: str
    smoke_manifest_sha256: str | None = None
    smoke_run_id: str | None = None
    smoke_status: str | None = None
    evolution_manifest_sha256: str | None = None
    evolution_run_id: str | None = None
    evolution_status: str | None = None
    evolution_evaluable_case_count: int | None = None
    evolution_discarded_evaluator_invalid_count: int | None = None
    advice_status: str | None = None
    plan_status: str | None = None
    materialization_status: str | None = None
    child_candidate_id: str | None = None

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise EvolutionCycleError("generation receipt must be positive")
        try:
            split_candidate_id(self.candidate_id)
            if self.child_candidate_id is not None:
                split_candidate_id(self.child_candidate_id)
        except CandidateContractError as error:
            raise EvolutionCycleError("generation receipt candidate ID is invalid") from error
        for manifest_hash in (self.smoke_manifest_sha256, self.evolution_manifest_sha256):
            if manifest_hash is not None and _SHA256_PATTERN.fullmatch(manifest_hash) is None:
                raise EvolutionCycleError("generation receipt manifest hash is invalid")
        for evaluator_count in (
            self.evolution_evaluable_case_count,
            self.evolution_discarded_evaluator_invalid_count,
        ):
            if evaluator_count is not None and (
                not isinstance(evaluator_count, int) or evaluator_count < 0
            ):
                raise EvolutionCycleError("generation receipt evaluator counts are invalid")

    def public_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "candidate_id": self.candidate_id,
            "smoke_manifest_sha256": self.smoke_manifest_sha256,
            "smoke_run_id": self.smoke_run_id,
            "smoke_status": self.smoke_status,
            "evolution_manifest_sha256": self.evolution_manifest_sha256,
            "evolution_run_id": self.evolution_run_id,
            "evolution_status": self.evolution_status,
            "evolution_evaluable_case_count": self.evolution_evaluable_case_count,
            "evolution_discarded_evaluator_invalid_count": (
                self.evolution_discarded_evaluator_invalid_count
            ),
            "advice_status": self.advice_status,
            "plan_status": self.plan_status,
            "materialization_status": self.materialization_status,
            "child_candidate_id": self.child_candidate_id,
        }


@dataclass(frozen=True)
class EvolutionCycleReceipt:
    """Terminal summary persisted locally and safe to print from the CLI."""

    cycle_id: str
    initial_candidate_id: str
    final_candidate_id: str
    stop_reason: CycleStopReason
    generations: tuple[EvolutionGenerationReceipt, ...]

    def __post_init__(self) -> None:
        if _CYCLE_ID_PATTERN.fullmatch(self.cycle_id) is None:
            raise EvolutionCycleError("cycle receipt ID is invalid")
        try:
            split_candidate_id(self.initial_candidate_id)
            split_candidate_id(self.final_candidate_id)
        except CandidateContractError as error:
            raise EvolutionCycleError("cycle receipt candidate ID is invalid") from error

    def public_dict(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "initial_candidate_id": self.initial_candidate_id,
            "final_candidate_id": self.final_candidate_id,
            "stop_reason": self.stop_reason,
            "generations": [generation.public_dict() for generation in self.generations],
        }
