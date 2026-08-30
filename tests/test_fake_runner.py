"""Runner contract tests that do not require Docker or a model provider."""

from __future__ import annotations

import json

import pytest

from skill_evolve.config import load_manifest
from skill_evolve.runner import FakeRunner
from skill_evolve.runner.contracts import RunInput, RunnerError
from tests.conftest import write_experiment


def _run_input(tmp_path) -> RunInput:
    resolved = load_manifest(write_experiment(tmp_path))
    return RunInput(
        run_id="run-123",
        workspace=resolved.workspace,
        environment_fingerprint="f" * 64,
        subject_ref=None,
        cases=resolved.manifest.cases,
    )


def test_fake_runner_selects_requested_case_evidence(tmp_path) -> None:
    run_input = _run_input(tmp_path)
    fixture = run_input.workspace / "fake-results" / "results.json"
    fixture.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "cases": [
                    {
                        "case_id": "customer-count",
                        "status": "completed",
                        "candidate_sql": "SELECT COUNT(*) FROM customers",
                        "trajectory": [
                            {
                                "timestamp": "2026-01-01T00:00:00Z",
                                "type": "assistant",
                                "payload": {},
                            }
                        ],
                        "usage": {"cost_usd": 0.01},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    execution = FakeRunner(fixture).run(run_input)

    assert execution.output.cases[0].candidate_sql == "SELECT COUNT(*) FROM customers"
    assert execution.output.cases[0].usage.cost_usd == 0.01
    assert execution.raw_output == fixture.read_bytes()


def test_fake_runner_rejects_missing_case_evidence(tmp_path) -> None:
    run_input = _run_input(tmp_path)
    fixture = run_input.workspace / "fake-results" / "results.json"

    with pytest.raises(RunnerError, match="missing requested cases"):
        FakeRunner(fixture).run(run_input)
