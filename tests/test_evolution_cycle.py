"""Synthetic coverage for the explicit, bounded Evolution-cycle orchestration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from skill_evolve.benchmark.livesqlbench_rotation_run import RotationRunError
from skill_evolve.cli import app
from skill_evolve.config.models import DshConfig, ModelConfig, OptimizerConfig
from skill_evolve.evolution import EvolutionCycleError, EvolutionCycleReceipt, EvolutionCycleRequest
from skill_evolve.evolution import service as cycle_service
from skill_evolve.registry import RunStore


def test_cycle_runs_baseline_then_smoke_gated_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _candidate("sql-agent/000-baseline")
    candidates = _FakeCandidates({baseline.candidate_id: baseline})
    manifest = _manifest(enabled=False)
    calls: dict[str, list[object]] = {"runner": [], "provider": [], "materialize": []}
    run_store = RunStore(tmp_path / "runs")

    monkeypatch.setattr(cycle_service, "load_manifest", lambda path: manifest)
    monkeypatch.setattr(cycle_service, "_ensure_rotation_reserve", lambda *args: None)
    monkeypatch.setattr(
        cycle_service,
        "create_rotation_manifest",
        _fake_manifest_creator(),
    )
    monkeypatch.setattr(cycle_service, "load_rotation_suite", _fake_suite_loader)
    monkeypatch.setattr(
        cycle_service, "meets_smoke_gate_policy", lambda status, evaluations: True
    )

    def fake_runner(
        suite: object,
        dsh_config: DshConfig,
        profile_patch: Path | None,
        model: ModelConfig,
        runs_root: Path,
        subject: object,
        optimizer: OptimizerConfig,
        *,
        allow_ungated_evolution: bool = False,
    ) -> object:
        del dsh_config, profile_patch, model, runs_root, subject
        calls["runner"].append((suite.kind, optimizer.enabled, allow_ungated_evolution))
        run = run_store.create()
        run.write_json("result.json", {"status": "task_failure"})
        return SimpleNamespace(run=run, status="task_failure", evaluations=())

    def fake_advice(
        run: object, subject: object, optimizer: OptimizerConfig, *, run_status: str
    ) -> object:
        del run, subject, run_status
        calls["provider"].append(("advice", optimizer.enabled))
        return _advice_result("proposed")

    def fake_plan(
        run: object,
        subject: object,
        optimizer: OptimizerConfig,
        *,
        run_status: str,
        adapter_id: str,
    ) -> object:
        del run, subject, run_status, adapter_id
        calls["provider"].append(("plan", optimizer.enabled))
        return _plan_result("proposed")

    def fake_materialize(
        run: object, subject: object, candidate_service: object, candidate_name: str
    ) -> object:
        del run, candidate_service
        child_id = f"sql-agent/{candidate_name}"
        child = _candidate(child_id, parent_candidate_id=subject.subject_id)
        candidates.items[child_id] = child
        calls["materialize"].append(child_id)
        return _materialization_result(child_id)

    monkeypatch.setattr(cycle_service, "run_rotation_dsh_suite", fake_runner)
    monkeypatch.setattr(cycle_service, "advise_after_evolution", fake_advice)
    monkeypatch.setattr(cycle_service, "plan_after_evolution", fake_plan)
    monkeypatch.setattr(cycle_service, "materialize_after_evolution", fake_materialize)

    receipt = cycle_service.run_evolution_cycle(
        EvolutionCycleRequest("sql-agent/000-baseline", 2, "evolution"),
        tmp_path / "data",
        tmp_path / "dsh.yaml",
        candidate_service=candidates,
        repository_root=tmp_path,
        candidate_store_root=tmp_path / "candidates",
        runs_root=tmp_path / "runs",
        cycle_receipts_root=tmp_path / "cycle-receipts",
    )

    assert receipt.stop_reason == "completed"
    assert receipt.final_candidate_id == "sql-agent/evolution-002"
    assert receipt.generations[0].smoke_run_id is None
    assert receipt.generations[1].smoke_run_id is not None
    assert calls["runner"] == [
        ("evolution", False, True),
        ("smoke", False, False),
        ("evolution", False, False),
    ]
    assert calls["provider"] == [
        ("advice", True),
        ("plan", True),
        ("advice", True),
        ("plan", True),
    ]
    assert calls["materialize"] == ["sql-agent/evolution-001", "sql-agent/evolution-002"]
    persisted = next((tmp_path / "cycle-receipts").glob("*.json")).read_text(encoding="utf-8")
    assert "PRIVATE SKILL" not in persisted
    assert json.loads(persisted)["final_candidate_id"] == "sql-agent/evolution-002"


def test_cycle_stops_when_a_child_smoke_gate_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _candidate("sql-agent/001-child", parent_candidate_id="sql-agent/000-baseline")
    candidates = _FakeCandidates({child.candidate_id: child})
    run_store = RunStore(tmp_path / "runs")
    provider_called = False

    monkeypatch.setattr(cycle_service, "load_manifest", lambda path: _manifest(enabled=False))
    monkeypatch.setattr(cycle_service, "_ensure_rotation_reserve", lambda *args: None)
    monkeypatch.setattr(cycle_service, "create_rotation_manifest", _fake_manifest_creator())
    monkeypatch.setattr(cycle_service, "load_rotation_suite", _fake_suite_loader)
    monkeypatch.setattr(
        cycle_service, "meets_smoke_gate_policy", lambda status, evaluations: False
    )

    def fake_runner(*args: object, **kwargs: object) -> object:
        del args, kwargs
        run = run_store.create()
        run.write_json("result.json", {"status": "task_failure"})
        return SimpleNamespace(run=run, status="task_failure", evaluations=())

    def fake_advice(*args: object, **kwargs: object) -> object:
        nonlocal provider_called
        del args, kwargs
        provider_called = True
        raise AssertionError("Smoke failure must stop before advice")

    monkeypatch.setattr(cycle_service, "run_rotation_dsh_suite", fake_runner)
    monkeypatch.setattr(cycle_service, "advise_after_evolution", fake_advice)

    receipt = cycle_service.run_evolution_cycle(
        EvolutionCycleRequest("sql-agent/001-child", 1, "evolution"),
        tmp_path / "data",
        tmp_path / "dsh.yaml",
        candidate_service=candidates,
        repository_root=tmp_path,
        candidate_store_root=tmp_path / "candidates",
        runs_root=tmp_path / "runs",
        cycle_receipts_root=tmp_path / "cycle-receipts",
    )

    assert receipt.stop_reason == "smoke_gate_not_met"
    assert receipt.final_candidate_id == child.candidate_id
    assert receipt.generations[0].evolution_run_id is None
    assert not provider_called


def test_cycle_continues_after_partial_evaluator_invalid_with_valid_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _candidate("sql-agent/000-baseline")
    candidates = _FakeCandidates({baseline.candidate_id: baseline})
    run_store = RunStore(tmp_path / "runs")
    calls: list[str] = []
    monkeypatch.setattr(cycle_service, "load_manifest", lambda path: _manifest(enabled=False))
    monkeypatch.setattr(cycle_service, "_ensure_rotation_reserve", lambda *args: None)
    monkeypatch.setattr(cycle_service, "create_rotation_manifest", _fake_manifest_creator())
    monkeypatch.setattr(cycle_service, "load_rotation_suite", _fake_suite_loader)

    def fake_runner(*args: object, **kwargs: object) -> object:
        del args, kwargs
        run = run_store.create()
        run.write_json("result.json", {"status": "partial_evaluator_invalid"})
        return SimpleNamespace(
            run=run,
            status="partial_evaluator_invalid",
            evaluations=(),
            evaluable_case_count=13,
            discarded_evaluator_invalid_count=2,
            optimizable_failure_count=1,
        )

    def fake_advice(*args: object, **kwargs: object) -> object:
        del args, kwargs
        calls.append("advice")
        return _advice_result("proposed")

    def fake_plan(*args: object, **kwargs: object) -> object:
        del args, kwargs
        calls.append("plan")
        return _plan_result("proposed")

    def fake_materialize(
        run: object, subject: object, candidate_service: object, candidate_name: str
    ) -> object:
        del run, subject, candidate_service
        calls.append("materialize")
        child_id = f"sql-agent/{candidate_name}"
        candidates.items[child_id] = _candidate(child_id, parent_candidate_id=baseline.candidate_id)
        return _materialization_result(child_id)

    monkeypatch.setattr(cycle_service, "run_rotation_dsh_suite", fake_runner)
    monkeypatch.setattr(cycle_service, "advise_after_evolution", fake_advice)
    monkeypatch.setattr(cycle_service, "plan_after_evolution", fake_plan)
    monkeypatch.setattr(cycle_service, "materialize_after_evolution", fake_materialize)

    receipt = cycle_service.run_evolution_cycle(
        EvolutionCycleRequest("sql-agent/000-baseline", 1, "evolution"),
        tmp_path / "data",
        tmp_path / "dsh.yaml",
        candidate_service=candidates,
        repository_root=tmp_path,
        candidate_store_root=tmp_path / "candidates",
        runs_root=tmp_path / "runs",
        cycle_receipts_root=tmp_path / "cycle-receipts",
    )

    assert receipt.stop_reason == "completed"
    assert calls == ["advice", "plan", "materialize"]
    generation = receipt.generations[0]
    assert generation.evolution_status == "partial_evaluator_invalid"
    assert generation.evolution_evaluable_case_count == 13
    assert generation.evolution_discarded_evaluator_invalid_count == 2


def test_cycle_skips_optimizer_when_partial_run_has_no_valid_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _candidate("sql-agent/000-baseline")
    candidates = _FakeCandidates({baseline.candidate_id: baseline})
    run_store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(cycle_service, "load_manifest", lambda path: _manifest(enabled=False))
    monkeypatch.setattr(cycle_service, "_ensure_rotation_reserve", lambda *args: None)
    monkeypatch.setattr(cycle_service, "create_rotation_manifest", _fake_manifest_creator())
    monkeypatch.setattr(cycle_service, "load_rotation_suite", _fake_suite_loader)

    def fake_runner(*args: object, **kwargs: object) -> object:
        del args, kwargs
        run = run_store.create()
        run.write_json("result.json", {"status": "partial_evaluator_invalid"})
        return SimpleNamespace(
            run=run,
            status="partial_evaluator_invalid",
            evaluations=(),
            evaluable_case_count=13,
            discarded_evaluator_invalid_count=2,
            optimizable_failure_count=0,
        )

    def unexpected_advice(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("empty valid failure set must not call the optimizer")

    monkeypatch.setattr(cycle_service, "run_rotation_dsh_suite", fake_runner)
    monkeypatch.setattr(cycle_service, "advise_after_evolution", unexpected_advice)

    receipt = cycle_service.run_evolution_cycle(
        EvolutionCycleRequest("sql-agent/000-baseline", 1, "evolution"),
        tmp_path / "data",
        tmp_path / "dsh.yaml",
        candidate_service=candidates,
        repository_root=tmp_path,
        candidate_store_root=tmp_path / "candidates",
        runs_root=tmp_path / "runs",
        cycle_receipts_root=tmp_path / "cycle-receipts",
    )

    assert receipt.stop_reason == "no_optimizable_failure"
    assert receipt.generations[0].advice_status is None


def test_cycle_rejects_enabled_manifest_optimizer_before_candidate_or_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_verify(candidate_id: str) -> object:
        nonlocal called
        del candidate_id
        called = True
        raise AssertionError("candidate verification must not follow an enabled optimizer")

    monkeypatch.setattr(cycle_service, "load_manifest", lambda path: _manifest(enabled=True))

    with pytest.raises(EvolutionCycleError, match="optimizer.enabled"):
        cycle_service.run_evolution_cycle(
            EvolutionCycleRequest("sql-agent/000-baseline", 1, "evolution"),
            tmp_path / "data",
            tmp_path / "dsh.yaml",
            candidate_service=SimpleNamespace(verify=fake_verify),
            repository_root=tmp_path,
            candidate_store_root=tmp_path / "candidates",
            runs_root=tmp_path / "runs",
            cycle_receipts_root=tmp_path / "cycle-receipts",
        )

    assert not called


def test_cycle_receipt_and_cli_stdout_exclude_private_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = EvolutionCycleReceipt(
        cycle_id="20260902T000000Z-abcdef12",
        initial_candidate_id="sql-agent/000-baseline",
        final_candidate_id="sql-agent/000-baseline",
        stop_reason="smoke_gate_not_met",
        generations=(),
    )
    calls: list[EvolutionCycleRequest] = []

    def fake_cycle(
        request: EvolutionCycleRequest, *args: object, **kwargs: object
    ) -> EvolutionCycleReceipt:
        del args, kwargs
        calls.append(request)
        return receipt

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skill_evolve.cli.run_evolution_cycle", fake_cycle)

    result = CliRunner().invoke(
        app,
        [
            "evolution",
            "cycle",
            "data",
            "dsh.yaml",
            "--candidate",
            "sql-agent/000-baseline",
            "--max-generations",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert calls == [EvolutionCycleRequest("sql-agent/000-baseline", 1, "evolution")]
    assert "PRIVATE SKILL" not in result.stdout
    assert json.loads(result.stdout) == receipt.public_dict()


def test_cycle_cli_maps_runner_failure_to_safe_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_cycle(*args: object, **kwargs: object) -> EvolutionCycleReceipt:
        del args, kwargs
        raise RotationRunError("rolling LiveSQLBench runs require dsh.runner: dsh")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("skill_evolve.cli.run_evolution_cycle", fake_cycle)

    result = CliRunner().invoke(
        app,
        [
            "evolution",
            "cycle",
            "data",
            "dsh.yaml",
            "--candidate",
            "sql-agent/000-baseline",
            "--max-generations",
            "1",
        ],
    )

    assert result.exit_code == 64
    assert "dsh.runner" in result.stderr


def test_cycle_request_rejects_invalid_bounds_and_generated_names() -> None:
    with pytest.raises(EvolutionCycleError, match="between 1 and 100"):
        EvolutionCycleRequest("sql-agent/000-baseline", 0, "evolution")
    with pytest.raises(EvolutionCycleError, match="between 1 and 100"):
        EvolutionCycleRequest("sql-agent/000-baseline", 101, "evolution")
    with pytest.raises(EvolutionCycleError, match="safe candidate name prefix"):
        EvolutionCycleRequest("sql-agent/000-baseline", 1, "invalid_prefix")
    with pytest.raises(EvolutionCycleError, match="generated child candidate name"):
        EvolutionCycleRequest("sql-agent/000-baseline", 1, "a" * 58).child_name_for_generation(
            10000
        )


class _FakeCandidates:
    def __init__(self, items: dict[str, object]) -> None:
        self.items = items

    def verify(self, candidate_id: str) -> object:
        return self.items[candidate_id]

    def list_candidates(self, skill_id: str) -> tuple[object, ...]:
        return tuple(
            candidate
            for candidate in self.items.values()
            if candidate.candidate_id.split("/", maxsplit=1)[0] == skill_id
        )


def _candidate(candidate_id: str, parent_candidate_id: str | None = None) -> object:
    content = b"PRIVATE SKILL CONTENT\n"
    return SimpleNamespace(
        candidate_id=candidate_id,
        skill_sha256=hashlib.sha256(content).hexdigest(),
        skill_bytes=content,
        metadata=SimpleNamespace(parent_candidate_id=parent_candidate_id),
    )


def _manifest(*, enabled: bool) -> object:
    return SimpleNamespace(
        profile_patch=None,
        manifest=SimpleNamespace(
            environment=SimpleNamespace(
                optimizer=OptimizerConfig(enabled=enabled),
                dsh=DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh"),
                model=ModelConfig(provider="test", name="stub"),
            )
        ),
    )


def _fake_manifest_creator() -> object:
    counter = 0

    def create(data_root: Path, kind: str, **kwargs: object) -> object:
        nonlocal counter
        del data_root, kwargs
        counter += 1
        return SimpleNamespace(
            manifest_id=f"{kind}-{counter}",
            manifest_sha256=f"{counter:064x}",
        )

    return create


def _fake_suite_loader(data_root: Path, manifest_id: str, **kwargs: object) -> object:
    del data_root, kwargs
    return SimpleNamespace(kind=manifest_id.split("-", maxsplit=1)[0], custody=object())


def _advice_result(status: str) -> object:
    return SimpleNamespace(
        receipt=SimpleNamespace(
            status=status,
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="skill-optimization-advice-prompt-v1",
        ),
        advice_path="optimizer/advice.json",
    )


def _plan_result(status: str) -> object:
    return SimpleNamespace(
        receipt=SimpleNamespace(
            status=status,
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="skill-edit-plan-prompt-v1",
            plan_sha256="a" * 64,
            skill_map_sha256="b" * 64,
            cluster_count=1,
            edit_count=1,
        ),
        plan_path="optimizer/edit-plan/edit-plan.json",
        skill_map_path="optimizer/edit-plan/skill-map.json",
    )


def _materialization_result(child_candidate_id: str) -> object:
    return SimpleNamespace(
        receipt=SimpleNamespace(
            status="created",
            source=SimpleNamespace(parent_candidate_id="sql-agent/000-baseline"),
            child_candidate_id=child_candidate_id,
            child_skill_sha256="c" * 64,
            patch_sha256="d" * 64,
            edit_count=1,
        ),
        patch_path="optimizer/materialization/rendered.patch",
    )
