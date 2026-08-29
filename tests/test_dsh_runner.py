"""DSH adapter tests without a live model or DSH installation."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from skill_evolve.candidate import (
    CandidateService,
    ExternalSource,
    LocalCandidateStore,
    subject_from_candidate,
)
from skill_evolve.candidate.store import skill_sha256_for_source
from skill_evolve.config.models import CaseConfig, DshConfig, ModelConfig
from skill_evolve.runner.contracts import (
    ReadOnlyDatabaseAccess,
    RunInput,
    RunnerError,
    TrajectoryEvent,
    Usage,
)
from skill_evolve.runner.dsh import DshRunner
from skill_evolve.runner.dsh_session import LoadedDshSessionLog, SessionEvidence


def _run_input(workspace: Path) -> RunInput:
    return RunInput(
        run_id="run-123",
        workspace=workspace,
        environment_fingerprint="f" * 64,
        subject_ref=None,
        cases=(
            CaseConfig(
                id="customer-count",
                slice="fast",
                prompt="Count customers by country.",
                expected_query="SELECT country, COUNT(*) FROM customers GROUP BY country",
            ),
        ),
    )


def _config() -> DshConfig:
    return DshConfig(
        version="0.1.1-rc.2",
        profile="headless",
        runner="dsh",
        runner_args=("--example-app-flag",),
    )


def _model() -> ModelConfig:
    return ModelConfig(provider="deepseek-official", name="deepseek-v4-flash")


def test_dsh_runner_invokes_headless_profile_and_extracts_one_sql_block(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, Any] = {}
    profile_patch = tmp_path / "profile.patch.yml"
    profile_patch.write_text("{}\n", encoding="utf-8")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        patch_indices = [index for index, value in enumerate(command) if value == "--patch"]
        selection_patch_path = Path(command[patch_indices[-1] + 1])
        selection_patch = yaml.safe_load(selection_patch_path.read_text(encoding="utf-8"))
        settings_path = Path(selection_patch[0]["config"]["path"])
        captured["selection_patch"] = selection_patch
        captured["settings_path"] = settings_path
        captured["effective_settings"] = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="分析完成。\n```sql\nSELECT COUNT(*) FROM customers\n```\n",
            stderr="",
        )

    monkeypatch.setattr("skill_evolve.runner.dsh.subprocess.run", fake_run)

    execution = DshRunner(_config(), profile_patch, _model()).run(_run_input(tmp_path))

    case = execution.output.cases[0]
    profile_index = captured["command"].index("--profile")
    assert captured["command"][profile_index : profile_index + 4] == [
        "--profile",
        "headless",
        "--patch",
        str(profile_patch),
    ]
    assert captured["command"][profile_index + 4] == "--example-app-flag"
    assert captured["command"][profile_index + 5] == "--patch"
    assert captured["command"][profile_index + 6].endswith("model-selection.patch.yml")
    assert "Count customers by country." in captured["command"][-1]
    assert captured["kwargs"]["cwd"] == tmp_path
    assert captured["kwargs"]["shell"] is False
    assert case.status == "completed"
    assert case.candidate_sql == "SELECT COUNT(*) FROM customers"
    assert case.usage.cost_usd is None
    artifacts = {
        artifact.relative_path: artifact.content.decode("utf-8")
        for artifact in execution.raw_artifacts
    }
    invocation = json.loads(artifacts["dsh/customer-count.invocation.json"])
    assert invocation["timeout_ms"] == 300_000
    assert invocation["model"] == {
        "provider": "deepseek-official",
        "name": "deepseek-v4-flash",
    }
    selection_patch_path = Path(captured["command"][profile_index + 6])
    selection_patch = captured["selection_patch"]
    assert selection_patch[0]["id"] == "settings"
    assert selection_patch[0]["config"]["watch"] is False
    assert Path(selection_patch[0]["config"]["path"]) == captured["settings_path"]
    assert captured["effective_settings"] == {
        "agent-default-model": {
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
        }
    }
    assert not captured["settings_path"].exists()
    assert yaml.safe_load(artifacts["dsh/customer-count.model-selection.yaml"]) == {
        "agent-default-model": {
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
        }
    }
    saved_selection_patch = artifacts["dsh/customer-count.model-selection.patch.yml"]
    assert saved_selection_patch == selection_patch_path.read_text(encoding="utf-8")
    assert artifacts["dsh/customer-count.stdout.log"] == case.final_message


def test_dsh_runner_preserves_non_secret_provider_routes_while_locking_model(
    monkeypatch, tmp_path
) -> None:
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir()
    source_settings = {
        "ui-onboarding": {"welcomeNoticeVersion": "2026-08-13.1"},
        "agent-default-model": {
            "provider": "deepseek-official",
            "model": "deepseek-v4-pro",
        },
        "llm-pi-ai": {
            "providers": {
                "openai": {
                    "apiKeyEnv": "OPENAI_API_KEY",
                    "retryPolicy": {"mode": "normal", "maxRetries": 2},
                }
            }
        },
    }
    (dsh_home / "settings.yaml").write_text(
        yaml.safe_dump(source_settings, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        patch_indices = [index for index, value in enumerate(command) if value == "--patch"]
        patch = yaml.safe_load(Path(command[patch_indices[-1] + 1]).read_text(encoding="utf-8"))
        settings_path = Path(patch[0]["config"]["path"])
        captured["settings_path"] = settings_path
        captured["settings"] = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, stdout="```sql\nSELECT 1\n```\n", stderr="")

    monkeypatch.setattr("skill_evolve.runner.dsh.subprocess.run", fake_run)
    model = ModelConfig(provider="openai", name="gpt-5.6-sol")
    DshRunner(_config(), None, model).run(_run_input(tmp_path))

    assert captured["settings"] == {
        "llm-pi-ai": source_settings["llm-pi-ai"],
        "agent-default-model": {"provider": "openai", "model": "gpt-5.6-sol"},
    }
    assert not captured["settings_path"].exists()
    final_user_settings = yaml.safe_load((dsh_home / "settings.yaml").read_text(encoding="utf-8"))
    assert final_user_settings == source_settings


def test_dsh_runner_rejects_plaintext_provider_credential(monkeypatch, tmp_path) -> None:
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir()
    (dsh_home / "settings.yaml").write_text(
        "llm-pi-ai:\n  providers:\n    openai:\n      apiKey: sk-not-a-credential-reference\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DSH_HOME", str(dsh_home))
    monkeypatch.setattr(
        "skill_evolve.runner.dsh.subprocess.run",
        lambda *args, **kwargs: pytest.fail(
            "DSH must not start with a plaintext provider credential"
        ),
    )

    with pytest.raises(RunnerError, match="DshModelSettingsError"):
        DshRunner(_config(), None, _model()).run(_run_input(tmp_path))


def test_dsh_runner_uses_isolated_task_workspace_with_only_declared_context(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, Any] = {}
    schema_file = tmp_path / "schema.sql"
    context_file = tmp_path / "policy.md"
    schema_file.write_text("CREATE TABLE customers(id integer);\n", encoding="utf-8")
    context_file.write_text("Customers are active.\n", encoding="utf-8")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="```sql\nSELECT COUNT(*) FROM customers\n```\n",
            stderr="",
        )

    monkeypatch.setattr("skill_evolve.runner.dsh.subprocess.run", fake_run)
    run_input = replace(
        _run_input(tmp_path),
        schema_file=schema_file,
        case_context_files=(("customer-count", (context_file,)),),
        agent_workspace_root=tmp_path / "agent-workspaces",
        database_access=ReadOnlyDatabaseAccess(query_endpoint="http://127.0.0.1:54321/query"),
    )

    execution = DshRunner(_config(), None, _model()).run(run_input)

    workspace = tmp_path / "agent-workspaces" / "customer-count"
    assert captured["cwd"] == workspace
    assert (workspace / "schema.sql").read_text(encoding="utf-8") == schema_file.read_text(
        encoding="utf-8"
    )
    assert (workspace / "context" / "01-policy.md").is_file()
    assert "expected_query" not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in workspace.rglob("*")
        if path.is_file()
    )
    assert "127.0.0.1:54321" in (workspace / "sql-readonly.ps1").read_text(encoding="utf-8")
    assert execution.output.cases[0].candidate_sql == "SELECT COUNT(*) FROM customers"


def test_dsh_runner_injects_only_verified_skill_and_requires_it_to_be_read(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, Any] = {}
    incoming_skill = tmp_path / "incoming" / "SKILL.md"
    incoming_notice = tmp_path / "incoming" / "NOTICE.md"
    incoming_skill.parent.mkdir()
    incoming_skill.write_text("# Candidate guidance\n", encoding="utf-8")
    incoming_notice.write_text("MIT License\n", encoding="utf-8")
    source = ExternalSource(
        canonical_url="https://github.com/bytebase/dbhub/blob/0123456789abcdef0123456789abcdef01234567/skills/dbhub/SKILL.md",
        source_repository="bytebase/dbhub",
        commit="0123456789abcdef0123456789abcdef01234567",
        file_sha256=skill_sha256_for_source(incoming_skill),
        license_id="MIT",
    )
    candidate_service = CandidateService(
        LocalCandidateStore(tmp_path / ".local" / "skill-evolve")
    )
    candidate = candidate_service.initialise(
        "dbhub",
        incoming_skill,
        candidate_name="000-upstream-baseline",
        external_source=source,
        notice_source=incoming_notice,
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(command, 0, stdout="```sql\nSELECT 1\n```\n", stderr="")

    monkeypatch.setattr("skill_evolve.runner.dsh.subprocess.run", fake_run)
    execution = DshRunner(_config(), None, _model()).run(
        replace(
            _run_input(tmp_path),
            subject=subject_from_candidate(candidate),
            subject_ref=candidate.candidate_id,
            agent_workspace_root=tmp_path / "agent-workspaces",
        )
    )

    workspace = captured["cwd"]
    assert (workspace / "subject" / "SKILL.md").read_bytes() == candidate.skill_bytes
    assert not (workspace / "subject" / "NOTICE.md").exists()
    assert "subject/SKILL.md" in (workspace / "TASK.md").read_text(encoding="utf-8")
    assert "subject/SKILL.md" in captured["command"][-1]
    assert execution.output.cases[0].candidate_sql == "SELECT 1"


def test_dsh_runner_uses_session_log_as_authoritative_evidence(monkeypatch, tmp_path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "session.jsonl.zstd"
    session_path.write_bytes(b"compressed-log")
    config = DshConfig(
        version="0.1.1-rc.2",
        profile="headless",
        runner="dsh",
        session_log_root=str(session_root),
    )
    loaded_log = LoadedDshSessionLog(events=(), redacted_jsonl=b'{"type":"session"}\n')
    evidence = SessionEvidence(
        session_id="session-123",
        final_message="```sql\nSELECT COUNT(*) FROM customers\n```",
        trajectory=(
            TrajectoryEvent(
                timestamp="2026-08-25T00:00:00Z", type="dsh_session_start", payload={}
            ),
        ),
        usage=Usage(input_tokens=12, output_tokens=4),
        unknown_event_types=(),
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, stdout="not authoritative", stderr="")

    monkeypatch.setattr("skill_evolve.runner.dsh.subprocess.run", fake_run)
    monkeypatch.setattr("skill_evolve.runner.dsh.snapshot_session_logs", lambda root: {})
    monkeypatch.setattr(
        "skill_evolve.runner.dsh.find_changed_session_logs", lambda before, root: (session_path,)
    )
    monkeypatch.setattr("skill_evolve.runner.dsh.load_session_log", lambda path: loaded_log)
    monkeypatch.setattr(
        "skill_evolve.runner.dsh._session_log_for_workspace",
        lambda candidates, workspace: (session_path, loaded_log),
    )
    monkeypatch.setattr("skill_evolve.runner.dsh.normalise_session_log", lambda log: evidence)

    execution = DshRunner(config, None, _model()).run(_run_input(tmp_path))

    case = execution.output.cases[0]
    assert case.final_message == evidence.final_message
    assert case.candidate_sql == "SELECT COUNT(*) FROM customers"
    assert case.usage.input_tokens == 12
    artifacts = {artifact.relative_path: artifact.content for artifact in execution.raw_artifacts}
    assert artifacts["dsh/customer-count.session.jsonl"] == loaded_log.redacted_jsonl


def test_dsh_runner_treats_nonzero_exit_as_runner_evidence_failure_and_redacts(
    monkeypatch, tmp_path
) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="DEEPSEEK_API_KEY=sk-abcdefghijklmnop\n",
            stderr="Authorization: Bearer ds-abcdefghijklmnop\n",
        )

    monkeypatch.setattr("skill_evolve.runner.dsh.subprocess.run", fake_run)

    execution = DshRunner(_config(), None, _model()).run(_run_input(tmp_path))

    case = execution.output.cases[0]
    artifacts = {
        artifact.relative_path: artifact.content.decode("utf-8")
        for artifact in execution.raw_artifacts
    }
    assert case.status == "runner_error"
    assert case.agent_execution is not None
    assert case.agent_execution.error == "DSH exited with code 1"
    assert "sk-abcdefghijklmnop" not in artifacts["dsh/customer-count.stdout.log"]
    assert "ds-abcdefghijklmnop" not in artifacts["dsh/customer-count.stderr.log"]
    assert "<redacted>" in artifacts["dsh/customer-count.stdout.log"]
    assert "<redacted>" in artifacts["dsh/customer-count.stderr.log"]


@pytest.mark.parametrize("failure", ("timeout", "nonzero-exit"))
def test_dsh_runner_preserves_changed_session_log_on_process_failure(
    monkeypatch, tmp_path, failure: str
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "session.jsonl.zstd"
    session_path.write_bytes(b"compressed-log")
    config = DshConfig(
        version="0.1.1-rc.2",
        profile="headless",
        runner="dsh",
        session_log_root=str(session_root),
    )
    loaded_log = LoadedDshSessionLog(
        events=(), redacted_jsonl=b'{"type":"session"}\n'
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 300, output="partial output", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

    monkeypatch.setattr("skill_evolve.runner.dsh.subprocess.run", fake_run)
    monkeypatch.setattr("skill_evolve.runner.dsh.snapshot_session_logs", lambda root: {})
    monkeypatch.setattr(
        "skill_evolve.runner.dsh.find_changed_session_logs", lambda before, root: (session_path,)
    )
    monkeypatch.setattr(
        "skill_evolve.runner.dsh._session_log_for_workspace",
        lambda candidates, workspace: (session_path, loaded_log),
    )

    execution = DshRunner(config, None, _model()).run(_run_input(tmp_path))

    case = execution.output.cases[0]
    artifacts = {artifact.relative_path: artifact.content for artifact in execution.raw_artifacts}
    assert case.status == "runner_error"
    assert case.candidate_sql is None
    assert artifacts["dsh/customer-count.session.jsonl"] == loaded_log.redacted_jsonl


@pytest.mark.skipif(os.name != "nt", reason="npm command shims are Windows-specific")
def test_dsh_runner_uses_node_entry_for_a_windows_npm_shim(monkeypatch, tmp_path) -> None:
    shim_dir = tmp_path / "npm"
    shim_dir.mkdir()
    shim = shim_dir / "dsh.cmd"
    shim.write_text("npm shim", encoding="utf-8")
    entry = shim_dir / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("console.log('dsh')", encoding="utf-8")

    def fake_which(value: str) -> str | None:
        return str(shim) if value == "dsh.cmd" else None

    monkeypatch.setattr("skill_evolve.runner.dsh.shutil.which", fake_which)

    command = DshRunner(_config(), None, _model())._command_for(_run_input(tmp_path).cases[0])

    assert command[:2] == ["node", str(entry)]
    assert command[2:4] == ["--profile", "headless"]
