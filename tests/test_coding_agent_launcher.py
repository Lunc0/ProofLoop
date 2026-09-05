from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skill_evolve.optimizer.coding_agent.launcher import (
    CodexLaunchSettings,
    codex_exec_argv,
    invoke_codex,
)
from skill_evolve.optimizer.coding_agent.workspace import isolated_agent_workspace


def test_codex_argv_uses_permission_profile_without_sandbox(tmp_path: Path) -> None:
    args = codex_exec_argv("codex", tmp_path, None)
    assert "--sandbox" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert 'default_permissions="skill-optimizer"' in args
    assert 'permissions.skill-optimizer.filesystem.":workspace_roots".output="write"' in args
    assert "permissions.skill-optimizer.network.enabled=false" in args
    assert 'approval_policy="never"' in args
    assert 'web_search="disabled"' in args
    assert args[-1] == "-"


def test_invoke_codex_probes_version_and_uses_prompt_stdin(tmp_path: Path) -> None:
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, "codex-cli 0.153.1\n", "")
        return subprocess.CompletedProcess(args, 0, "{}\n", "")

    with isolated_agent_workspace(
        dedicated,
        {
            "SKILL.md": b"# Skill\n",
            "skill-map.json": b"{}\n",
            "evidence.json": b"{}\n",
            "manifest.json": b"{}\n",
        },
        private_roots=(),
    ) as workspace:
        receipt = invoke_codex(
            workspace,
            "prompt",
            CodexLaunchSettings(binary=sys.executable),
            runner=fake_runner,
            environment={},
        )
    assert receipt.codex_cli_version == "0.153.1"
    assert calls[1][1]["input"] == "prompt"
    assert calls[1][1]["shell"] is False


def test_launch_settings_rejects_unsafe_model_override() -> None:
    with pytest.raises(ValueError, match="model"):
        CodexLaunchSettings(model='gpt-5" -c approval_policy="on-request"')
