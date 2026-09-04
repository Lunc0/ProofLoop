"""Local candidate store immutability, provenance, and lineage coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_evolve.candidate import (
    CandidateOrigin,
    CandidateService,
    CandidateStoreError,
    ExternalSource,
    LocalCandidateStore,
    OptimizerProvenance,
)
from skill_evolve.candidate.store import skill_sha256_for_source


def _service(tmp_path: Path) -> CandidateService:
    return CandidateService(LocalCandidateStore(tmp_path / ".local" / "skill-evolve"))


def _write_skill(path: Path, content: str = "# Safe SQL Skill\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_store_creates_immutable_baseline_child_and_local_selection(tmp_path: Path) -> None:
    service = _service(tmp_path)
    baseline_source = _write_skill(tmp_path / "sources" / "baseline.md")
    baseline = service.initialise("sql-analyst", baseline_source)
    run_id = "20260826T000000Z-12345678"
    child_source = _write_skill(tmp_path / "sources" / "child.md", "# Improved SQL Skill\n")
    child = service.import_candidate(
        "sql-analyst",
        "001-improve-joins",
        child_source,
        parent_candidate_id=baseline.candidate_id,
        source_run_id=run_id,
    )

    selected = service.select(child.candidate_id)

    assert baseline.candidate_id == "sql-analyst/000-baseline"
    assert child.metadata.parent_candidate_id == baseline.candidate_id
    assert selected.catalog_entry.status.value == "selected"
    assert selected.metadata.status.value == "draft"
    assert [item.candidate_id for item in service.list_candidates("sql-analyst")] == [
        baseline.candidate_id,
        child.candidate_id,
    ]
    root = tmp_path / ".local" / "skill-evolve"
    assert (root / "candidates" / "sql-analyst" / "000-baseline" / "SKILL.md").is_file()
    skill_map = json.loads(
        (root / "candidates" / "sql-analyst" / "000-baseline" / "skill-map.json").read_text(
            encoding="utf-8"
        )
    )
    assert skill_map["schema_version"] == "skill-map-v3"
    assert "skill_sha256" not in skill_map
    assert not (root / "candidates" / "sql-analyst" / "000-baseline" / "NOTICE.md").exists()


def test_store_requires_external_provenance_notice_and_detects_tampering(tmp_path: Path) -> None:
    service = _service(tmp_path)
    skill_source = _write_skill(tmp_path / "sources" / "dbhub.md", "# DBHub source\n")
    notice_source = _write_skill(tmp_path / "sources" / "NOTICE.md", "MIT License\n")
    source = ExternalSource(
        canonical_url="https://github.com/bytebase/dbhub/blob/0123456789abcdef0123456789abcdef01234567/skills/dbhub/SKILL.md",
        source_repository="bytebase/dbhub",
        commit="0123456789abcdef0123456789abcdef01234567",
        file_sha256=skill_sha256_for_source(skill_source),
        license_id="MIT",
    )
    baseline = service.initialise(
        "dbhub",
        skill_source,
        candidate_name="000-upstream-baseline",
        external_source=source,
        notice_source=notice_source,
    )
    skill_path = (
        tmp_path
        / ".local"
        / "skill-evolve"
        / "candidates"
        / "dbhub"
        / "000-upstream-baseline"
        / "SKILL.md"
    )

    assert baseline.metadata.external_source == source
    assert skill_path.with_name("NOTICE.md").read_text(encoding="utf-8") == "MIT License\n"
    skill_path.write_text("# Tampered\n", encoding="utf-8")

    with pytest.raises(CandidateStoreError, match="does not match"):
        service.verify(baseline.candidate_id)


def test_store_rejects_candidate_attachments(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = _write_skill(tmp_path / "sources" / "skill.md")
    candidate = service.initialise("sql-analyst", source)
    attachment = (
        tmp_path
        / ".local"
        / "skill-evolve"
        / "candidates"
        / "sql-analyst"
        / "000-baseline"
        / "plugin.py"
    )
    attachment.write_text("raise RuntimeError('never execute')\n", encoding="utf-8")

    with pytest.raises(CandidateStoreError, match="invalid file set"):
        service.verify(candidate.candidate_id)


def test_store_rejects_missing_or_mismatched_skill_map_and_backfills_explicitly(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    candidate = service.initialise("sql-analyst", _write_skill(tmp_path / "sources" / "skill.md"))
    map_path = (
        tmp_path
        / ".local"
        / "skill-evolve"
        / "candidates"
        / "sql-analyst"
        / "000-baseline"
        / "skill-map.json"
    )

    map_path.unlink()
    with pytest.raises(CandidateStoreError, match="skill-map.json"):
        service.verify(candidate.candidate_id)

    backfilled = service.backfill_skill_map(candidate.candidate_id)
    assert backfilled.candidate_id == candidate.candidate_id
    skill_map = json.loads(map_path.read_text(encoding="utf-8"))
    assert skill_map["schema_version"] == "skill-map-v3"
    with pytest.raises(CandidateStoreError, match="already exists"):
        service.backfill_skill_map(candidate.candidate_id)

    payload = json.loads(map_path.read_text(encoding="utf-8"))
    payload["parser_policy_version"] = "tampered"
    map_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CandidateStoreError, match="does not match"):
        service.verify(candidate.candidate_id)


def test_store_rebuilds_an_existing_legacy_skill_map_explicitly(tmp_path: Path) -> None:
    service = _service(tmp_path)
    candidate = service.initialise("sql-analyst", _write_skill(tmp_path / "sources" / "skill.md"))
    map_path = (
        tmp_path
        / ".local"
        / "skill-evolve"
        / "candidates"
        / "sql-analyst"
        / "000-baseline"
        / "skill-map.json"
    )
    map_path.write_text('{"schema_version":"skill-map-v1"}', encoding="utf-8")

    with pytest.raises(CandidateStoreError, match="does not match"):
        service.verify(candidate.candidate_id)

    rebuilt = service.rebuild_skill_map(candidate.candidate_id)

    assert rebuilt.candidate_id == candidate.candidate_id
    assert json.loads(map_path.read_text(encoding="utf-8"))["schema_version"] == "skill-map-v3"


@pytest.mark.parametrize(
    "render_policy_version",
    ("skill-edit-render-policy-v1", "skill-edit-render-policy-v2"),
)
def test_store_persists_optimizer_provenance_for_a_draft_child(
    tmp_path: Path, render_policy_version: str
) -> None:
    service = _service(tmp_path)
    baseline = service.initialise("sql-analyst", _write_skill(tmp_path / "sources" / "base.md"))
    provenance = OptimizerProvenance(
        edit_plan_sha256="a" * 64,
        patch_sha256="b" * 64,
        parent_skill_sha256=baseline.metadata.skill_sha256,
        render_policy_version=render_policy_version,
    )
    child = service.import_candidate(
        "sql-analyst",
        "001-materialized",
        _write_skill(tmp_path / "sources" / "child.md", "# Child\n"),
        parent_candidate_id=baseline.candidate_id,
        source_run_id="20260902T000000Z-12345678",
        origin=CandidateOrigin.OPTIMIZER,
        optimizer_provenance=provenance,
    )

    assert child.metadata.origin is CandidateOrigin.OPTIMIZER
    assert child.metadata.optimizer_provenance == provenance
    assert service.verify(child.candidate_id).metadata.status.value == "draft"


def test_store_rejects_catalog_lineage_cycle_and_does_not_use_paths_from_metadata(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    source = _write_skill(tmp_path / "sources" / "skill.md")
    baseline = service.initialise("sql-analyst", source)
    root = tmp_path / ".local" / "skill-evolve"
    metadata_path = root / "candidates" / "sql-analyst" / "000-baseline" / "metadata.json"
    catalog_path = root / "catalog.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["parent_candidate_id"] = baseline.candidate_id
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["entries"][0]["parent_candidate_id"] = baseline.candidate_id
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(CandidateStoreError, match="cycle"):
        service.verify(baseline.candidate_id)


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform has no symlink support")
def test_store_rejects_symlink_skill_input(tmp_path: Path) -> None:
    target = _write_skill(tmp_path / "sources" / "target.md")
    symlink = tmp_path / "sources" / "linked.md"
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(CandidateStoreError, match="regular non-symlink"):
        _service(tmp_path).initialise("sql-analyst", symlink)
