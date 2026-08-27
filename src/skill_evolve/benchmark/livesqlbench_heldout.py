"""Custody-only contracts and loading for the LiveSQLBench final held-out suite.

The manifest is deliberately stored outside the repository.  This module never
prints or serialises a case list through a public adapter contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from skill_evolve.benchmark.contracts import JsonObject
from skill_evolve.benchmark.livesqlbench import (
    APPROVED_BASE_LITE_QUERY_IDS,
    LiveSQLBenchCase,
    LiveSQLBenchDataError,
    LiveSQLBenchMaterials,
    build_livesqlbench_case,
    load_livesqlbench_materials,
)

HeldOutTier: TypeAlias = Literal["Simple", "Moderate", "Challenging"]

LIVE_SQL_BENCH_BASE_LITE_HELDOUT_ADAPTER_ID: Literal[
    "livesqlbench-base-lite-query-final-v1"
] = "livesqlbench-base-lite-query-final-v1"
HELDOUT_ROOT_ENVIRONMENT_VARIABLE = "SKILL_EVOLVE_HELDOUT_ROOT"
HELDOUT_MANIFEST_FILENAME = "livesqlbench-base-lite-query-final-v1.json"
HELDOUT_MANIFEST_SCHEMA_VERSION: Literal["v1"] = "v1"
HELDOUT_CASE_COUNT = 30
HELDOUT_DATABASE_COUNT = 18
HELDOUT_TIERS: tuple[HeldOutTier, ...] = ("Simple", "Moderate", "Challenging")

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_PUBLIC_DATA_BYTES = 64 * 1024 * 1024


class HeldOutError(ValueError):
    """A held-out input or custody boundary is unsafe or incomplete.

    Public callers receive deliberately generic messages so an invalid manifest
    cannot disclose its selected case identities through stderr.
    """


class StrictHeldOutModel(BaseModel):
    """Persisted custody records reject coercion and unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HeldOutManifestCase(StrictHeldOutModel):
    """Public metadata retained beside one private held-out case identity."""

    instance_id: str
    category: Literal["Query"]
    difficulty_tier: HeldOutTier
    selected_database: str
    order_matters: bool

    @field_validator("instance_id", "selected_database")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if _IDENTIFIER_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a safe identifier")
        return value


