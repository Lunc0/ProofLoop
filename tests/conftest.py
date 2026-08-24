"""Shared temporary experiment fixtures."""

from __future__ import annotations

import json
from pathlib import Path


def write_experiment(tmp_path: Path, extra: str = "") -> Path:
    """Write the smallest valid fake-runner experiment and its declared inputs."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "fake-results").mkdir()
    (tmp_path / "fixtures" / "schema.sql").write_text(
        "CREATE TABLE customers(id integer);\n", encoding="utf-8"
    )
    (tmp_path / "fixtures" / "seed.sql").write_text(
        "INSERT INTO customers VALUES (1);\n", encoding="utf-8"
    )
    (tmp_path / "tools" / "sql-readonly.json").write_text(
        '{"name":"sql-readonly"}\n', encoding="utf-8"
    )
    (tmp_path / "fake-results" / "results.json").write_text(
        json.dumps({"schema_version": "v1", "cases": []}), encoding="utf-8"
    )
    digest = "sha256:" + "0" * 64
    manifest = f"""\
schema_version: v1
experiment: sales-sql-skill
environment:
  model:
    provider: deepseek
    name: deepseek-v4
    params:
      temperature: 0
  dsh:
    version: 0.1.0-rc.6
    profile: sql-eval
    runner: fake
    fake_output: fake-results/results.json
  tools:
    - id: sql-readonly
      version: 1.0.0
      manifest: tools/sql-readonly.json
  database:
    kind: postgresql
    image: postgres:16.6
    image_digest: {digest}
    schema: fixtures/schema.sql
    seed: fixtures/seed.sql
  evaluator:
    version: sql-result-v1
cases:
  - id: customer-count
    slice: fast
    prompt: Count customers.
    expected_query: SELECT COUNT(*) FROM customers
{extra}"""
    path = tmp_path / "experiment.yaml"
    path.write_text(manifest, encoding="utf-8")
    return path
