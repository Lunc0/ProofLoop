"""Manifest validation and reproducibility tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_evolve.candidate import CandidateService, LocalCandidateStore
from skill_evolve.config import ManifestError, load_manifest
from skill_evolve.config.models import DshConfig, OptimizerConfig
from skill_evolve.fingerprint import create_snapshot
from skill_evolve.runner.contracts import RawArtifact
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


def test_snapshot_binds_verified_local_candidate_identity_and_hash(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path / "experiment")
    skill = tmp_path / "incoming" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("# Candidate\n", encoding="utf-8")
    candidate_service = CandidateService(
        LocalCandidateStore(tmp_path / ".local" / "skill-evolve")
    )
    candidate = candidate_service.initialise(
        "sql-analyst", skill
    )

    without_candidate = create_snapshot(load_manifest(manifest_path))
    with_candidate = create_snapshot(load_manifest(manifest_path), candidate)

    assert with_candidate.fingerprint != without_candidate.fingerprint
    assert with_candidate.candidate == candidate.public_binding()
    assert "SKILL.md" not in str(with_candidate.to_dict())


def test_workspace_dotenv_resolves_only_dsh_binary_and_changes_fingerprint(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path)
    before = create_snapshot(load_manifest(manifest_path))
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=must-not-be-read-or-persisted\n"
        "SKILL_EVOLVE_DSH_BINARY='C:/Tools/dsh.exe'\n",
        encoding="utf-8",
    )

    resolved = load_manifest(manifest_path)
    after = create_snapshot(resolved)

    assert resolved.manifest.environment.dsh.binary == "C:/Tools/dsh.exe"
    assert before.fingerprint != after.fingerprint
    assert "must-not-be-read-or-persisted" not in str(after.to_dict())


def test_process_dsh_binary_override_takes_precedence_over_workspace_dotenv(
    monkeypatch, tmp_path
) -> None:
    manifest_path = write_experiment(tmp_path)
    (tmp_path / ".env").write_text(
        "SKILL_EVOLVE_DSH_BINARY=from-dotenv\n", encoding="utf-8"
    )
    monkeypatch.setenv("SKILL_EVOLVE_DSH_BINARY", "from-process")

    resolved = load_manifest(manifest_path)

    assert resolved.manifest.environment.dsh.binary == "from-process"


def test_dsh_session_log_root_resolves_from_non_secret_dotenv(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path)
    (tmp_path / ".env").write_text(
        "SKILL_EVOLVE_DSH_SESSION_LOG_ROOT=C:/Users/test/.dsh/sessions\n", encoding="utf-8"
    )

    resolved = load_manifest(manifest_path)

    assert resolved.manifest.environment.dsh.session_log_root == "C:/Users/test/.dsh/sessions"


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


def test_dsh_runner_args_reject_credential_options() -> None:
    with pytest.raises(ValidationError, match="must not contain credential"):
        DshConfig(
            version="0.1.1-rc.2",
            profile="headless",
            runner="dsh",
            runner_args=("--api-key", "not-a-secret-here"),
        )


def test_optimizer_key_reference_is_not_treated_as_a_literal_secret(tmp_path) -> None:
    manifest_path = write_experiment(tmp_path)
    content = manifest_path.read_text(encoding="utf-8").replace(
        "  evaluator:\n",
        """  optimizer:
    enabled: true
    provider: deepseek
    model: deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
  evaluator:
""",
    )
    manifest_path.write_text(content, encoding="utf-8")

    resolved = load_manifest(manifest_path)

    assert resolved.manifest.environment.optimizer == OptimizerConfig(enabled=True)


@pytest.mark.parametrize("path", ("../outside.log", "dsh\\windows.log", "/absolute.log"))
def test_raw_artifact_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="relative POSIX"):
        RawArtifact(relative_path=path, content=b"evidence")
