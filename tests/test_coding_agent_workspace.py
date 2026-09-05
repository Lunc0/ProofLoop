from __future__ import annotations

from pathlib import Path

import pytest

from skill_evolve.optimizer.coding_agent.workspace import (
    AgentWorkspaceError,
    isolated_agent_workspace,
)


def test_workspace_hashes_inputs_and_accepts_only_formal_regular_outputs(tmp_path: Path) -> None:
    dedicated = tmp_path / "dedicated"
    private = tmp_path / "private"
    dedicated.mkdir()
    private.mkdir()
    with isolated_agent_workspace(
        dedicated,
        {
            "SKILL.md": b"# Skill\n",
            "skill-map.json": b"{}\n",
            "evidence.json": b"{}\n",
            "manifest.json": b"{}\n",
        },
        private_roots=(private,),
    ) as workspace:
        workspace.verify_input_hash()
        (workspace.output_dir / "SKILL.md").write_bytes(b"# Changed\n")
        assert workspace.read_output("SKILL.md", max_bytes=1024) == b"# Changed\n"
        root = workspace.root
    assert not root.exists()


def test_workspace_detects_input_tampering_and_private_root_overlap(tmp_path: Path) -> None:
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir()
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
        (workspace.input_dir / "SKILL.md").write_bytes(b"tampered")
        with pytest.raises(AgentWorkspaceError) as error:
            workspace.verify_input_hash()
        assert error.value.code == "input_tampered"

    with pytest.raises(AgentWorkspaceError, match="overlaps"), isolated_agent_workspace(
        dedicated,
        {
            "SKILL.md": b"# Skill\n",
            "skill-map.json": b"{}\n",
            "evidence.json": b"{}\n",
            "manifest.json": b"{}\n",
        },
        private_roots=(tmp_path,),
    ):
        pass


def test_workspace_rejects_replaced_output_directory(tmp_path: Path) -> None:
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir()
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
        displaced = workspace.root / "displaced-output"
        workspace.output_dir.rename(displaced)
        workspace.output_dir.mkdir()
        (workspace.output_dir / "SKILL.md").write_bytes(b"# Unbound\n")
        with pytest.raises(AgentWorkspaceError) as error:
            workspace.read_output("SKILL.md", max_bytes=1024)
        assert error.value.code == "unsafe_output"
