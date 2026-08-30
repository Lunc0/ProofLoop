"""Candidate-store boundary adapter for generic benchmark subjects."""

from __future__ import annotations

from skill_evolve.candidate.contracts import ResolvedCandidate
from skill_evolve.subject import RunSubject


def subject_from_candidate(candidate: ResolvedCandidate) -> RunSubject:
    """Expose verified Skill bytes without exposing candidate provenance downstream."""

    return RunSubject(
        subject_id=candidate.candidate_id,
        content_sha256=candidate.skill_sha256,
        content=candidate.skill_bytes,
    )
