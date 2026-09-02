"""Synthetic-only custody and selection coverage for rolling LiveSQLBench."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from skill_evolve.benchmark.livesqlbench_heldout import (
    HELDOUT_ROOT_ENVIRONMENT_VARIABLE,
    initialise_heldout_manifest,
    load_heldout_manifest,
    resolve_heldout_custody,
)
from skill_evolve.benchmark.livesqlbench_rotation import (
    ROTATION_ROOT_ENVIRONMENT_VARIABLE,
    RotationCustody,
    RotationError,
    RotationManifest,
    RotationManifestCase,
    create_rotation_manifest,
    initialise_rotation_reserve,
    load_rotation_manifest,
    load_rotation_reserve,
    load_rotation_suite,
    resolve_rotation_custody,
)
from tests.test_livesqlbench_heldout import _write_final_suite_fixture


def test_rotation_reserve_and_manifest_selection_are_custody_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, custody = _initialise_rotation_custody(tmp_path, monkeypatch)
    reserve_receipt = initialise_rotation_reserve(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    reserve = load_rotation_reserve(custody)

    assert reserve_receipt.tier_counts == {"Simple": 5, "Moderate": 5, "Challenging": 5}
    assert len(reserve.cases) == 15
    assert "instance_id" not in str(reserve_receipt.public_dict())

    smoke_receipts = [
        create_rotation_manifest(
            data_root,
            "smoke",
            repository_root=Path.cwd(),
            candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
        )
        for _ in range(4)
    ]
    smoke_manifests = [
        load_rotation_manifest(custody, receipt.manifest_id) for receipt in smoke_receipts
    ]
    assert [manifest.selection_index for manifest in smoke_manifests] == [0, 1, 2, 3]
    smoke_tier_counts = [
        Counter(case.difficulty_tier for case in manifest.cases) for manifest in smoke_manifests
    ]
    assert smoke_tier_counts == [Counter({"Simple": 3, "Moderate": 2, "Challenging": 0})] * 4
    assert all(manifest.schema_version == "v2" for manifest in smoke_manifests)

    evolution_receipt = create_rotation_manifest(
        data_root,
        "evolution",
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    evolution = load_rotation_manifest(custody, evolution_receipt.manifest_id)
    assert evolution.schema_version == "v1"
    assert Counter(case.difficulty_tier for case in evolution.cases) == Counter(
        {"Simple": 5, "Moderate": 5, "Challenging": 5}
    )

    final_manifest = load_heldout_manifest(
        resolve_heldout_custody(
            repository_root=Path.cwd(),
            candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
            require_write=False,
        )
    )
    excluded_ids = {case.instance_id for case in final_manifest.cases}
    excluded_ids.update(case.instance_id for case in reserve.cases)
    assert all(
        excluded_ids.isdisjoint(case.instance_id for case in manifest.cases)
        for manifest in [*smoke_manifests, evolution]
    )
    assert not (tmp_path / "runs").exists()


def test_rotation_manifest_tampering_fails_closed_before_suite_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, custody = _initialise_rotation_custody(tmp_path, monkeypatch)
    initialise_rotation_reserve(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    receipt = create_rotation_manifest(
        data_root,
        "smoke",
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    manifest_path = custody.manifests_root / f"{receipt.manifest_id}.json"
    manifest_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RotationError, match="rotation manifest"):
        load_rotation_manifest(custody, receipt.manifest_id)


def test_legacy_smoke_manifest_is_auditable_but_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, custody = _initialise_rotation_custody(tmp_path, monkeypatch)
    common = {
        "repository_root": Path.cwd(),
        "candidate_store_root": Path.cwd() / ".local" / "skill-evolve",
    }
    initialise_rotation_reserve(data_root, **common)
    current_receipt = create_rotation_manifest(data_root, "smoke", **common)
    current = load_rotation_manifest(custody, current_receipt.manifest_id)
    reserve = load_rotation_reserve(custody)
    final_manifest = load_heldout_manifest(
        resolve_heldout_custody(require_write=False, **common)
    )
    legacy_cases = list(case for case in current.cases if case.difficulty_tier != "Simple")
    legacy_cases.extend(
        case for case in current.cases if case.difficulty_tier == "Simple"
    )
    legacy_cases = legacy_cases[:4]
    excluded = {case.instance_id for case in [*reserve.cases, *final_manifest.cases]}
    selected_ids = {case.instance_id for case in legacy_cases}
    public_records = [
        json.loads(line)
        for line in (data_root / "public" / "livesqlbench_data.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    challenging = next(
        record
        for record in public_records
        if record["difficulty_tier"] == "Challenging"
        and record["instance_id"] not in excluded | selected_ids
    )
    legacy_cases.append(
        RotationManifestCase(
            instance_id=challenging["instance_id"],
            category="Query",
            difficulty_tier="Challenging",
            selected_database=challenging["selected_database"],
            order_matters=challenging["conditions"]["order"],
        )
    )
    draft = RotationManifest(
        schema_version="v1",
        adapter_id=current.adapter_id,
        manifest_id="smoke-0123456789abcdef01234567",
        selection_index=0,
        public_data_sha256=current.public_data_sha256,
        final_manifest_sha256=current.final_manifest_sha256,
        reserve_sha256=current.reserve_sha256,
        cases=tuple(legacy_cases),
        manifest_sha256="0" * 64,
    )
    digest_payload = draft.model_dump(mode="json", exclude={"manifest_sha256"})
    digest = hashlib.sha256(
        json.dumps(
            digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    legacy = draft.model_copy(update={"manifest_sha256": digest})
    legacy_path = custody.manifests_root / f"{legacy.manifest_id}.json"
    legacy_path.write_text(legacy.model_dump_json() + "\n", encoding="utf-8")

    assert load_rotation_manifest(custody, legacy.manifest_id).schema_version == "v1"
    with pytest.raises(RotationError, match="legacy Smoke manifests are retired"):
        load_rotation_suite(data_root, legacy.manifest_id, **common)


def test_rotation_rejects_an_invalid_manifest_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root, custody = _initialise_rotation_custody(tmp_path, monkeypatch)
    initialise_rotation_reserve(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    custody.manifests_root.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(RotationError, match="manifests directory"):
        create_rotation_manifest(
            data_root,
            "smoke",
            repository_root=Path.cwd(),
            candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
        )


def _initialise_rotation_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, RotationCustody]:
    data_root = _write_final_suite_fixture(tmp_path / "data", database_count=24)
    heldout_root = tmp_path / "heldout"
    rotation_root = tmp_path / "rotation"
    heldout_root.mkdir()
    rotation_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(heldout_root))
    monkeypatch.setenv(ROTATION_ROOT_ENVIRONMENT_VARIABLE, str(rotation_root))
    initialise_heldout_manifest(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    custody = resolve_rotation_custody(
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
        require_write=True,
    )
    return data_root, custody
