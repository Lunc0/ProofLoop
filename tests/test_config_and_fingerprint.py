"""Manifest validation and reproducibility tests."""

from __future__ import annotations

import pytest

from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.fingerprint import create_snapshot
from tests.conftest import write_experiment


def test_snapshot_is_stable_for_identical_inputs(tmp_path) -> None:
    resolved = load_manifest(write_experiment(tmp_path))

    first = create_snapshot(resolved)
    second = create_snapshot(resolved)

    assert first.fingerprint == second.fingerprint
    assert {artifact.name for artifact in first.artifacts} == {
        "database.schema",
        "database.seed",
        "dsh.fake_output",
        "tools.sql-readonly.manifest",
    }


def test_snapshot_changes_when_fixture_changes(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path)
    before = create_snapshot(load_manifest(manifest_path))
    (tmp_path / "fixtures" / "seed.sql").write_text(
        "INSERT INTO customers VALUES (2);\n", encoding="utf-8"
    )

    after = create_snapshot(load_manifest(manifest_path))

    assert before.fingerprint != after.fingerprint


def test_manifest_rejects_secret_key(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path, "api_key: not-allowed\n")

    with pytest.raises(ManifestError, match="must not contain secrets"):
        load_manifest(manifest_path)


def test_manifest_rejects_path_outside_workspace(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path)
    content = manifest_path.read_text(encoding="utf-8").replace(
        "fixtures/schema.sql", "../outside.sql"
    )
    manifest_path.write_text(content, encoding="utf-8")

    with pytest.raises(ManifestError, match="must remain inside"):
        load_manifest(manifest_path)


def test_selecting_empty_slice_is_an_error(tmp_path) -> None:
    resolved = load_manifest(write_experiment(tmp_path))

    with pytest.raises(ManifestError, match="contains no cases"):
        resolved.select_cases("held_out")
