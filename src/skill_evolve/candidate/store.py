"""Filesystem store for immutable, single-file SQL Skill candidates."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import ValidationError

from skill_evolve.candidate.contracts import (
    CandidateCatalog,
    CandidateContractError,
    CandidateMetadata,
    CandidateStatus,
    CatalogEntry,
    ResolvedCandidate,
    split_candidate_id,
    validate_skill_id,
)
from skill_evolve.optimizer.skill_map import build_legacy_skill_map, build_skill_map
from skill_evolve.trajectory.compressor import canonical_json_bytes

MAX_SKILL_BYTES = 512 * 1024
MAX_SKILL_MAP_BYTES = 2 * 1024 * 1024
MAX_NOTICE_BYTES = 512 * 1024


class CandidateStoreError(ValueError):
    """A local candidate store input or invariant is unsafe."""


class LocalCandidateStore:
    """Own and verify the Git-ignored local candidate store without executing content."""

    def __init__(self, root: Path) -> None:
        self._requested_root = root.absolute()

    @property
    def root(self) -> Path:
        """Return the resolved store root after rejecting a symlinked local root."""

        self._ensure_root_path_safe(create=False)
        return self._requested_root.resolve()

    def initialise(
        self,
        metadata: CandidateMetadata,
        skill_source: Path,
        notice_source: Path | None = None,
    ) -> ResolvedCandidate:
        """Create a baseline candidate in an empty ID slot."""

        if metadata.parent_candidate_id is not None:
            raise CandidateStoreError("initial candidate must not have a parent_candidate_id")
        return self._create_candidate(metadata, skill_source, notice_source, require_parent=False)

    def import_candidate(
        self,
        metadata: CandidateMetadata,
        skill_source: Path,
        notice_source: Path | None = None,
    ) -> ResolvedCandidate:
        """Create an immutable child candidate after verifying its parent lineage."""

        if metadata.parent_candidate_id is None:
            raise CandidateStoreError("imported candidate must declare parent_candidate_id")
        parent = self.resolve_and_verify(metadata.parent_candidate_id)
        if parent.metadata.skill_id != metadata.skill_id:
            raise CandidateStoreError("candidate parent must belong to the same skill_id")
        return self._create_candidate(metadata, skill_source, notice_source, require_parent=True)

    def list_candidates(self, skill_id: str) -> tuple[ResolvedCandidate, ...]:
        """Return only verified candidates for one safe skill identifier."""

        try:
            validate_skill_id(skill_id)
        except CandidateContractError as error:
            raise CandidateStoreError(str(error)) from error
        catalog = self._load_catalog()
        return tuple(
            self._resolve_and_verify_from_catalog(entry.candidate_id, catalog)
            for entry in catalog.entries
            if entry.skill_id == skill_id
        )

    def resolve_and_verify(self, candidate_id: str) -> ResolvedCandidate:
        """Recompute hashes and check catalog, metadata, and complete parent lineage."""

        catalog = self._load_catalog()
        return self._resolve_and_verify_from_catalog(candidate_id, catalog)

    def verify(self, candidate_id: str) -> ResolvedCandidate:
        """Alias that makes verification intent explicit for service and CLI callers."""

        return self.resolve_and_verify(candidate_id)

    def backfill_skill_map(self, candidate_id: str) -> ResolvedCandidate:
        """Add a locally generated Map to one verified legacy candidate."""

        catalog = self._load_catalog()
        candidate = self._resolve_and_verify_from_catalog(
            candidate_id,
            catalog,
            allow_missing_skill_map=True,
        )
        directory = self._candidate_directory(candidate.candidate_id)
        map_path = directory / "skill-map.json"
        if map_path.exists() or map_path.is_symlink():
            raise CandidateStoreError("candidate skill-map.json already exists")
        _write_atomic(map_path, _skill_map_bytes(candidate.skill_bytes))
        return self.resolve_and_verify(candidate_id)

    def rebuild_skill_map(self, candidate_id: str) -> ResolvedCandidate:
        """Replace one candidate Map with the current deterministic format."""

        catalog = self._load_catalog()
        candidate = self._resolve_and_verify_from_catalog(
            candidate_id,
            catalog,
            allow_missing_skill_map=True,
            allow_invalid_skill_map=True,
        )
        map_path = self._candidate_directory(candidate.candidate_id) / "skill-map.json"
        _write_atomic(map_path, _skill_map_bytes(candidate.skill_bytes))
        return self.resolve_and_verify(candidate_id)

    def select(self, candidate_id: str) -> ResolvedCandidate:
        """Select one verified candidate without mutating candidate metadata or content."""

        catalog = self._load_catalog()
        resolved = self._resolve_and_verify_from_catalog(candidate_id, catalog)
        if resolved.catalog_entry.status is CandidateStatus.RETIRED:
            raise CandidateStoreError("retired candidates cannot be selected")
        updated_entries = tuple(
            entry.model_copy(
                update={
                    "status": (
                        CandidateStatus.SELECTED
                        if entry.candidate_id == candidate_id
                        else CandidateStatus.EVALUATED
                        if entry.skill_id == resolved.metadata.skill_id
                        and entry.status is CandidateStatus.SELECTED
                        else entry.status
                    )
                },
            )
            for entry in catalog.entries
        )
        self._write_catalog(CandidateCatalog(entries=updated_entries))
        return self.resolve_and_verify(candidate_id)

    def _create_candidate(
        self,
        metadata: CandidateMetadata,
        skill_source: Path,
        notice_source: Path | None,
        *,
        require_parent: bool,
    ) -> ResolvedCandidate:
        self._ensure_root_path_safe(create=True)
        if require_parent != (metadata.parent_candidate_id is not None):
            raise CandidateStoreError("candidate parent relationship is inconsistent")
        if (metadata.external_source is None) != (notice_source is None):
            raise CandidateStoreError(
                "external candidates require NOTICE.md and local candidates forbid it"
            )
        skill_bytes = _read_text_file(skill_source, MAX_SKILL_BYTES, "SKILL.md source")
        skill_sha256 = hashlib.sha256(skill_bytes).hexdigest()
        if skill_sha256 != metadata.skill_sha256:
            raise CandidateStoreError("SKILL.md source hash does not match candidate metadata")
        skill_map_bytes = _skill_map_bytes(skill_bytes)
        notice_bytes = (
            _read_text_file(notice_source, MAX_NOTICE_BYTES, "NOTICE.md source")
            if notice_source is not None
            else None
        )
        catalog = self._load_catalog_or_empty()
        if any(entry.candidate_id == metadata.candidate_id for entry in catalog.entries):
            raise CandidateStoreError(f"candidate already exists: {metadata.candidate_id}")
        candidate_directory = self._candidate_directory(metadata.candidate_id)
        if candidate_directory.exists() or candidate_directory.is_symlink():
            raise CandidateStoreError(
                f"candidate directory already exists: {metadata.candidate_id}"
            )

        candidate_root = self._candidate_root()
        candidate_root.mkdir(parents=True, exist_ok=True)
        staging_directory = Path(tempfile.mkdtemp(prefix=".candidate-", dir=candidate_root))
        try:
            _write_atomic(staging_directory / "SKILL.md", skill_bytes)
            _write_atomic(staging_directory / "skill-map.json", skill_map_bytes)
            _write_atomic(
                staging_directory / "metadata.json",
                (metadata.model_dump_json(indent=2) + "\n").encode("utf-8"),
            )
            if notice_bytes is not None:
                _write_atomic(staging_directory / "NOTICE.md", notice_bytes)
            candidate_directory.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_directory, candidate_directory)
            entry = CatalogEntry(
                candidate_id=metadata.candidate_id,
                skill_id=metadata.skill_id,
                parent_candidate_id=metadata.parent_candidate_id,
                created_at=metadata.created_at,
                source_run_id=metadata.source_run_id,
                skill_sha256=metadata.skill_sha256,
                status=metadata.status,
            )
            try:
                self._write_catalog(CandidateCatalog(entries=(*catalog.entries, entry)))
            except OSError as error:
                _remove_confined_directory(candidate_directory, candidate_root)
                raise CandidateStoreError(
                    f"could not atomically update candidate catalog: {error}"
                ) from error
        except OSError as error:
            if staging_directory.exists():
                _remove_confined_directory(staging_directory, candidate_root)
            raise CandidateStoreError(f"could not persist candidate: {error}") from error
        return self.resolve_and_verify(metadata.candidate_id)

    def _resolve_and_verify_from_catalog(
        self,
        candidate_id: str,
        catalog: CandidateCatalog,
        *,
        allow_missing_skill_map: bool = False,
        allow_invalid_skill_map: bool = False,
    ) -> ResolvedCandidate:
        try:
            split_candidate_id(candidate_id)
        except CandidateContractError as error:
            raise CandidateStoreError(str(error)) from error
        entries_by_id = {entry.candidate_id: entry for entry in catalog.entries}
        if candidate_id not in entries_by_id:
            raise CandidateStoreError(f"candidate not found: {candidate_id}")
        verified: dict[str, tuple[CandidateMetadata, CatalogEntry, bytes]] = {}
        seen: set[str] = set()
        current_id: str | None = candidate_id
        while current_id is not None:
            if current_id in seen:
                raise CandidateStoreError("candidate lineage contains a cycle")
            seen.add(current_id)
            entry = entries_by_id.get(current_id)
            if entry is None:
                raise CandidateStoreError(f"candidate lineage parent is missing: {current_id}")
            metadata, skill_bytes = self._read_candidate_files(
                current_id,
                allow_missing_skill_map=allow_missing_skill_map,
                allow_invalid_skill_map=allow_invalid_skill_map,
            )
            _verify_catalog_entry(metadata, entry, skill_bytes)
            verified[current_id] = (metadata, entry, skill_bytes)
            current_id = metadata.parent_candidate_id

        metadata, entry, skill_bytes = verified[candidate_id]
        return ResolvedCandidate(metadata=metadata, catalog_entry=entry, skill_bytes=skill_bytes)

    def _read_candidate_files(
        self,
        candidate_id: str,
        *,
        allow_missing_skill_map: bool = False,
        allow_invalid_skill_map: bool = False,
    ) -> tuple[CandidateMetadata, bytes]:
        directory = self._candidate_directory(candidate_id)
        if not directory.is_dir() or directory.is_symlink():
            raise CandidateStoreError(f"candidate directory is missing or unsafe: {candidate_id}")
        skill_path = directory / "SKILL.md"
        metadata_path = directory / "metadata.json"
        skill_bytes = _read_text_file(skill_path, MAX_SKILL_BYTES, "candidate SKILL.md")
        if not _is_regular_file(metadata_path):
            raise CandidateStoreError(f"candidate metadata is missing or unsafe: {candidate_id}")
        try:
            metadata = CandidateMetadata.model_validate_json(metadata_path.read_bytes())
        except (OSError, ValidationError, ValueError) as error:
            raise CandidateStoreError(
                f"invalid candidate metadata for {candidate_id}: {error}"
            ) from error
        map_path = directory / "skill-map.json"
        if not map_path.exists() and allow_missing_skill_map:
            skill_map_present = False
        else:
            skill_map_present = True
            if not allow_invalid_skill_map:
                _validate_skill_map(map_path, skill_bytes, candidate_id)
        notice_path = directory / "NOTICE.md"
        allowed_names = {"SKILL.md", "metadata.json"}
        if skill_map_present:
            allowed_names.add("skill-map.json")
        if metadata.external_source is not None:
            if not _is_regular_file(notice_path):
                raise CandidateStoreError(
                    f"external candidate NOTICE.md is missing or unsafe: {candidate_id}"
                )
            _read_text_file(notice_path, MAX_NOTICE_BYTES, "candidate NOTICE.md")
            allowed_names.add("NOTICE.md")
        elif notice_path.exists():
            raise CandidateStoreError(f"local candidate must not contain NOTICE.md: {candidate_id}")
        try:
            actual_names = {child.name for child in directory.iterdir()}
        except OSError as error:
            raise CandidateStoreError(
                f"could not inspect candidate directory: {candidate_id}"
            ) from error
        if actual_names != allowed_names:
            raise CandidateStoreError(
                "candidate directory has an invalid file set"
            )
        return metadata, skill_bytes

    def _load_catalog_or_empty(self) -> CandidateCatalog:
        catalog_path = self._catalog_path()
        if not catalog_path.exists():
            return CandidateCatalog()
        return self._load_catalog()

    def _load_catalog(self) -> CandidateCatalog:
        self._ensure_root_path_safe(create=False)
        catalog_path = self._catalog_path()
        if not _is_regular_file(catalog_path):
            raise CandidateStoreError("candidate store has not been initialized")
        try:
            return CandidateCatalog.model_validate_json(catalog_path.read_bytes())
        except (OSError, ValidationError, ValueError) as error:
            raise CandidateStoreError(f"invalid candidate catalog: {error}") from error

    def _write_catalog(self, catalog: CandidateCatalog) -> None:
        self._ensure_root_path_safe(create=True)
        _write_atomic(
            self._catalog_path(),
            (catalog.model_dump_json(indent=2) + "\n").encode("utf-8"),
        )

    def _ensure_root_path_safe(self, *, create: bool) -> None:
        parent = self._requested_root.parent
        if parent.exists() and parent.is_symlink():
            raise CandidateStoreError("candidate store parent must not be a symlink")
        if self._requested_root.exists() and self._requested_root.is_symlink():
            raise CandidateStoreError("candidate store root must not be a symlink")
        if create:
            self._requested_root.mkdir(parents=True, exist_ok=True)
            if self._requested_root.is_symlink():
                raise CandidateStoreError("candidate store root must not be a symlink")

    def _catalog_path(self) -> Path:
        return self._confined_path(self.root / "catalog.json")

    def _candidate_root(self) -> Path:
        root = self._confined_path(self.root / "candidates")
        if root.exists() and root.is_symlink():
            raise CandidateStoreError("candidate directory root must not be a symlink")
        return root

    def _candidate_directory(self, candidate_id: str) -> Path:
        try:
            skill_id, candidate_name = split_candidate_id(candidate_id)
        except CandidateContractError as error:
            raise CandidateStoreError(str(error)) from error
        return self._confined_path(self._candidate_root() / skill_id / candidate_name)

    def _confined_path(self, path: Path) -> Path:
        root = self.root
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise CandidateStoreError(
                "candidate path must remain inside the local store"
            ) from error
        return resolved


def _verify_catalog_entry(
    metadata: CandidateMetadata, entry: CatalogEntry, skill_bytes: bytes
) -> None:
    if (
        metadata.candidate_id != entry.candidate_id
        or metadata.skill_id != entry.skill_id
        or metadata.parent_candidate_id != entry.parent_candidate_id
        or metadata.created_at != entry.created_at
        or metadata.source_run_id != entry.source_run_id
        or metadata.skill_sha256 != entry.skill_sha256
    ):
        raise CandidateStoreError("candidate metadata and catalog entry are inconsistent")
    actual_hash = hashlib.sha256(skill_bytes).hexdigest()
    if actual_hash != metadata.skill_sha256:
        raise CandidateStoreError("candidate SKILL.md hash does not match metadata or catalog")


def _read_text_file(path: Path, maximum_size: int, label: str) -> bytes:
    if not _is_regular_file(path):
        raise CandidateStoreError(f"{label} must be a regular non-symlink file")
    try:
        size = path.stat().st_size
        if size <= 0 or size > maximum_size:
            raise CandidateStoreError(f"{label} must be between 1 and {maximum_size} bytes")
        content = path.read_bytes()
    except OSError as error:
        raise CandidateStoreError(f"could not read {label}: {error}") from error
    if b"\x00" in content:
        raise CandidateStoreError(f"{label} must be text, not binary")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateStoreError(f"{label} must be valid UTF-8 text") from error
    return content


def skill_sha256_for_source(path: Path) -> str:
    """Hash a prospective Skill only after applying the store's text-file limits."""

    return hashlib.sha256(_read_text_file(path, MAX_SKILL_BYTES, "SKILL.md source")).hexdigest()


