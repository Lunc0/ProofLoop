"""Run directories preserve enough evidence to resolve an identical rerun."""

from __future__ import annotations

from skill_evolve.config import load_manifest
from skill_evolve.fingerprint import create_snapshot
from skill_evolve.registry import RunStore
from tests.conftest import write_experiment


def test_materialised_manifest_is_self_contained_and_fingerprint_identical(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path / "source")
    source = load_manifest(manifest_path)
    source_snapshot = create_snapshot(source)
    run = RunStore(tmp_path / "runs").create()

    resolved_manifest_path = run.materialise_manifest(source)
    copied = load_manifest(resolved_manifest_path)
    copied_snapshot = create_snapshot(copied)

    assert copied_snapshot.fingerprint == source_snapshot.fingerprint
    assert resolved_manifest_path.parent == run.path
    assert (run.path / "inputs").is_dir()
