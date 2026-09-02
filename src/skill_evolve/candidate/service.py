"""Pure orchestration for local candidate lifecycle operations."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from skill_evolve.candidate.contracts import (
    CandidateMetadata,
    CandidateOrigin,
    CandidateStatus,
    ExternalSource,
    OptimizerProvenance,
    ResolvedCandidate,
    split_candidate_id,
    validate_skill_id,
)
from skill_evolve.candidate.store import (
    CandidateStoreError,
    LocalCandidateStore,
    skill_sha256_for_source,
)


class CandidateService:
    """Create immutable metadata before delegating persistence to a verified store."""

    def __init__(self, store: LocalCandidateStore) -> None:
        self._store = store

    def initialise(
        self,
        skill_id: str,
        skill_source: Path,
        *,
        candidate_name: str = "000-baseline",
        origin: CandidateOrigin = CandidateOrigin.MANUAL,
        external_source: ExternalSource | None = None,
        notice_source: Path | None = None,
    ) -> ResolvedCandidate:
        validate_skill_id(skill_id)
        candidate_id = f"{skill_id}/{candidate_name}"
        split_candidate_id(candidate_id)
        return self._store.initialise(
            self._metadata(
                candidate_id,
                skill_id,
                skill_source,
                parent_candidate_id=None,
                source_run_id=None,
                origin=origin,
                external_source=external_source,
            ),
            skill_source,
            notice_source,
        )

    def import_candidate(
        self,
        skill_id: str,
        candidate_name: str,
        skill_source: Path,
        *,
        parent_candidate_id: str,
        source_run_id: str,
        origin: CandidateOrigin = CandidateOrigin.EXTERNAL_AGENT,
        external_source: ExternalSource | None = None,
        notice_source: Path | None = None,
        optimizer_provenance: OptimizerProvenance | None = None,
    ) -> ResolvedCandidate:
        validate_skill_id(skill_id)
        candidate_id = f"{skill_id}/{candidate_name}"
        split_candidate_id(candidate_id)
        parent_skill_id, _ = split_candidate_id(parent_candidate_id)
        if parent_skill_id != skill_id:
            raise CandidateStoreError("candidate parent must belong to the requested skill_id")
        return self._store.import_candidate(
            self._metadata(
                candidate_id,
                skill_id,
                skill_source,
                parent_candidate_id=parent_candidate_id,
                source_run_id=source_run_id,
                origin=origin,
                external_source=external_source,
                optimizer_provenance=optimizer_provenance,
            ),
            skill_source,
            notice_source,
        )

    def list_candidates(self, skill_id: str) -> tuple[ResolvedCandidate, ...]:
        return self._store.list_candidates(skill_id)

    def show(self, candidate_id: str) -> ResolvedCandidate:
        return self._store.resolve_and_verify(candidate_id)

    def verify(self, candidate_id: str) -> ResolvedCandidate:
        return self._store.verify(candidate_id)

    def select(self, candidate_id: str) -> ResolvedCandidate:
        return self._store.select(candidate_id)

    def _metadata(
        self,
        candidate_id: str,
        skill_id: str,
        skill_source: Path,
        *,
        parent_candidate_id: str | None,
        source_run_id: str | None,
        origin: CandidateOrigin,
        external_source: ExternalSource | None,
        optimizer_provenance: OptimizerProvenance | None = None,
    ) -> CandidateMetadata:
        skill_sha256 = skill_sha256_for_source(skill_source)
        return CandidateMetadata(
            candidate_id=candidate_id,
            skill_id=skill_id,
            parent_candidate_id=parent_candidate_id,
            created_at=datetime.now(UTC),
            source_run_id=source_run_id,
            skill_sha256=skill_sha256,
            origin=origin,
            status=CandidateStatus.DRAFT,
            external_source=external_source,
            optimizer_provenance=optimizer_provenance,
        )
