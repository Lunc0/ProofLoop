"""Explicit, no-rerun CLI coverage for existing Evolution advice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from skill_evolve.benchmark import LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID
from skill_evolve.cli import app
from skill_evolve.optimizer.contracts import AdviceSource, OptimizationResult, OptimizerReceipt
from skill_evolve.registry import RunDirectory, RunStore
from skill_evolve.subject import RunSubject


def test_advice_command_uses_existing_evolution_run_without_rerunning(
    tmp_path: Path, monkeypatch
) -> None:
    run = _write_completed_evolution_run(tmp_path / "runs")
    calls: list[tuple[str, str]] = []

    def fake_advise(
        received_run: RunDirectory,
        subject: RunSubject,
        optimizer: object,
        *,
        run_status: str,
    ) -> OptimizationResult:
        del optimizer
        calls.append((received_run.run_id, run_status))
        assert subject.content == b"PRIVATE SKILL CONTENT\n"
        received_run.write_text("optimizer/advice.md", "# Advice\n")
        return OptimizationResult(
            receipt=OptimizerReceipt(
                status="proposed",
                provider="deepseek",
                model="deepseek-v4-flash",
                prompt_version="skill-optimization-advice-prompt-v1",
                source=AdviceSource(
                    run_id=received_run.run_id,
                    agent_input_sha256="a" * 64,
                    evaluated_skill_sha256=subject.content_sha256,
                ),
            ),
            advice_path="optimizer/advice.md",
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skill_evolve.cli.advise_after_evolution", fake_advise)

    result = CliRunner().invoke(app, ["optimizer", "advise", run.run_id])

    assert result.exit_code == 0
    assert calls == [(run.run_id, "task_failure")]
    assert "PRIVATE SKILL CONTENT" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload == {
        "run_id": run.run_id,
        "optimizer": {
            "status": "proposed",
            "advice_path": "optimizer/advice.md",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "prompt_version": "skill-optimization-advice-prompt-v1",
        },
    }
    persisted = json.loads((run.path / "result.json").read_text(encoding="utf-8"))
    assert persisted["optimizer"] == payload["optimizer"]
    assert len(tuple((tmp_path / "runs").iterdir())) == 1


def test_advice_command_rejects_non_evolution_or_incomplete_run(
    tmp_path: Path, monkeypatch
) -> None:
    store = RunStore(tmp_path / "runs")
    non_evolution = store.create()
    non_evolution.write_json("run.json", {"adapter_id": "other"})
    incomplete = store.create()
    incomplete.write_json(
        "run.json", {"adapter_id": LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID}
    )
    incomplete.write_json(
        "result.json",
        {
            "adapter_id": LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID,
            "status": "task_failure",
        },
    )
    called = False

    def fake_advise(*args: object, **kwargs: object) -> OptimizationResult:
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("must not be called")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skill_evolve.cli.advise_after_evolution", fake_advise)
    runner = CliRunner()

    rejected_kind = runner.invoke(app, ["optimizer", "advise", non_evolution.run_id])
    rejected_input = runner.invoke(app, ["optimizer", "advise", incomplete.run_id])

    assert rejected_kind.exit_code == rejected_input.exit_code == 64
    assert "requires a completed Evolution run" in rejected_kind.stderr
    assert "inputs/subject.json" in rejected_input.stderr
    assert not called


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
