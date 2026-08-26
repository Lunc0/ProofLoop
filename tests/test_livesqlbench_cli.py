"""Non-Docker command coverage for the LiveSQLBench local adapter."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from skill_evolve.cli import app
from tests.test_livesqlbench_import import _write_base_lite_fixture


def test_validate_command_reports_only_public_metadata(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["livesqlbench", "validate", str(_write_base_lite_fixture(tmp_path))],
    )

    assert result.exit_code == 0
    assert '"case_count": 5' in result.stdout
    assert "private gold" not in result.stdout
    assert "expected_query" not in result.stdout
