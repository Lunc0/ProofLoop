"""Custody-owned rolling LiveSQLBench selection and loading.

The rotation root is deliberately outside the repository.  It keeps the
complete selected manifests and reserve identity private while exposing only
an already-authorised manifest ID to the benchmark runner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from skill_evolve.benchmark.contracts import JsonObject, JsonValue
from skill_evolve.benchmark.livesqlbench import (
    LiveSQLBenchCase,
    LiveSQLBenchDataError,
    LiveSQLBenchMaterials,
    build_livesqlbench_case,
    load_livesqlbench_materials,
)
from skill_evolve.benchmark.livesqlbench_heldout import (
    HeldOutError,
    HeldOutManifest,
    load_heldout_manifest,
    resolve_heldout_custody,
)

RotationTier: TypeAlias = Literal["Simple", "Moderate", "Challenging"]
RotationKind: TypeAlias = Literal["smoke", "evolution"]
RotationAdapterId: TypeAlias = Literal[
    "livesqlbench-base-lite-smoke-rotation-v1",
    "livesqlbench-base-lite-evolution-v1",
]

LIVE_SQL_BENCH_BASE_LITE_SMOKE_ROTATION_ADAPTER_ID: Literal[
    "livesqlbench-base-lite-smoke-rotation-v1"
] = "livesqlbench-base-lite-smoke-rotation-v1"
LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID: Literal[
    "livesqlbench-base-lite-evolution-v1"
] = "livesqlbench-base-lite-evolution-v1"
ROTATION_ROOT_ENVIRONMENT_VARIABLE = "SKILL_EVOLVE_ROTATION_ROOT"
ROTATION_MANIFEST_SCHEMA_VERSION: Literal["v1"] = "v1"
ROTATION_RESERVE_FILENAME = "livesqlbench-base-lite-rotation-reserve-v1.json"
ROTATION_TIERS: tuple[RotationTier, ...] = ("Simple", "Moderate", "Challenging")
SMOKE_CASE_COUNT = 5
EVOLUTION_CASE_COUNT = 15
RESERVE_CASES_PER_TIER = 5

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_MANIFEST_ID_PATTERN = re.compile(r"^(?:smoke|evolution)-[0-9a-f]{24}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 128 * 1024
_SMOKE_QUOTAS: tuple[dict[RotationTier, int], ...] = (
    {"Simple": 2, "Moderate": 2, "Challenging": 1},
    {"Simple": 2, "Moderate": 1, "Challenging": 2},
    {"Simple": 1, "Moderate": 2, "Challenging": 2},
)
_RotationRecord = TypeVar("_RotationRecord", bound="StrictRotationModel")


class RotationError(ValueError):
    """A rolling evaluator input, manifest, or custody root is unsafe."""


class StrictRotationModel(BaseModel):
    """Persisted rotation records accept no implicit coercion or extra data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RotationManifestCase(StrictRotationModel):
    """One Query selection retained in evaluator rotation custody."""

    instance_id: str
    category: Literal["Query"]
    difficulty_tier: RotationTier
    selected_database: str
    order_matters: bool

    @field_validator("instance_id", "selected_database")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if _IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a safe identifier")
        return value


