"""Integration coverage for the disposable, non-production PostgreSQL fixture."""

from __future__ import annotations

import subprocess

import pytest

from skill_evolve.config.models import DatabaseConfig, EvaluatorConfig
from skill_evolve.sql.postgres import PostgresFixture


@pytest.mark.docker
def test_postgres_fixture_executes_only_read_queries(tmp_path) -> None:
    digest = _postgres_digest()
    schema_file = tmp_path / "schema.sql"
    seed_file = tmp_path / "seed.sql"
    schema_file.write_text(
        "CREATE TABLE customers (id integer PRIMARY KEY, name text NOT NULL);", encoding="utf-8"
    )
    seed_file.write_text(
        "INSERT INTO customers (id, name) VALUES (1, 'Ada'), (2, 'Lin');", encoding="utf-8"
    )
    database = DatabaseConfig(
        kind="postgresql",
        image="postgres:16.6",
        image_digest=digest,
        schema="schema.sql",
        seed="seed.sql",
    )
    evaluator = EvaluatorConfig(version="sql-result-v1")

    with PostgresFixture(database, evaluator, schema_file, seed_file) as fixture:
        success = fixture.execute_read_only("SELECT name FROM customers ORDER BY id")
        unsafe = fixture.execute_read_only("DELETE FROM customers")

    assert success.ok
    assert success.result is not None
    assert success.result.rows == (("Ada",), ("Lin",))
    assert unsafe.unsafe


def _postgres_digest() -> str:
    completed = subprocess.run(
        ["docker", "image", "inspect", "postgres:16.6", "--format", "{{index .RepoDigests 0}}"],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0 or "@sha256:" not in completed.stdout:
        pytest.skip("postgres:16.6 image is unavailable")
    return "sha256:" + completed.stdout.strip().rsplit("@sha256:", maxsplit=1)[1]
