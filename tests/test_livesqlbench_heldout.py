"""Custody and leak-boundary coverage for the final LiveSQLBench suite."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skill_evolve.benchmark import (
    HELDOUT_ROOT_ENVIRONMENT_VARIABLE,
    HeldOutError,
    initialise_heldout_manifest,
    load_heldout_suite,
    resolve_heldout_custody,
)
from skill_evolve.benchmark.contracts import BenchmarkAdapterError
from skill_evolve.benchmark.livesqlbench_heldout_adapter import (
    LiveSQLBenchBaseLiteHeldOutAdapter,
)
from skill_evolve.cli import app
from skill_evolve.config.models import DshConfig, ModelConfig
from skill_evolve.runner.contracts import (
    RawArtifact,
    RunInput,
    RunnerCaseOutput,
    RunnerExecution,
    RunnerOutput,
)
from skill_evolve.sql.postgres import QueryExecution, QueryResult
from tests.conftest import write_experiment

_SMOKE_IDS = {"alien_2", "polar_10", "gaming_1", "archeology_5", "cross_db_8"}


@dataclass
class StubExecutor:
    def execute_read_only(self, query: str) -> QueryExecution:
        del query
        return QueryExecution(result=QueryResult(columns=("value",), rows=((1,),)))


class StubFixture:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def __enter__(self) -> StubFixture:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback

    def for_database(self, database_name: str) -> StubExecutor:
        assert database_name.startswith("db")
        return StubExecutor()


class StubQueryService:
    def __init__(self, executor: StubExecutor) -> None:
        del executor

    def __enter__(self) -> StubQueryService:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:54321/query"


class StubDshRunner:
    def __init__(
        self, config: DshConfig, profile_patch: Path | None, model: ModelConfig
    ) -> None:
        del config, profile_patch, model

    def run(self, run_input: RunInput) -> RunnerExecution:
        cases = run_input.cases
        assert len(cases) == 1
        case = cases[0]
        assert case.slice == "held_out"
        assert case.expected_query == "SELECT 1"
        output_case = RunnerCaseOutput(
            case_id=case.id,
            status="completed",
            candidate_sql="SELECT 1",
            trajectory=(),
        )
        output = RunnerOutput(schema_version="v1", cases=(output_case,))
        return RunnerExecution(
            output=output,
            raw_output=(output.model_dump_json() + "\n").encode(),
            raw_artifacts=(
                RawArtifact(
                    relative_path=f"dsh/{case.id}.session.jsonl",
                    content=b'{"type":"session","value":"redacted"}\n',
                ),
            ),
        )


def test_initialisation_creates_one_immutable_non_public_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = _write_final_suite_fixture(tmp_path / "data")
    custody_root = tmp_path / "custody"
    custody_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(custody_root))

    receipt = initialise_heldout_manifest(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    suite = load_heldout_suite(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )

    assert receipt.suite_id == "livesqlbench-base-lite-query-final-v1"
    assert receipt.manifest_sha256 == suite.manifest.manifest_sha256
    assert len(suite.cases) == 30
    assert set(case.instance_id for case in suite.cases).isdisjoint(_SMOKE_IDS)
    assert Counter(case.difficulty_tier for case in suite.cases) == {
        "Simple": 10,
        "Moderate": 10,
        "Challenging": 10,
    }
    databases = Counter(case.database_name for case in suite.cases)
    assert len(databases) == 18
    assert max(databases.values()) == 2
    for tier in ("Simple", "Moderate", "Challenging"):
        tier_databases = {
            case.database_name for case in suite.cases if case.difficulty_tier == tier
        }
        assert len(tier_databases) == 10
    assert all(case.instance_id not in json.dumps(suite.public_summary()) for case in suite.cases)
    assert not (tmp_path / "runs").exists()
    with pytest.raises(HeldOutError, match="already exists"):
        initialise_heldout_manifest(
            data_root,
            repository_root=Path.cwd(),
            candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
        )


def test_cli_initialisation_only_prints_suite_identity_and_manifest_hash(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = _write_final_suite_fixture(tmp_path / "data")
    custody_root = tmp_path / "custody"
    custody_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(custody_root))

    result = CliRunner().invoke(app, ["benchmark", "initialize-heldout", str(data_root)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {"suite_id", "manifest_sha256"}
    assert "simple_db00" not in result.stdout
    assert "selected_database" not in result.stdout


def test_initialisation_ignores_non_query_identifier_formats(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = _write_final_suite_fixture(tmp_path / "data")
    custody_root = tmp_path / "custody"
    custody_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(custody_root))
    public_file = data_root / "public" / "livesqlbench_data.jsonl"
    gt_file = data_root / "gt" / "official_gt.jsonl"
    with public_file.open("a", encoding="utf-8") as public_handle:
        public_handle.write(
            json.dumps({"instance_id": "management-case", "category": "Management"}) + "\n"
        )
    with gt_file.open("a", encoding="utf-8") as gt_handle:
        gt_handle.write(
            json.dumps({"instance_id": "management-case", "sol_sql": [], "external_knowledge": []})
            + "\n"
        )

    receipt = initialise_heldout_manifest(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )

    assert receipt.suite_id == "livesqlbench-base-lite-query-final-v1"


def test_initialisation_rejects_incomplete_selected_materials_before_manifest_write(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = _write_final_suite_fixture(tmp_path / "data")
    custody_root = tmp_path / "custody"
    custody_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(custody_root))
    shutil.rmtree(data_root / "dumps")

    with pytest.raises(HeldOutError, match="source data is invalid"):
        initialise_heldout_manifest(
            data_root,
            repository_root=Path.cwd(),
            candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
        )

    assert not any(custody_root.iterdir())


def test_heldout_loader_fails_closed_for_data_drift_and_custody_overlap(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = _write_final_suite_fixture(tmp_path / "data")
    custody_root = tmp_path / "custody"
    custody_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(custody_root))
    initialise_heldout_manifest(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    public_file = data_root / "public" / "livesqlbench_data.jsonl"
    public_file.write_text(public_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(HeldOutError, match="public data integrity"):
        load_heldout_suite(
            data_root,
            repository_root=Path.cwd(),
            candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
        )

    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(Path.cwd()))
    with pytest.raises(HeldOutError, match="custody root"):
        resolve_heldout_custody(
            repository_root=Path.cwd(),
            candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
            require_write=False,
        )


def test_heldout_run_keeps_case_evidence_in_custody_and_returns_only_aggregates(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_heldout_run.LiveSQLBenchFixture", StubFixture
    )
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_heldout_run.ReadOnlyQueryService",
        StubQueryService,
    )
    monkeypatch.setattr(
        "skill_evolve.benchmark.livesqlbench_heldout_run.DshRunner", StubDshRunner
    )
    data_root = _write_final_suite_fixture(tmp_path / "data")
    custody_root = tmp_path / "custody"
    custody_root.mkdir()
    session_root = custody_root / "dsh-session-logs"
    session_root.mkdir()
    monkeypatch.setenv(HELDOUT_ROOT_ENVIRONMENT_VARIABLE, str(custody_root))
    adapter = LiveSQLBenchBaseLiteHeldOutAdapter()
    initialise_heldout_manifest(
        data_root,
        repository_root=Path.cwd(),
        candidate_store_root=Path.cwd() / ".local" / "skill-evolve",
    )
    suite = adapter.load_suite(data_root)

    with pytest.raises(BenchmarkAdapterError, match="session_log_root"):
        adapter.run_dsh(
            suite,
            DshConfig(version="0.1.1-rc.2", profile="headless", runner="dsh"),
            None,
            ModelConfig(provider="test", name="stub"),
            tmp_path / "ordinary-runs",
        )
    assert not (custody_root / "runs").exists()

    result = adapter.run_dsh(
        suite,
        DshConfig(
            version="0.1.1-rc.2",
            profile="headless",
            runner="dsh",
            session_log_root=str(session_root),
        ),
        None,
        ModelConfig(provider="test", name="stub"),
        tmp_path / "ordinary-runs",
    )

    assert result.public_result is not None
    serialised = json.dumps(result.public_result)
    assert result.public_result["status"] == "completed"
    assert result.public_result["case_count"] == 30
    assert result.public_result["subject_binding"] is None
    assert result.public_result["macro_average"] == 1.0
    assert "simple_db00" not in serialised
    assert "candidate_sql" not in serialised
    assert str(custody_root) not in serialised
    assert not (tmp_path / "ordinary-runs").exists()
    custody_runs = tuple((custody_root / "runs").iterdir())
    assert len(custody_runs) == 1
    custody_run = custody_runs[0]
    assert (custody_run / "raw" / "runner-output.json").is_file()
    assert not (custody_run / "inputs" / "subject-SKILL.md").exists()
    with pytest.raises(BenchmarkAdapterError, match="custody-only"):
        adapter.evaluate(suite, RunnerOutput(schema_version="v1", cases=()), "postgres:test")

    dsh_manifest = write_experiment(tmp_path / "dsh-manifest")
    dsh_manifest.write_text(
        dsh_manifest.read_text(encoding="utf-8").replace(
            "runner: fake\n    fake_output: fake-results/results.json\n",
            "runner: dsh\n    session_log_root: " + str(session_root) + "\n",
        ),
        encoding="utf-8",
    )
    cli_result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "run",
            "livesqlbench-base-lite-query-final-v1",
            str(data_root),
            str(dsh_manifest),
        ],
    )

    assert cli_result.exit_code == 0
    assert json.loads(cli_result.stdout)["macro_average"] == 1.0
    assert "simple_db00" not in cli_result.stdout
    assert "candidate_sql" not in cli_result.stdout
    assert str(custody_root) not in cli_result.stdout


def _write_final_suite_fixture(tmp_path: Path, database_count: int = 18) -> Path:
    data_root = tmp_path / "base-lite"
    public_root = data_root / "public"
    gt_root = data_root / "gt"
    dump_root = data_root / "dumps" / "base-lite" / "livesqlbench-base-lite-dumps"
    public_records: list[dict[str, object]] = []
    gt_records: list[dict[str, object]] = []
    tiers = ("Simple", "Moderate", "Challenging")
    databases = tuple(f"db{index:02d}" for index in range(database_count))
    for database in databases:
        public_directory = public_root / database
        public_directory.mkdir(parents=True)
        (public_directory / f"{database}_schema.txt").write_text("schema", encoding="utf-8")
        (public_directory / f"{database}_kb.jsonl").write_text("{}\n", encoding="utf-8")
        (public_directory / f"{database}_column_meaning_base.json").write_text(
            "{}\n", encoding="utf-8"
        )
        dump_directory = dump_root / f"{database}_template"
        dump_directory.mkdir(parents=True)
        (dump_directory / "records.sql").write_text(
            "CREATE TABLE records (id integer);", encoding="utf-8"
        )
        (dump_root / f"{database}_table_orders.txt").write_text("records\n", encoding="utf-8")
    for tier_index, tier in enumerate(tiers):
        for database_index, database in enumerate(databases):
            instance_id = f"{tier.lower()}_{database}"
            public_records.append(
                {
                    "instance_id": instance_id,
                    "category": "Query",
                    "selected_database": database,
                    "query": f"Find values for {database}.",
                    "conditions": {"order": (tier_index + database_index) % 2 == 0},
                    "difficulty_tier": tier,
                }
            )
            gt_records.append(
                {
                    "instance_id": instance_id,
                    "sol_sql": ["SELECT expected FROM private_gold"],
                    "external_knowledge": [1],
                }
            )
    (public_root / "livesqlbench_data.jsonl").write_text(
        "\n".join(json.dumps(record) for record in public_records) + "\n", encoding="utf-8"
    )
    gt_root.mkdir(parents=True)
    (gt_root / "official_gt.jsonl").write_text(
        "\n".join(json.dumps(record) for record in gt_records) + "\n", encoding="utf-8"
    )
    return data_root
