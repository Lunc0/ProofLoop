"""Explicit, no-rerun CLI coverage for stage 13 semantic edit plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from skill_evolve.benchmark import LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID
from skill_evolve.cli import app
from skill_evolve.config.models import OptimizerConfig
from skill_evolve.optimizer.edit_plan_contracts import (
    EditPlanReceipt,
    EditPlanResult,
    EditPlanSource,
)
from skill_evolve.registry import RunDirectory, RunStore
from skill_evolve.subject import RunSubject


def test_plan_command_uses_existing_evolution_run_without_rerunning(
    tmp_path: Path, monkeypatch
) -> None:
    run = _write_completed_evolution_run(tmp_path / "runs")
    calls: list[tuple[str, str, str]] = []
    received_optimizers: list[OptimizerConfig] = []

    def fake_plan(
        received_run: RunDirectory,
        subject: RunSubject,
        optimizer: OptimizerConfig,
        *,
        run_status: str,
        adapter_id: str,
    ) -> EditPlanResult:
        calls.append((received_run.run_id, run_status, adapter_id))
        received_optimizers.append(optimizer)
        assert subject.content == b"PRIVATE SKILL CONTENT\n"
        source = EditPlanSource(
            run_id=received_run.run_id,
            parent_candidate_id=subject.subject_id,
            parent_skill_sha256=subject.content_sha256,
            agent_input_sha256="a" * 64,
            target_contract_sha256="b" * 64,
            skill_map_sha256="c" * 64,
        )
        return EditPlanResult(
            receipt=EditPlanReceipt(
                status="proposed",
                provider="deepseek",
                model="deepseek-v4-flash",
                source=source,
                plan_sha256="d" * 64,
                skill_map_sha256="c" * 64,
                cluster_count=2,
                edit_count=1,
            ),
            plan_path="optimizer/edit-plan/edit-plan.json",
            skill_map_path="optimizer/edit-plan/skill-map.json",
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_CHAT_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_BASE_URL=https://www.micuapi.ai/v1\n"
        "DEEPSEEK_CHAT_MODEL=deepseek-v4-flash-0731\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("skill_evolve.cli.plan_after_evolution", fake_plan)

    result = CliRunner().invoke(app, ["optimizer", "plan", run.run_id])

    assert result.exit_code == 0
    assert calls == [
        (run.run_id, "task_failure", LIVE_SQL_BENCH_BASE_LITE_EVOLUTION_ADAPTER_ID)
    ]
    assert received_optimizers == [
        OptimizerConfig(
            enabled=True,
            base_url="https://www.micuapi.ai/v1",
            model="deepseek-v4-flash-0731",
        )
    ]
    assert "PRIVATE SKILL CONTENT" not in result.stdout
    payload = json.loads(result.stdout)
    summary = payload["optimizer_edit_plan"]
    assert summary == {
        "status": "proposed",
        "plan_path": "optimizer/edit-plan/edit-plan.json",
        "skill_map_path": "optimizer/edit-plan/skill-map.json",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "prompt_version": "skill-edit-plan-prompt-v6",
        "plan_sha256": "d" * 64,
        "skill_map_sha256": "c" * 64,
        "cluster_count": 2,
        "edit_count": 1,
    }
    persisted = json.loads((run.path / "result.json").read_text(encoding="utf-8"))
    assert persisted["optimizer_edit_plan"] == summary
    assert len(tuple((tmp_path / "runs").iterdir())) == 1


def test_plan_command_rejects_non_evolution_or_incomplete_run(
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

    def fake_plan(*args: object, **kwargs: object) -> EditPlanResult:
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("must not be called")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skill_evolve.cli.plan_after_evolution", fake_plan)
    runner = CliRunner()

    rejected_kind = runner.invoke(app, ["optimizer", "plan", non_evolution.run_id])
    rejected_input = runner.invoke(app, ["optimizer", "plan", incomplete.run_id])

    assert rejected_kind.exit_code == rejected_input.exit_code == 64
    assert "optimizer plan requires a completed Evolution run" in rejected_kind.stderr
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