class RotationReserve(StrictRotationModel):
    """Private fixed exclusion set for the rolling development pool."""

    schema_version: Literal["v1"]
    public_data_sha256: str
    final_manifest_sha256: str
    cases: tuple[RotationManifestCase, ...]
    reserve_sha256: str

    @field_validator("public_data_sha256", "final_manifest_sha256", "reserve_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_selection(self) -> RotationReserve:
        _validate_unique_case_ids(self.cases)
        counts = Counter(case.difficulty_tier for case in self.cases)
        if any(counts[tier] != RESERVE_CASES_PER_TIER for tier in ROTATION_TIERS):
            raise ValueError("must contain five cases in every tier")
        return self


class RotationManifest(StrictRotationModel):
    """Private immutable selection for one authorised rolling execution."""

    schema_version: Literal["v1"]
    adapter_id: Literal[
        "livesqlbench-base-lite-smoke-rotation-v1",
        "livesqlbench-base-lite-evolution-v1",
    ]
    manifest_id: str
    selection_index: int
    public_data_sha256: str
    final_manifest_sha256: str
    reserve_sha256: str
    cases: tuple[RotationManifestCase, ...]
    manifest_sha256: str

    @field_validator("manifest_id")
    @classmethod
    def validate_manifest_id(cls, value: str) -> str:
        if _MANIFEST_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a generated rotation manifest ID")
        return value

    @field_validator(
        "public_data_sha256", "final_manifest_sha256", "reserve_sha256", "manifest_sha256"
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("selection_index")
    @classmethod
    def validate_selection_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_selection(self) -> RotationManifest:
        _validate_unique_case_ids(self.cases)
        expected = quota_for_adapter(self.adapter_id, self.selection_index)
        counts = Counter(case.difficulty_tier for case in self.cases)
        if counts != expected:
            raise ValueError("does not match the required tier quota")
        return self


@dataclass(frozen=True)
class RotationCustody:
    """The external evaluator-owned location for rotation state and evidence."""

    root: Path

    @property
    def reserve_path(self) -> Path:
        return self.root / ROTATION_RESERVE_FILENAME

    @property
    def manifests_root(self) -> Path:
        return self.root / "manifests"

    @property
    def gate_root(self) -> Path:
        return self.root / "smoke-gates"


@dataclass(frozen=True)
class RotationManifestReceipt:
    """The safe terminal representation of a newly created manifest."""

    adapter_id: str
    manifest_id: str
    manifest_sha256: str
    tier_counts: dict[str, int]

    def public_dict(self) -> JsonObject:
        tier_counts = _json_tier_counts(self.tier_counts)
        return {
            "adapter_id": self.adapter_id,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "tier_counts": tier_counts,
        }


@dataclass(frozen=True)
class RotationReserveReceipt:
    """The safe terminal representation of a created reserve."""

    reserve_sha256: str
    tier_counts: dict[str, int]

    def public_dict(self) -> JsonObject:
        return {
            "reserve_sha256": self.reserve_sha256,
            "tier_counts": _json_tier_counts(self.tier_counts),
        }


@dataclass(frozen=True)
class RotationLiveSQLBenchSuite:
    """A validated rolling selection whose identity remains in custody."""

    custody: RotationCustody
    manifest: RotationManifest
    data_root: Path
    public_data_file: Path
    gt_file: Path
    dump_root: Path
    cases: tuple[LiveSQLBenchCase, ...]

    @property
    def adapter_id(self) -> str:
        return self.manifest.adapter_id

    @property
    def kind(self) -> RotationKind:
        return kind_for_adapter(self.adapter_id)

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.instance_id for case in self.cases)

    def public_summary(self) -> JsonObject:
        tier_counts = _json_tier_counts(_tier_counts(self.manifest.cases))
        return {
            "adapter_id": self.adapter_id,
            "manifest_id": self.manifest.manifest_id,
            "manifest_sha256": self.manifest.manifest_sha256,
            "case_count": len(self.cases),
            "tier_counts": tier_counts,
        }

    def source_digests(self) -> dict[str, str]:
        paths: set[Path] = {self.public_data_file, self.gt_file}
        for case in self.cases:
            paths.update(case.public_context_files)
            dump_directory = self.dump_root / f"{case.database_name}_template"
            paths.update(dump_directory.glob("*.sql"))
            paths.add(self.dump_root / f"{case.database_name}_table_orders.txt")
        return {
            path.relative_to(self.data_root).as_posix(): _sha256_file(path)
            for path in sorted(paths)
        }


@dataclass(frozen=True)
class _PublicCaseMetadata:
    instance_id: str
    difficulty_tier: RotationTier
    selected_database: str
    order_matters: bool


def adapter_for_kind(kind: RotationKind) -> RotationAdapterId:
    if kind == "smoke":
        return LIVE_SQL_BENCH_BASE_LITE_SMOKE_ROTATION_ADAPTER_ID
    return LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID


def kind_for_adapter(adapter_id: str) -> RotationKind:
    if adapter_id == LIVE_SQL_BENCH_BASE_LITE_SMOKE_ROTATION_ADAPTER_ID:
        return "smoke"
    if adapter_id == LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID:
        return "evolution"
    raise RotationError("rotation adapter ID is invalid")


def quota_for_adapter(adapter_id: str, selection_index: int) -> dict[RotationTier, int]:
    kind = kind_for_adapter(adapter_id)
    if selection_index < 0:
        raise RotationError("rotation selection index is invalid")
    if kind == "smoke":
        return dict(_SMOKE_QUOTAS[selection_index % len(_SMOKE_QUOTAS)])
    return {tier: RESERVE_CASES_PER_TIER for tier in ROTATION_TIERS}


def resolve_rotation_custody(
    *, repository_root: Path, candidate_store_root: Path, require_write: bool
) -> RotationCustody:
    """Resolve an existing external root without treating documentation as ACLs."""

    configured = os.environ.get(ROTATION_ROOT_ENVIRONMENT_VARIABLE)
    if configured is None or not configured.strip():
        raise RotationError("rotation evaluator custody root is unavailable")
    try:
        supplied = Path(configured)
        if not supplied.is_absolute():
            raise RotationError("rotation evaluator custody root is unavailable")
        root = _existing_directory_without_symlinks(supplied)
        prohibited = (_comparison_path(repository_root), _comparison_path(candidate_store_root))
        if any(_paths_overlap(root, path) for path in prohibited):
            raise RotationError("rotation evaluator custody root is unavailable")
        heldout = resolve_heldout_custody(
            repository_root=repository_root,
            candidate_store_root=candidate_store_root,
            require_write=False,
        )
        if _paths_overlap(root, heldout.root):
            raise RotationError("rotation evaluator custody root is unavailable")
        required_access = os.R_OK | os.X_OK
        if require_write:
            required_access |= os.W_OK
        if not os.access(root, required_access):
            raise RotationError("rotation evaluator custody root is unavailable")
    except (HeldOutError, OSError, ValueError):
        raise RotationError("rotation evaluator custody root is unavailable") from None
    return RotationCustody(root=root)


def initialise_rotation_reserve(
    data_root: Path, *, repository_root: Path, candidate_store_root: Path
) -> RotationReserveReceipt:
    """Create the one private five-per-tier reserve without exposing identities."""

    custody = resolve_rotation_custody(
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
        require_write=True,
    )
    if os.path.lexists(custody.reserve_path):
        raise RotationError("rotation reserve already exists")
    try:
        final_manifest = _load_final_manifest(repository_root, candidate_store_root)
        _, records, public_digest = _load_public_metadata(data_root)
        final_case_ids = {case.instance_id for case in final_manifest.cases}
        selected = _select_records(records, final_case_ids, _reserve_quota())
        draft = RotationReserve(
            schema_version=ROTATION_MANIFEST_SCHEMA_VERSION,
            public_data_sha256=public_digest,
            final_manifest_sha256=final_manifest.manifest_sha256,
            cases=tuple(_manifest_case(record) for record in selected),
            reserve_sha256="0" * 64,
        )
        reserve = draft.model_copy(update={"reserve_sha256": _digest(draft, "reserve_sha256")})
        _validate_selected_materials(data_root, reserve.cases)
        _write_atomically(custody.reserve_path, _record_bytes(reserve), "rotation reserve")
    except RotationError:
        raise
    except (OSError, ValidationError, ValueError):
        raise RotationError("rotation reserve could not be initialised") from None
    return RotationReserveReceipt(reserve.reserve_sha256, _tier_counts(reserve.cases))


def create_rotation_manifest(
    data_root: Path,
    kind: RotationKind,
    *,
    repository_root: Path,
    candidate_store_root: Path,
) -> RotationManifestReceipt:
    """Select and atomically freeze one new manifest with no caller-controlled cases."""

    custody = resolve_rotation_custody(
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
        require_write=True,
    )
    try:
        reserve = load_rotation_reserve(custody)
        final_manifest = _load_final_manifest(repository_root, candidate_store_root)
        if final_manifest.manifest_sha256 != reserve.final_manifest_sha256:
            raise RotationError("rotation reserve does not match final custody")
        _, records, public_digest = _load_public_metadata(data_root)
        if public_digest != reserve.public_data_sha256:
            raise RotationError("rotation public data integrity check failed")
        selection_index = _next_selection_index(custody, kind)
        adapter_id = adapter_for_kind(kind)
        quota = quota_for_adapter(adapter_id, selection_index)
        excluded = {case.instance_id for case in final_manifest.cases}
        excluded.update(case.instance_id for case in reserve.cases)
        selected = _select_records(records, excluded, quota)
        manifest_id = f"{kind}-{secrets.token_hex(12)}"
        draft = RotationManifest(
            schema_version=ROTATION_MANIFEST_SCHEMA_VERSION,
            adapter_id=adapter_id,
            manifest_id=manifest_id,
            selection_index=selection_index,
            public_data_sha256=public_digest,
            final_manifest_sha256=final_manifest.manifest_sha256,
            reserve_sha256=reserve.reserve_sha256,
            cases=tuple(_manifest_case(record) for record in selected),
            manifest_sha256="0" * 64,
        )
        manifest = draft.model_copy(update={"manifest_sha256": _digest(draft, "manifest_sha256")})
        _validate_selected_materials(data_root, manifest.cases)
        custody.manifests_root.mkdir(exist_ok=True)
        if custody.manifests_root.is_symlink() or not custody.manifests_root.is_dir():
            raise RotationError("rotation manifests directory is invalid")
        _write_atomically(
            _manifest_path(custody, manifest.manifest_id),
            _record_bytes(manifest),
            "rotation manifest",
        )
    except RotationError:
        raise
    except (OSError, ValidationError, ValueError):
        raise RotationError("rotation manifest could not be created") from None
    return RotationManifestReceipt(
        adapter_id=manifest.adapter_id,
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        tier_counts=_tier_counts(manifest.cases),
    )


def load_rotation_reserve(custody: RotationCustody) -> RotationReserve:
    """Load and verify a private reserve before selecting or loading manifests."""

    return _load_record(custody.reserve_path, RotationReserve, "rotation reserve", "reserve_sha256")


def load_rotation_manifest(custody: RotationCustody, manifest_id: str) -> RotationManifest:
    """Load one requested manifest only; callers cannot enumerate the pool."""

    if _MANIFEST_ID_PATTERN.fullmatch(manifest_id) is None:
        raise RotationError("rotation manifest ID is invalid")
    if custody.manifests_root.is_symlink() or not custody.manifests_root.is_dir():
        raise RotationError("rotation manifests directory is invalid")
    manifest = _load_record(
        _manifest_path(custody, manifest_id),
        RotationManifest,
        "rotation manifest",
        "manifest_sha256",
    )
    if manifest.manifest_id != manifest_id:
        raise RotationError("rotation manifest integrity check failed")
    return manifest


def load_rotation_suite(
    data_root: Path,
    manifest_id: str,
    *,
    repository_root: Path,
    candidate_store_root: Path,
) -> RotationLiveSQLBenchSuite:
    """Validate one authorised rolling selection and retain GT only in memory."""

    custody = resolve_rotation_custody(
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
        require_write=False,
    )
    try:
        reserve = load_rotation_reserve(custody)
        final_manifest = _load_final_manifest(repository_root, candidate_store_root)
        manifest = load_rotation_manifest(custody, manifest_id)
        if (
            manifest.final_manifest_sha256 != final_manifest.manifest_sha256
            or manifest.reserve_sha256 != reserve.reserve_sha256
            or reserve.final_manifest_sha256 != final_manifest.manifest_sha256
        ):
            raise RotationError("rotation manifest custody binding is invalid")
        _, _, public_digest = _load_public_metadata(data_root)
        if (
            public_digest != manifest.public_data_sha256
            or public_digest != reserve.public_data_sha256
        ):
            raise RotationError("rotation public data integrity check failed")
        prohibited = {case.instance_id for case in final_manifest.cases}
        prohibited.update(case.instance_id for case in reserve.cases)
        if prohibited.intersection(case.instance_id for case in manifest.cases):
            raise RotationError("rotation manifest selection is invalid")
        materials, cases = _validate_selected_materials(data_root, manifest.cases)
    except RotationError:
        raise
    except (OSError, ValidationError, ValueError):
        raise RotationError("rotation source data is invalid") from None
    return RotationLiveSQLBenchSuite(
        custody=custody,
        manifest=manifest,
        data_root=materials.data_root,
        public_data_file=materials.public_data_file,
        gt_file=materials.gt_file,
        dump_root=materials.dump_root,
        cases=cases,
    )


def _load_final_manifest(repository_root: Path, candidate_store_root: Path) -> HeldOutManifest:
    try:
        custody = resolve_heldout_custody(
            repository_root=repository_root,
            candidate_store_root=candidate_store_root,
            require_write=False,
        )
        return load_heldout_manifest(custody)
    except HeldOutError as error:
        raise RotationError("final held-out custody is unavailable") from error


def _load_public_metadata(
    data_root: Path,
) -> tuple[Path, tuple[_PublicCaseMetadata, ...], str]:
    try:
        root = _existing_directory_without_symlinks(data_root)
        public_directory = root / "public"
        public_data_file = public_directory / "livesqlbench_data.jsonl"
        _require_path_without_symlinks(root, public_directory, directory=True)
        _require_path_without_symlinks(root, public_data_file, directory=False)
        payload = public_data_file.read_bytes()
        if not payload or len(payload) > 64 * 1024 * 1024:
            raise ValueError("invalid public data")
        records: list[_PublicCaseMetadata] = []
        seen_ids: set[str] = set()
        for line in payload.decode("utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, Mapping) or item.get("category") != "Query":
                continue
            instance_id = item.get("instance_id")
            tier = item.get("difficulty_tier")
            database = item.get("selected_database")
            conditions = item.get("conditions")
            if (
                not isinstance(instance_id, str)
                or _IDENTIFIER_PATTERN.fullmatch(instance_id) is None
                or instance_id in seen_ids
                or tier not in ROTATION_TIERS
                or not isinstance(database, str)
                or _IDENTIFIER_PATTERN.fullmatch(database) is None
                or not isinstance(conditions, Mapping)
                or not isinstance(conditions.get("order"), bool)
            ):
                raise ValueError("invalid Query metadata")
            seen_ids.add(instance_id)
            records.append(_PublicCaseMetadata(instance_id, tier, database, conditions["order"]))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RotationError("rotation public metadata is invalid") from error
    if not records:
        raise RotationError("rotation public metadata is invalid")
    return public_data_file, tuple(records), hashlib.sha256(payload).hexdigest()


def _select_records(
    records: Iterable[_PublicCaseMetadata],
    excluded_ids: set[str],
    quota: Mapping[RotationTier, int],
) -> tuple[_PublicCaseMetadata, ...]:
    candidates: dict[RotationTier, list[_PublicCaseMetadata]] = {
        tier: [] for tier in ROTATION_TIERS
    }
    for record in records:
        if record.instance_id not in excluded_ids:
            candidates[record.difficulty_tier].append(record)
    random_source = secrets.SystemRandom()
    selected: list[_PublicCaseMetadata] = []
    for tier in ROTATION_TIERS:
        required = quota[tier]
        if len(candidates[tier]) < required:
            raise RotationError("rotation pool cannot satisfy tier quota")
        selected.extend(random_source.sample(candidates[tier], required))
    return tuple(selected)


def _reserve_quota() -> dict[RotationTier, int]:
    return {tier: RESERVE_CASES_PER_TIER for tier in ROTATION_TIERS}


def _manifest_case(record: _PublicCaseMetadata) -> RotationManifestCase:
    return RotationManifestCase(
        instance_id=record.instance_id,
        category="Query",
        difficulty_tier=record.difficulty_tier,
        selected_database=record.selected_database,
        order_matters=record.order_matters,
    )


def _validate_selected_materials(
    data_root: Path, selections: tuple[RotationManifestCase, ...]
) -> tuple[LiveSQLBenchMaterials, tuple[LiveSQLBenchCase, ...]]:
    try:
        materials = load_livesqlbench_materials(data_root)
        _require_path_without_symlinks(materials.data_root, materials.gt_file, directory=False)
        _require_path_without_symlinks(materials.data_root, materials.dump_root, directory=True)
        cases = tuple(build_livesqlbench_case(materials, item.instance_id) for item in selections)
        for selected, case in zip(selections, cases, strict=True):
            if (
                case.database_name != selected.selected_database
                or case.difficulty_tier != selected.difficulty_tier
                or case.order_matters != selected.order_matters
            ):
                raise RotationError("rotation manifest metadata does not match source data")
            for path in case.public_context_files:
                _require_path_without_symlinks(materials.data_root, path, directory=False)
            _require_path_without_symlinks(
                materials.data_root,
                materials.dump_root / f"{case.database_name}_template",
                directory=True,
            )
    except RotationError:
        raise
    except (LiveSQLBenchDataError, OSError, ValueError):
        raise RotationError("rotation source data is invalid") from None
    return materials, cases


def _next_selection_index(custody: RotationCustody, kind: RotationKind) -> int:
    root = custody.manifests_root
    if not root.exists():
        return 0
    if root.is_symlink() or not root.is_dir():
        raise RotationError("rotation manifests directory is invalid")
    count = 0
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise RotationError("rotation manifests directory is invalid")
        manifest = _load_record(path, RotationManifest, "rotation manifest", "manifest_sha256")
        if kind_for_adapter(manifest.adapter_id) == kind:
            count += 1
    return count


def _manifest_path(custody: RotationCustody, manifest_id: str) -> Path:
    if _MANIFEST_ID_PATTERN.fullmatch(manifest_id) is None:
        raise RotationError("rotation manifest ID is invalid")
    return custody.manifests_root / f"{manifest_id}.json"


def _load_record(
    path: Path,
    model: type[_RotationRecord],
    label: str,
    digest_field: str,
) -> _RotationRecord:
    try:
        if path.is_symlink() or not path.is_file():
            raise RotationError(f"{label} is unavailable")
        payload = path.read_bytes()
        if not payload or len(payload) > _MAX_MANIFEST_BYTES or b"\x00" in payload:
            raise RotationError(f"{label} is invalid")
        record = model.model_validate_json(payload)
        digest = getattr(record, digest_field)
        if not isinstance(digest, str) or digest != _digest(record, digest_field):
            raise RotationError(f"{label} integrity check failed")
    except RotationError:
        raise
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise RotationError(f"{label} is invalid") from None
    return record


def _digest(record: StrictRotationModel, field: str) -> str:
    payload = record.model_dump(mode="json", exclude={field})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_bytes(record: StrictRotationModel) -> bytes:
    return (
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def _write_atomically(path: Path, payload: bytes, label: str) -> None:
    if os.path.lexists(path):
        raise RotationError(f"{label} already exists")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise RotationError(f"{label} already exists") from None
    except OSError:
        raise RotationError(f"{label} could not be persisted") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _validate_unique_case_ids(cases: tuple[RotationManifestCase, ...]) -> None:
    if len(cases) != len({case.instance_id for case in cases}):
        raise ValueError("must not contain duplicate cases")


def _tier_counts(cases: Iterable[RotationManifestCase]) -> dict[str, int]:
    counts = Counter(case.difficulty_tier for case in cases)
    return {tier: counts[tier] for tier in ROTATION_TIERS}


def _json_tier_counts(counts: Mapping[str, int]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for tier, count in counts.items():
        result[tier] = count
    return result


def _existing_directory_without_symlinks(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise RotationError("rotation evaluator custody root is unavailable")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise RotationError("rotation evaluator custody root is unavailable")
    _reject_symlink_components(resolved)
    return resolved


def _require_path_without_symlinks(root: Path, path: Path, *, directory: bool) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RotationError("rotation source data is invalid") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RotationError("rotation source data is invalid")
    if directory:
        if not path.is_dir():
            raise RotationError("rotation source data is invalid")
    elif not path.is_file():
        raise RotationError("rotation source data is invalid")


def _reject_symlink_components(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise RotationError("rotation evaluator custody root is unavailable")
        current = current.parent


def _comparison_path(path: Path) -> Path:
    return path.resolve(strict=False)


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        try:
            second.relative_to(first)
            return True
        except ValueError:
            return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
