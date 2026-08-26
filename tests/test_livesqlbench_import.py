"""Unit coverage for the private-data-safe LiveSQLBench adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from skill_evolve.benchmark import (
    APPROVED_BASE_LITE_QUERY_IDS,
    LiveSQLBenchDataError,
    evaluate_query_case,
    load_base_lite_suite,
    public_evaluation_dict,
)
from skill_evolve.evaluator import Verdict
from skill_evolve.runner.contracts import RunnerCaseOutput, Usage
from skill_evolve.sql.postgres import QueryExecution, QueryResult


@dataclass
class StubExecutor:
    responses: list[QueryExecution]
    received_queries: list[str]

    def execute_read_only(self, query: str) -> QueryExecution:
        self.received_queries.append(query)
        return self.responses.pop(0)


def test_loads_selected_query_cases_and_keeps_gold_out_of_public_summary(tmp_path: Path) -> None:
    data_root = _write_base_lite_fixture(tmp_path)

    suite = load_base_lite_suite(data_root)

    assert suite.case_ids == APPROVED_BASE_LITE_QUERY_IDS
    assert tuple(case.database_name for case in suite.cases) == (
        "alien",
        "polar",
        "gaming",
        "archeology",
        "cross_db",
    )
    public_json = json.dumps(suite.public_summary())
    assert "private gold" not in public_json
    assert "SELECT expected" not in public_json
    assert len(suite.source_digests()) == 27


def test_rejects_a_non_query_approved_case(tmp_path: Path) -> None:
    data_root = _write_base_lite_fixture(tmp_path)
    public_file = data_root / "public" / "livesqlbench_data.jsonl"
    records = [json.loads(line) for line in public_file.read_text(encoding="utf-8").splitlines()]
    records[0]["category"] = "Management"
    public_file.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )

    with pytest.raises(LiveSQLBenchDataError, match="not a Query"):
        load_base_lite_suite(data_root)


def test_rejects_public_and_gt_case_id_mismatch(tmp_path: Path) -> None:
    data_root = _write_base_lite_fixture(tmp_path)
    gt_file = data_root / "gt" / "official_gt.jsonl"
    records = [json.loads(line) for line in gt_file.read_text(encoding="utf-8").splitlines()]
    records[-1]["instance_id"] = "different_case"
    gt_file.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )

    with pytest.raises(LiveSQLBenchDataError, match="case IDs must match exactly"):
        load_base_lite_suite(data_root)


def test_soft_ex_removes_comments_distinct_and_round_without_public_gold_leak(
    tmp_path: Path,
) -> None:
    suite = load_base_lite_suite(_write_base_lite_fixture(tmp_path))
    case = suite.cases[0]
    runner_case = RunnerCaseOutput(
        case_id=case.instance_id,
        status="completed",
        candidate_sql="SELECT DISTINCT ROUND(score, 2) FROM results -- display score\n",
        trajectory=(),
        usage=Usage(cost_usd=0.01),
    )
    executor = StubExecutor(
        responses=[
            QueryExecution(result=QueryResult(columns=("score",), rows=((1.234,), (2.345,)))),
            QueryExecution(
                result=QueryResult(columns=("score",), rows=((2.3451,), (1.2344,), (1.2344,)))
            ),
        ],
        received_queries=[],
    )

    evaluation = evaluate_query_case(case, runner_case, executor)

    assert evaluation.verdict is Verdict.PASS
    assert executor.received_queries[1] == "SELECT  score FROM results"
    public_result = public_evaluation_dict(evaluation)
    assert public_result["candidate_sql"] == runner_case.candidate_sql
    assert "expected_query" not in public_result
    assert "expected_result" not in public_result
    assert "private gold" not in json.dumps(public_result)


def _write_base_lite_fixture(tmp_path: Path) -> Path:
    data_root = tmp_path / "base-lite"
    public_root = data_root / "public"
    gt_root = data_root / "gt"
    dump_root = data_root / "dumps" / "base-lite" / "livesqlbench-base-lite-dumps"
    public_records: list[dict[str, object]] = []
    gt_records: list[dict[str, object]] = []
    databases = ("alien", "polar", "gaming", "archeology", "cross_db")
    for instance_id, database_name in zip(
        APPROVED_BASE_LITE_QUERY_IDS, databases, strict=True
    ):
        public_directory = public_root / database_name
        public_directory.mkdir(parents=True)
        (public_directory / f"{database_name}_schema.txt").write_text("schema", encoding="utf-8")
        (public_directory / f"{database_name}_kb.jsonl").write_text("{}\n", encoding="utf-8")
        (public_directory / f"{database_name}_column_meaning_base.json").write_text(
            "{}\n", encoding="utf-8"
        )
        public_records.append(
            {
                "instance_id": instance_id,
                "category": "Query",
                "selected_database": database_name,
                "query": f"Find values for {database_name}.",
                "conditions": {"order": database_name in {"gaming", "archeology", "cross_db"}},
                "difficulty_tier": "Simple",
            }
        )
        gt_records.append(
            {
                "instance_id": instance_id,
                "sol_sql": ["SELECT expected FROM private_gold"],
                "external_knowledge": [1],
            }
        )
        dump_directory = dump_root / f"{database_name}_template"
        dump_directory.mkdir(parents=True)
        (dump_directory / "records.sql").write_text(
            "CREATE TABLE records (id integer);", encoding="utf-8"
        )
        (dump_root / f"{database_name}_table_orders.txt").write_text("records\n", encoding="utf-8")
    (public_root / "livesqlbench_data.jsonl").write_text(
        "\n".join(json.dumps(record) for record in public_records) + "\n", encoding="utf-8"
    )
    gt_root.mkdir(parents=True)
    (gt_root / "official_gt.jsonl").write_text(
        "\n".join(json.dumps(record) for record in gt_records) + "\n", encoding="utf-8"
    )
    return data_root
