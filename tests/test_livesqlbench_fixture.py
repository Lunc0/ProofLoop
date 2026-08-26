"""Integration coverage for database-isolated LiveSQLBench dump restores."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skill_evolve.config.models import EvaluatorConfig
from skill_evolve.sql import LiveSQLBenchFixture


@pytest.mark.docker
def test_fixture_restores_dumps_and_prevents_cross_database_reads(tmp_path: Path) -> None:
    image = _available_postgres_image()
    dump_root = tmp_path / "dumps"
    _write_dump(dump_root, "alien", "Ada")
    _write_dump(dump_root, "polar", "Lin")
    evaluator = EvaluatorConfig(version="livesqlbench-base-lite-soft-ex-v1")

    with LiveSQLBenchFixture(dump_root, ("alien", "polar"), evaluator, image) as fixture:
        alien_reader = fixture.for_database("alien")
        own_data = alien_reader.execute_read_only("SELECT name FROM records")
        cross_database = alien_reader.execute_read_only(
            "SELECT name FROM livesqlbench_polar.records"
        )
        unsafe = alien_reader.execute_read_only("DELETE FROM records")

    assert own_data.ok
    assert own_data.result is not None
    assert own_data.result.rows == (("Ada",),)
    assert not cross_database.ok
    assert unsafe.unsafe


def _write_dump(dump_root: Path, database_name: str, name: str) -> None:
    dump_directory = dump_root / f"{database_name}_template"
    dump_directory.mkdir(parents=True)
    (dump_directory / "records.sql").write_text(
        "CREATE TABLE records (name text NOT NULL);\n"
        f"INSERT INTO records (name) VALUES ('{name}');\n",
        encoding="utf-8",
    )
    (dump_root / f"{database_name}_table_orders.txt").write_text("Records\n", encoding="utf-8")


def _available_postgres_image() -> str:
    for image in ("postgres:14.12", "postgres:16.6"):
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
        if completed.returncode == 0:
            return image
    pytest.skip("neither postgres:14.12 nor postgres:16.6 image is available")
