"""Candidate CLI returns only public metadata and safe usage failures."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from skill_evolve.candidate import CandidateService, LocalCandidateStore
from skill_evolve.cli import app
from skill_evolve.registry import RunStore


def test_candidate_cli_lifecycle_never_prints_skill_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "incoming" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("DO NOT PRINT THIS SKILL TEXT\n", encoding="utf-8")
    child = tmp_path / "incoming" / "child.md"
    child.write_text("CHILD SKILL TEXT\n", encoding="utf-8")
    runner = CliRunner()

    initialised = runner.invoke(app, ["candidate", "init", "sql-analyst", str(skill)])
    assert initialised.exit_code == 0
    assert "DO NOT PRINT THIS SKILL TEXT" not in initialised.stdout
    assert json.loads(initialised.stdout)["binding"]["candidate_id"] == "sql-analyst/000-baseline"

    run = RunStore(tmp_path / "runs").create()
    imported = runner.invoke(
        app,
        [
            "candidate",
            "import",
            "sql-analyst",
            "001-adaptation",
            str(child),
            "--parent",
            "sql-analyst/000-baseline",
            "--source-run",
            run.run_id,
        ],
    )
    selected = runner.invoke(app, ["candidate", "select", "sql-analyst/001-adaptation"])
    verified = runner.invoke(app, ["candidate", "verify", "sql-analyst/001-adaptation"])

    assert imported.exit_code == selected.exit_code == verified.exit_code == 0
    assert json.loads(selected.stdout)["catalog"]["status"] == "selected"
    assert json.loads(verified.stdout)["verified"] is True


def test_candidate_cli_rejects_unknown_and_incomplete_external_input_with_64(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Skill\n", encoding="utf-8")
    runner = CliRunner()

    unknown = runner.invoke(app, ["candidate", "verify", "sql-analyst/unknown"])
    incomplete = runner.invoke(
        app,
        ["candidate", "init", "sql-analyst", str(skill), "--source-url", "https://example.com/x"],
    )

    assert unknown.exit_code == 64
    assert incomplete.exit_code == 64
    assert not (tmp_path / ".local" / "skill-evolve" / "catalog.json").exists()


def test_benchmark_run_resolves_verified_candidate_before_adapter_startup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Candidate\n", encoding="utf-8")
    candidate_service = CandidateService(
        LocalCandidateStore(tmp_path / ".local" / "skill-evolve")
    )
    candidate = candidate_service.initialise(
        "sql-analyst", skill
    )
    captured: dict[str, object] = {}

    def load_adapter(adapter_id: str) -> object:
        captured["adapter_id"] = adapter_id
        return object()

    def load_suite(adapter: object, data_root: Path) -> object:
        del adapter, data_root
        return object()

    def run_adapter(
        adapter: object, suite: object, manifest: Path, resolved_candidate: object
    ) -> None:
        del adapter, suite, manifest
        captured["candidate"] = resolved_candidate

    monkeypatch.setattr("skill_evolve.cli._load_adapter", load_adapter)
    monkeypatch.setattr("skill_evolve.cli._load_benchmark_suite", load_suite)
    monkeypatch.setattr("skill_evolve.cli._run_adapter_dsh", run_adapter)

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "run",
            "stub-adapter",
            str(tmp_path / "data"),
            str(tmp_path / "dsh.yaml"),
            "--candidate",
            candidate.candidate_id,
        ],
    )

    assert result.exit_code == 0
    assert captured["adapter_id"] == "stub-adapter"
    assert captured["candidate"] == candidate
