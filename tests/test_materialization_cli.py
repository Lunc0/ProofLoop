"""Explicit no-rerun CLI coverage for stage 14 materialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from skill_evolve.benchmark import LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID
from skill_evolve.cli import app
from skill_evolve.optimizer.materialization_contracts import (
    MaterializationReceipt,
    MaterializationResult,
    MaterializationSource,
)
from skill_evolve.registry import RunDirectory, RunStore
from skill_evolve.subject import RunSubject


def test_materialize_command_uses_existing_run_and_emits_only_summary(
    tmp_path: Path, monkeypatch
) -> None:
    run = _write_completed_evolution_run(tmp_path / "runs")
    calls: list[tuple[str, str, str]] = []

    def fake_materialize(
        received_run: RunDirectory,
        subject: RunSubject,
        candidate_service: object,
        candidate_name: str,
    ) -> MaterializationResult:
        del candidate_service
        calls.append((received_run.run_id, subject.content_sha256, candidate_name))
        source = MaterializationSource(
            run_id=received_run.run_id,
            parent_candidate_id=subject.subject_id,
            parent_skill_sha256=subject.content_sha256,
            edit_plan_sha256="a" * 64,
            target_contract_sha256="b" * 64,
        )
        return MaterializationResult(
            receipt=MaterializationReceipt(
                status="created",
                source=source,
                child_candidate_id="sql-analyst/001-child",
                child_skill_sha256="c" * 64,
                patch_sha256="d" * 64,
                edit_count=1,
            ),
            patch_path="optimizer/materialization/rendered.patch",
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skill_evolve.cli.materialize_after_evolution", fake_materialize)

    result = CliRunner().invoke(
        app,
        ["optimizer", "materialize", run.run_id, "--candidate-name", "001-child"],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            run.run_id,
            hashlib.sha256(b"PRIVATE SKILL CONTENT\n").hexdigest(),
            "001-child",
        )
    ]
    assert "PRIVATE SKILL CONTENT" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["optimizer_materialization"]["child_candidate_id"] == "sql-analyst/001-child"
    persisted = json.loads((run.path / "result.json").read_text(encoding="utf-8"))
    assert persisted["optimizer_materialization"] == payload["optimizer_materialization"]


def test_materialize_command_requires_a_candidate_name(tmp_path: Path, monkeypatch) -> None:
    run = _write_completed_evolution_run(tmp_path / "runs")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["optimizer", "materialize", run.run_id])

    assert result.exit_code != 0
    assert "--candidate-name" in result.output


def _write_completed_evolution_run(runs_root: Path) -> RunDirectory:
    run = RunStore(runs_root).create()
    content = b"PRIVATE SKILL CONTENT\n"
    subject = RunSubject(
        subject_id="candidate-1",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    run.write_json(
        "run.json",
        {"adapter_id": LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID},
    )
    run.write_json(
        "result.json",
        {
            "adapter_id": LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
            "status": "task_failure",
        },
    )
    run.materialise_subject(subject)
    return run