class HeldOutManifest(StrictHeldOutModel):
    """Immutable evaluator-owned case selection, never a repository asset."""

    schema_version: Literal["v1"]
    suite_id: Literal["livesqlbench-base-lite-query-final-v1"]
    public_data_sha256: str
    cases: tuple[HeldOutManifestCase, ...]
    manifest_sha256: str

    @field_validator("public_data_sha256", "manifest_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_selection_constraints(self) -> HeldOutManifest:
        if len(self.cases) != HELDOUT_CASE_COUNT:
            raise ValueError("must contain exactly thirty cases")
        case_ids = [case.instance_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("must not contain duplicate cases")
        if set(case_ids).intersection(APPROVED_BASE_LITE_QUERY_IDS):
            raise ValueError("must not contain smoke-suite cases")
        tier_counts = Counter(case.difficulty_tier for case in self.cases)
        if any(tier_counts[tier] != 10 for tier in HELDOUT_TIERS):
            raise ValueError("must contain ten cases in every tier")
        for tier in HELDOUT_TIERS:
            tier_databases = [
                case.selected_database for case in self.cases if case.difficulty_tier == tier
            ]
            if len(tier_databases) != len(set(tier_databases)):
                raise ValueError("each tier must use ten different databases")
        database_counts = Counter(case.selected_database for case in self.cases)
        if len(database_counts) != HELDOUT_DATABASE_COUNT:
            raise ValueError("must cover exactly eighteen databases")
        if any(count > 2 for count in database_counts.values()):
            raise ValueError("a database may appear at most twice")
        return self


@dataclass(frozen=True)
class HeldOutCustody:
    """Resolved local root dedicated to evaluator-held secrets and evidence."""

    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / HELDOUT_MANIFEST_FILENAME

    @property
    def runs_root(self) -> Path:
        return self.root / "runs"

    @property
    def dsh_session_logs_root(self) -> Path:
        return self.root / "dsh-session-logs"


@dataclass(frozen=True)
class HeldOutManifestReceipt:
    """The only successful initialisation data intended for terminal output."""

    suite_id: str
    manifest_sha256: str

    def public_dict(self) -> JsonObject:
        return {
            "suite_id": self.suite_id,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True)
class _PublicCaseMetadata:
    instance_id: str
    difficulty_tier: HeldOutTier
    selected_database: str
    order_matters: bool


@dataclass(frozen=True)
class HeldOutLiveSQLBenchSuite:
    """Validated selected cases with Gold data retained only in evaluator memory."""

    custody: HeldOutCustody
    manifest: HeldOutManifest
    data_root: Path
    public_data_file: Path
    gt_file: Path
    dump_root: Path
    cases: tuple[LiveSQLBenchCase, ...]

    @property
    def adapter_id(self) -> str:
        return LIVE_SQL_BENCH_BASE_LITE_HELDOUT_ADAPTER_ID

    def public_summary(self) -> JsonObject:
        """Return aggregate metadata without the manifest or case identities."""

        return {
            "suite_id": self.manifest.suite_id,
            "manifest_sha256": self.manifest.manifest_sha256,
            "case_count": HELDOUT_CASE_COUNT,
            "tiers": {tier: 10 for tier in HELDOUT_TIERS},
        }

    def source_digests(self) -> dict[str, str]:
        """Retain comparable evaluator fingerprints without exposing them publicly."""

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


def resolve_heldout_custody(
    *,
    repository_root: Path,
    candidate_store_root: Path,
    require_write: bool,
) -> HeldOutCustody:
    """Resolve the configured custody root and reject overlap, links, and bad ACLs."""

    configured_root = os.environ.get(HELDOUT_ROOT_ENVIRONMENT_VARIABLE)
    if configured_root is None or not configured_root.strip():
        raise HeldOutError("held-out evaluator custody root is unavailable")
    try:
        supplied_root = Path(configured_root)
        if not supplied_root.is_absolute():
            raise HeldOutError("held-out evaluator custody root is unavailable")
        root = _existing_directory_without_symlinks(supplied_root)
        repository = _comparison_path(repository_root)
        candidate_store = _comparison_path(candidate_store_root)
        if _paths_overlap(root, repository) or _paths_overlap(root, candidate_store):
            raise HeldOutError("held-out evaluator custody root is unavailable")
        required_access = os.R_OK | os.X_OK
        if require_write:
            required_access |= os.W_OK
        if not os.access(root, required_access):
            raise HeldOutError("held-out evaluator custody root is unavailable")
    except (OSError, ValueError):
        raise HeldOutError("held-out evaluator custody root is unavailable") from None
    return HeldOutCustody(root=root)


def initialise_heldout_manifest(
    data_root: Path,
    *,
    repository_root: Path,
    candidate_store_root: Path,
) -> HeldOutManifestReceipt:
    """Select only public metadata and atomically create one immutable manifest."""

    custody = resolve_heldout_custody(
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
        require_write=True,
    )
    if os.path.lexists(custody.manifest_path):
        raise HeldOutError("held-out manifest already exists")
    try:
        public_data_file, records, public_digest = _load_public_metadata(data_root)
        del public_data_file
        selected = _select_cases(records)
        draft = HeldOutManifest(
            schema_version=HELDOUT_MANIFEST_SCHEMA_VERSION,
            suite_id=LIVE_SQL_BENCH_BASE_LITE_HELDOUT_ADAPTER_ID,
            public_data_sha256=public_digest,
            cases=tuple(
                HeldOutManifestCase(
                    instance_id=case.instance_id,
                    category="Query",
                    difficulty_tier=case.difficulty_tier,
                    selected_database=case.selected_database,
                    order_matters=case.order_matters,
                )
                for case in selected
            ),
            manifest_sha256="0" * 64,
        )
        manifest = draft.model_copy(update={"manifest_sha256": _manifest_digest(draft)})
        _validate_manifest_source_materials(data_root, manifest)
        _write_manifest_atomically(custody.manifest_path, _manifest_bytes(manifest))
    except HeldOutError:
        raise
    except (OSError, ValidationError, ValueError):
        raise HeldOutError("held-out manifest could not be initialised") from None
    return HeldOutManifestReceipt(
        suite_id=manifest.suite_id,
        manifest_sha256=manifest.manifest_sha256,
    )


def load_heldout_manifest(custody: HeldOutCustody) -> HeldOutManifest:
    """Read, strictly validate, and verify the immutable custody manifest."""

    path = custody.manifest_path
    try:
        if path.is_symlink() or not path.is_file():
            raise HeldOutError("held-out manifest is unavailable")
        size = path.stat().st_size
        if size <= 0 or size > _MAX_MANIFEST_BYTES:
            raise HeldOutError("held-out manifest is invalid")
        payload = path.read_bytes()
        if b"\x00" in payload:
            raise HeldOutError("held-out manifest is invalid")
        manifest = HeldOutManifest.model_validate_json(payload)
        if manifest.manifest_sha256 != _manifest_digest(manifest):
            raise HeldOutError("held-out manifest integrity check failed")
    except HeldOutError:
        raise
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise HeldOutError("held-out manifest is invalid") from None
    return manifest


def load_heldout_suite(
    data_root: Path,
    *,
    repository_root: Path,
    candidate_store_root: Path,
) -> HeldOutLiveSQLBenchSuite:
    """Validate one custody manifest against local public, GT, and dump inputs."""

    custody = resolve_heldout_custody(
        repository_root=repository_root,
        candidate_store_root=candidate_store_root,
        require_write=False,
    )
    manifest = load_heldout_manifest(custody)
    try:
        public_data_file, _, public_digest = _load_public_metadata(data_root)
        if manifest.public_data_sha256 != public_digest:
            raise HeldOutError("held-out public data integrity check failed")
        materials, cases = _validate_manifest_source_materials(data_root, manifest)
    except HeldOutError:
        raise
    except (LiveSQLBenchDataError, OSError, ValueError):
        raise HeldOutError("held-out source data is invalid") from None
    return HeldOutLiveSQLBenchSuite(
        custody=custody,
        manifest=manifest,
        data_root=materials.data_root,
        public_data_file=public_data_file,
        gt_file=materials.gt_file,
        dump_root=materials.dump_root,
        cases=cases,
    )


def _validate_manifest_source_materials(
    data_root: Path, manifest: HeldOutManifest
) -> tuple[LiveSQLBenchMaterials, tuple[LiveSQLBenchCase, ...]]:
    """Fail before manifest persistence when selected evaluator sources are incomplete."""

    try:
        materials = load_livesqlbench_materials(data_root)
        _require_selected_paths_without_symlinks(materials.data_root, materials, manifest)
        cases = tuple(
            build_livesqlbench_case(materials, item.instance_id) for item in manifest.cases
        )
        for manifest_case, case in zip(manifest.cases, cases, strict=True):
            if (
                case.database_name != manifest_case.selected_database
                or case.difficulty_tier != manifest_case.difficulty_tier
                or case.order_matters != manifest_case.order_matters
            ):
                raise HeldOutError("held-out manifest metadata does not match public data")
    except HeldOutError:
        raise
    except (LiveSQLBenchDataError, OSError, ValueError):
        raise HeldOutError("held-out source data is invalid") from None
    return materials, cases


def _load_public_metadata(
    data_root: Path,
) -> tuple[Path, tuple[_PublicCaseMetadata, ...], str]:
    root = _existing_directory_without_symlinks(data_root)
    public_directory = root / "public"
    public_data_file = public_directory / "livesqlbench_data.jsonl"
    _require_path_without_symlinks(root, public_directory, directory=True)
    _require_path_without_symlinks(root, public_data_file, directory=False)
    try:
        payload = public_data_file.read_bytes()
    except OSError as error:
        raise HeldOutError("held-out public metadata is unavailable") from error
    if not payload or len(payload) > _MAX_PUBLIC_DATA_BYTES:
        raise HeldOutError("held-out public metadata is invalid")
    records: list[_PublicCaseMetadata] = []
    seen_ids: set[str] = set()
    try:
        for line in payload.decode("utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("record is not an object")
            if item.get("category") != "Query":
                continue
            instance_id = item.get("instance_id")
            if (
                not isinstance(instance_id, str)
                or _IDENTIFIER_PATTERN.fullmatch(instance_id) is None
            ):
                raise ValueError("invalid instance identifier")
            if instance_id in seen_ids:
                raise ValueError("duplicate instance identifier")
            seen_ids.add(instance_id)
            difficulty = item.get("difficulty_tier")
            database = item.get("selected_database")
            conditions = item.get("conditions")
            if (
                difficulty not in HELDOUT_TIERS
                or not isinstance(database, str)
                or _IDENTIFIER_PATTERN.fullmatch(database) is None
                or not isinstance(conditions, Mapping)
                or not isinstance(conditions.get("order"), bool)
            ):
                raise ValueError("invalid public Query metadata")
            records.append(
                _PublicCaseMetadata(
                    instance_id=instance_id,
                    difficulty_tier=difficulty,
                    selected_database=database,
                    order_matters=conditions["order"],
                )
            )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HeldOutError("held-out public metadata is invalid") from error
    if not records:
        raise HeldOutError("held-out public metadata is invalid")
    return public_data_file, tuple(records), hashlib.sha256(payload).hexdigest()


def _select_cases(records: tuple[_PublicCaseMetadata, ...]) -> tuple[_PublicCaseMetadata, ...]:
    candidates: dict[HeldOutTier, dict[str, list[_PublicCaseMetadata]]] = {
        tier: {} for tier in HELDOUT_TIERS
    }
    for record in records:
        if record.instance_id in APPROVED_BASE_LITE_QUERY_IDS:
            continue
        candidates[record.difficulty_tier].setdefault(record.selected_database, []).append(record)
    if any(len(candidates[tier]) < 10 for tier in HELDOUT_TIERS):
        raise HeldOutError("public metadata cannot satisfy held-out selection constraints")

    random_source = secrets.SystemRandom()
    best: tuple[_PublicCaseMetadata, ...] | None = None
    best_order_coverage = -1
    for _ in range(20_000):
        selected_databases = _select_database_sets(candidates, random_source)
        if selected_databases is None:
            continue
        selection = tuple(
            _select_tier_cases(candidates[tier], selected_databases[tier], random_source)
            for tier in HELDOUT_TIERS
        )
        flattened = tuple(case for tier_cases in selection for case in tier_cases)
        order_coverage = sum(
            len({case.order_matters for case in tier_cases}) for tier_cases in selection
        )
        if order_coverage > best_order_coverage:
            best = flattened
            best_order_coverage = order_coverage
        if order_coverage == len(HELDOUT_TIERS) * 2:
            return flattened
    if best is None:
        raise HeldOutError("public metadata cannot satisfy held-out selection constraints")
    return best


def _select_database_sets(
    candidates: Mapping[HeldOutTier, Mapping[str, list[_PublicCaseMetadata]]],
    random_source: secrets.SystemRandom,
) -> dict[HeldOutTier, list[str]] | None:
    """Construct a 30-slot selection with exactly 18 databases by design.

    Selecting each tier independently would be vanishingly unlikely to land on
    exactly eighteen databases when a public bundle has many schemas.  This
    construction gives one pair of tiers ``r`` shared databases, then completes
    the third tier with the required ``12-r`` once-used and ``r-2`` new databases.
    Every selected database consequently has one or two appearances.
    """

    tier_order = list(HELDOUT_TIERS)
    random_source.shuffle(tier_order)
    first_tier, second_tier, third_tier = tier_order
    first = random_source.sample(list(candidates[first_tier]), 10)
    first_set = set(first)
    possible_overlaps = [
        count
        for count in range(2, 9)
        if len(first_set.intersection(candidates[second_tier])) >= count
        and len(set(candidates[second_tier]).difference(first_set)) >= 10 - count
    ]
    if not possible_overlaps:
        return None
    overlap_count = random_source.choice(possible_overlaps)
    shared = random_source.sample(
        list(first_set.intersection(candidates[second_tier])), overlap_count
    )
    second_new = random_source.sample(
        list(set(candidates[second_tier]).difference(first_set)), 10 - overlap_count
    )
    second = [*shared, *second_new]
    first_and_second = first_set.union(second)
    once_used = first_and_second.difference(shared)
    third_existing = list(once_used.intersection(candidates[third_tier]))
    third_new = list(set(candidates[third_tier]).difference(first_and_second))
    required_existing = 12 - overlap_count
    required_new = overlap_count - 2
    if len(third_existing) < required_existing or len(third_new) < required_new:
        return None
    third = [
        *random_source.sample(third_existing, required_existing),
        *random_source.sample(third_new, required_new),
    ]
    selected = {first_tier: first, second_tier: second, third_tier: third}
    database_counts = Counter(database for values in selected.values() for database in values)
    if (
        len(database_counts) != HELDOUT_DATABASE_COUNT
        or max(database_counts.values()) > 2
        or any(len(values) != 10 for values in selected.values())
    ):
        return None
    return selected


def _select_tier_cases(
    candidates: Mapping[str, list[_PublicCaseMetadata]],
    databases: list[str],
    random_source: secrets.SystemRandom,
) -> tuple[_PublicCaseMetadata, ...]:
    selected = [random_source.choice(candidates[database]) for database in databases]
    orders = {item.order_matters for item in selected}
    if len(orders) == 1:
        desired_order = not selected[0].order_matters
        for index, database in enumerate(databases):
            alternatives = [
                item for item in candidates[database] if item.order_matters == desired_order
            ]
            if alternatives:
                selected[index] = random_source.choice(alternatives)
                break
    return tuple(selected)


def _manifest_digest(manifest: HeldOutManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manifest_bytes(manifest: HeldOutManifest) -> bytes:
    return (
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def _write_manifest_atomically(path: Path, payload: bytes) -> None:
    if os.path.lexists(path):
        raise HeldOutError("held-out manifest already exists")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    try:
        file_descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(file_descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise HeldOutError("held-out manifest already exists") from None
    except OSError:
        raise HeldOutError("held-out manifest could not be initialised") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _require_selected_paths_without_symlinks(
    root: Path, materials: LiveSQLBenchMaterials, manifest: HeldOutManifest
) -> None:
    _require_path_without_symlinks(root, root / "gt", directory=True)
    _require_path_without_symlinks(root, root / "gt" / materials.gt_file.name, directory=False)
    _require_path_without_symlinks(root, root / "dumps", directory=True)
    _require_path_without_symlinks(root, root / "dumps" / "base-lite", directory=True)
    _require_path_without_symlinks(
        root,
        root / "dumps" / "base-lite" / "livesqlbench-base-lite-dumps",
        directory=True,
    )
    for item in manifest.cases:
        database = item.selected_database
        public_directory = root / "public" / database
        dump_directory = materials.dump_root / f"{database}_template"
        _require_path_without_symlinks(root, public_directory, directory=True)
        _require_path_without_symlinks(
            root, public_directory / f"{database}_schema.txt", directory=False
        )
        _require_path_without_symlinks(
            root, public_directory / f"{database}_kb.jsonl", directory=False
        )
        _require_path_without_symlinks(
            root, public_directory / f"{database}_column_meaning_base.json", directory=False
        )
        _require_path_without_symlinks(root, dump_directory, directory=True)
        _require_path_without_symlinks(
            root, materials.dump_root / f"{database}_table_orders.txt", directory=False
        )
        dump_files = tuple(dump_directory.glob("*.sql"))
        if not dump_files:
            raise HeldOutError("held-out source data is invalid")
        for dump_file in dump_files:
            _require_path_without_symlinks(root, dump_file, directory=False)


def _existing_directory_without_symlinks(path: Path) -> Path:
    absolute = path.absolute()
    if not absolute.is_dir():
        raise HeldOutError("held-out source data is invalid")
    _reject_symlink_components(absolute)
    try:
        return absolute.resolve(strict=True)
    except OSError as error:
        raise HeldOutError("held-out source data is invalid") from error


def _require_path_without_symlinks(root: Path, path: Path, *, directory: bool) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise HeldOutError("held-out source data is invalid") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise HeldOutError("held-out source data is invalid")
    valid = path.is_dir() if directory else path.is_file()
    if not valid:
        raise HeldOutError("held-out source data is invalid")


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise HeldOutError("held-out source data is invalid")


def _comparison_path(path: Path) -> Path:
    return path.absolute().resolve(strict=False)


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
