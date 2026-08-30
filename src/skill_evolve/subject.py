"""Immutable, non-executable input supplied to a benchmark run."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class RunSubject:
    """Opaque evaluated content, independent of candidate-store provenance.

    The bytes are data only. Runners may materialise them as one read-only text
    artifact but must never import or execute them.
    """

    subject_id: str
    content_sha256: str
    content: bytes
    filename: str = "SKILL.md"

    def __post_init__(self) -> None:
        if not self.subject_id or "\x00" in self.subject_id or len(self.subject_id) > 256:
            raise ValueError("subject_id must be a non-empty opaque identifier")
        if _SHA256_PATTERN.fullmatch(self.content_sha256) is None:
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if hashlib.sha256(self.content).hexdigest() != self.content_sha256:
            raise ValueError("subject content does not match content_sha256")
        if _FILENAME_PATTERN.fullmatch(self.filename) is None:
            raise ValueError("subject filename must be a safe single path component")

    def public_binding(self) -> dict[str, str]:
        return {"subject_id": self.subject_id, "content_sha256": self.content_sha256}
