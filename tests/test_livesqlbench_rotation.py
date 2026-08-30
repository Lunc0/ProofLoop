"""Synthetic-only custody and selection coverage for rolling LiveSQLBench."""

from __future__ import annotations

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
    create_rotation_manifest,
    initialise_rotation_reserve,
    load_rotation_manifest,
    load_rotation_reserve,
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
    assert smoke_tier_counts == [
        Counter({"Simple": 2, "Moderate": 2, "Challenging": 1}),
        Counter({"Simple": 2, "Moderate": 1, "Challenging": 2}),
        Counter({"Simple": 1, "Moderate": 2, "Challenging": 2}),
        Counter({"Simple": 2, "Moderate": 2, "Challenging": 1}),
    ]

    evolution_receipt = create_rotation_manifest(
        data_root,
        "evolution",
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    evolution = load_rotation_manifest(custody, evolution_receipt.manifest_id)
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
