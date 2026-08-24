"""End-to-end workflow tests using the fake agent evidence and a real PostgreSQL fixture."""

from __future__ import annotations

import json
import subprocess

import pytest

from skill_evolve.attribution import AttributionKind, RunStatus
from skill_evolve.workflow import run_experiment
from tests.conftest import write_experiment


@pytest.mark.docker
def test_workflow_writes_a_completed_auditable_run(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path / "experiment")
    _pin_postgres_digest(manifest_path)
    runner_output = manifest_path.parent / "fake-results" / "results.json"
    runner_output.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "cases": [
                    {
                        "case_id": "customer-count",
                        "status": "completed",
                        "candidate_sql": "SELECT COUNT(*) FROM customers",
                        "trajectory": [
                            {"timestamp": "2026-01-01T00:00:00Z", "type": "tool", "payload": {}}
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    outcome = run_experiment(manifest_path, "fast", tmp_path / "runs")

    assert outcome.attribution.kind is AttributionKind.NONE
    assert outcome.attribution.status is RunStatus.COMPLETED
    assert (outcome.run.path / "raw" / "runner-output.json").is_file()
    assert (outcome.run.path / "trajectory.json").is_file()
    assert (outcome.run.path / "evaluation.json").is_file()
    assert (outcome.run.path / "result.json").is_file()
    assert "all_requested_cases_passed" in (outcome.run.path / "report.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.docker
def test_workflow_generates_regression_draft_for_skill_failure(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path / "experiment")
    _pin_postgres_digest(manifest_path)
    _write_runner_output(manifest_path, "SELECT 0")

    outcome = run_experiment(manifest_path, "fast", tmp_path / "runs")

    assert outcome.attribution.kind is AttributionKind.SKILL
    assert outcome.attribution.status is RunStatus.NEEDS_SKILL_CHANGE
    assert (outcome.run.path / "regression-case.draft.yaml").is_file()
    assert (outcome.run.path / "skill-change-suggestion.md").is_file()
    assert not (outcome.run.path / "repair-checklist.md").exists()


@pytest.mark.docker
def test_workflow_pauses_for_invalid_expected_query(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path / "experiment")
    _pin_postgres_digest(manifest_path)
    content = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        content.replace("SELECT COUNT(*) FROM customers", "SELECT * FORM customers"),
        encoding="utf-8",
    )
    _write_runner_output(manifest_path, "SELECT COUNT(*) FROM customers")

    outcome = run_experiment(manifest_path, "fast", tmp_path / "runs")

    assert outcome.attribution.kind is AttributionKind.EVAL_DATA
    assert outcome.attribution.status is RunStatus.EXPERIMENT_PAUSED
    assert (outcome.run.path / "repair-checklist.md").is_file()
    assert not (outcome.run.path / "regression-case.draft.yaml").exists()


def _write_runner_output(manifest_path, candidate_sql: str) -> None:
    runner_output = manifest_path.parent / "fake-results" / "results.json"
    runner_output.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "cases": [
                    {
                        "case_id": "customer-count",
                        "status": "completed",
                        "candidate_sql": candidate_sql,
                        "trajectory": [
                            {
                                "timestamp": "2026-01-01T00:00:00Z",
                                "type": "tool",
                                "payload": {},
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _pin_postgres_digest(manifest_path) -> None:
    completed = subprocess.run(
        ["docker", "image", "inspect", "postgres:16.6", "--format", "{{index .RepoDigests 0}}"],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0 or "@sha256:" not in completed.stdout:
        pytest.skip("postgres:16.6 image is unavailable")
    digest = "sha256:" + completed.stdout.strip().rsplit("@sha256:", maxsplit=1)[1]
    content = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(content.replace("sha256:" + "0" * 64, digest), encoding="utf-8")