def _skill_map_bytes(skill_bytes: bytes) -> bytes:
    try:
        skill_map = build_skill_map(skill_bytes, max_input_bytes=MAX_SKILL_BYTES)
    except ValueError as error:
        raise CandidateStoreError(f"could not build candidate skill-map: {error}") from error
    return canonical_json_bytes(skill_map.model_dump(mode="json")) + b"\n"


def _legacy_skill_map_bytes(skill_bytes: bytes) -> bytes:
    try:
        skill_map = build_legacy_skill_map(skill_bytes, max_input_bytes=MAX_SKILL_BYTES)
    except ValueError as error:
        raise CandidateStoreError(f"could not build legacy candidate skill-map: {error}") from error
    return canonical_json_bytes(skill_map.model_dump(mode="json")) + b"\n"


def _validate_skill_map(map_path: Path, skill_bytes: bytes, candidate_id: str) -> None:
    map_bytes = _read_text_file(map_path, MAX_SKILL_MAP_BYTES, "candidate skill-map.json")
    if map_bytes not in {_skill_map_bytes(skill_bytes), _legacy_skill_map_bytes(skill_bytes)}:
        raise CandidateStoreError("candidate skill-map does not match candidate SKILL.md")


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _remove_confined_directory(path: Path, root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise CandidateStoreError("refusing to remove directory outside candidate root") from error
    if resolved_path.is_dir() and not resolved_path.is_symlink():
        shutil.rmtree(resolved_path)
