"""Private-data-safe loader and Query evaluator for LiveSQLBench Base-Lite."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from skill_evolve.benchmark.contracts import JsonObject, JsonValue
from skill_evolve.evaluator.core import CaseEvaluation, QueryExecutor, Verdict
from skill_evolve.runner.contracts import RunnerCaseOutput
from skill_evolve.sql.postgres import QueryResult

APPROVED_BASE_LITE_QUERY_IDS: tuple[str, ...] = (
    "alien_2",
    "polar_10",
    "gaming_1",
    "archeology_5",
    "cross_db_8",
)
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ROUND_PATTERN = re.compile(r"ROUND\s*\(", re.IGNORECASE)
_DISTINCT_PATTERN = re.compile(r"\bDISTINCT\b(?![^()]*\bON\b)", re.IGNORECASE)


class LiveSQLBenchDataError(ValueError):
    """LiveSQLBench inputs are incomplete, inconsistent, or unsafe to use."""


@dataclass(frozen=True)
class LiveSQLBenchCase:
    """One Query task, with private gold retained only in process memory."""

    instance_id: str
    database_name: str
    prompt: str
    schema_file: Path
    knowledge_file: Path
    column_meaning_file: Path
    gold_sql: str
    required_knowledge_ids: tuple[str, ...]
    order_matters: bool
    difficulty_tier: str

    @property
    def public_context_files(self) -> tuple[Path, Path, Path]:
        """Return the only source files allowed into an agent task workspace."""

        return (self.schema_file, self.knowledge_file, self.column_meaning_file)


@dataclass(frozen=True)
class LiveSQLBenchSuite:
    """Resolved local Base-Lite materials for the fixed five-task smoke suite."""

    data_root: Path
    public_data_file: Path
    gt_file: Path
    dump_root: Path
    cases: tuple[LiveSQLBenchCase, ...]

    @property
    def adapter_id(self) -> str:
        """Return the fixed, registry-owned adapter identity."""

        return "livesqlbench-base-lite"

    @property
    def case_ids(self) -> tuple[str, ...]:
        """Return the fixed suite order used to bind runner output safely."""

        return tuple(case.instance_id for case in self.cases)

    def public_summary(self) -> JsonObject:
        """Return machine-readable metadata that intentionally excludes gold SQL."""

        source_digests: dict[str, JsonValue] = {
            path: digest for path, digest in self.source_digests().items()
        }
        return {
            "benchmark": "livesqlbench-base-lite",
            "case_count": len(self.cases),
            "cases": [
                {
                    "instance_id": case.instance_id,
                    "database": case.database_name,
                    "difficulty_tier": case.difficulty_tier,
                    "order_matters": case.order_matters,
                    "context_files": [
                        path.relative_to(self.data_root).as_posix()
                        for path in case.public_context_files
                    ],
                }
                for case in self.cases
            ],
            "source_digests": source_digests,
        }

    def source_digests(self) -> dict[str, str]:
        """Hash all required local sources without serialising their contents."""

        paths: list[Path] = [self.public_data_file, self.gt_file]
        for case in self.cases:
            paths.extend(case.public_context_files)
            dump_directory = _path_within_root(
                self.data_root, self.dump_root / f"{case.database_name}_template"
            )
            paths.extend(
                sorted(
                    _path_within_root(self.data_root, path)
                    for path in dump_directory.glob("*.sql")
                )
            )
            paths.append(
                _path_within_root(
                    self.data_root,
                    self.dump_root / f"{case.database_name}_table_orders.txt",
                )
            )
        return {
            path.relative_to(self.data_root).as_posix(): _sha256_file(path)
            for path in sorted(set(paths))
        }


def load_base_lite_suite(data_root: Path) -> LiveSQLBenchSuite:
    """Load and strictly validate the approved Base-Lite Query-only smoke suite."""

    try:
        root = data_root.resolve(strict=True)
    except OSError as error:
        raise LiveSQLBenchDataError(f"could not resolve data root {data_root}: {error}") from error
    public_data_file = _path_within_root(root, root / "public" / "livesqlbench_data.jsonl")
    gt_file = _single_jsonl_file(_path_within_root(root, root / "gt"), "GT", root)
    dump_root = _path_within_root(
        root, root / "dumps" / "base-lite" / "livesqlbench-base-lite-dumps"
    )
    _require_file(public_data_file, "public data JSONL")
    _require_directory(dump_root, "extracted dump root")

    public_records = _records_by_id(_read_jsonl(public_data_file), "public data")
    gt_records = _records_by_id(_read_jsonl(gt_file), "GT data")
    _require_matching_case_ids(public_records, gt_records)
    cases = tuple(
        _build_case(root, instance_id, public_records, gt_records, dump_root)
        for instance_id in APPROVED_BASE_LITE_QUERY_IDS
    )
    return LiveSQLBenchSuite(
        data_root=root,
        public_data_file=public_data_file,
        gt_file=gt_file,
        dump_root=dump_root,
        cases=cases,
    )


def evaluate_query_case(
    case: LiveSQLBenchCase,
    runner_case: RunnerCaseOutput,
    executor: QueryExecutor,
) -> CaseEvaluation:
    """Evaluate one Query task with the upstream Base-Lite Soft-EX semantics.

    The result is an in-memory object and contains gold SQL for attribution compatibility.
    Callers persisting or displaying it must use :func:`public_evaluation_dict`.
    """

    usage = runner_case.usage.model_dump(mode="json")
    if runner_case.status == "runner_error":
        return _evaluation(
            case, runner_case, Verdict.RUNNER_ERROR, "runner returned an error", usage
        )
    if runner_case.candidate_sql is None or not runner_case.candidate_sql.strip():
        return _evaluation(
            case,
            runner_case,
            Verdict.NO_SQL_GENERATED,
            "runner completed without candidate SQL",
            usage,
        )

    candidate_query = _normalise_query_for_soft_ex(runner_case.candidate_sql)
    expected_query = _normalise_query_for_soft_ex(case.gold_sql)
    expected = executor.execute_read_only(expected_query)
    if not expected.ok:
        return _evaluation(
            case,
            runner_case,
            Verdict.EVALUATOR_INVALID,
            "gold query could not be evaluated",
            usage,
        )
    actual = executor.execute_read_only(candidate_query)
    if not actual.ok:
        verdict = Verdict.UNSAFE_SQL if actual.unsafe else Verdict.SQL_EXECUTION_ERROR
        return _evaluation(
            case,
            runner_case,
            verdict,
            actual.error or "candidate query could not be evaluated",
            usage,
        )
    if not _soft_ex_equivalent(actual.result, expected.result, case.order_matters):
        return _evaluation(
            case,
            runner_case,
            Verdict.WRONG_RESULT,
            "candidate result is not equivalent under LiveSQLBench Soft-EX",
            usage,
        )
    return _evaluation(case, runner_case, Verdict.PASS, None, usage)


def public_evaluation_dict(evaluation: CaseEvaluation) -> dict[str, Any]:
    """Produce a serialisable verdict without gold SQL or gold execution results."""

    return {
        "case_id": evaluation.case_id,
        "slice": evaluation.slice,
        "verdict": evaluation.verdict.value,
        "candidate_sql": evaluation.candidate_sql,
        "detail": evaluation.detail,
        "usage": evaluation.usage,
    }


def public_summary(evaluations: Sequence[CaseEvaluation]) -> dict[str, int]:
    """Count verdicts without exposing case inputs or evaluator material."""

    return dict(sorted(Counter(evaluation.verdict.value for evaluation in evaluations).items()))


def _build_case(
    root: Path,
    instance_id: str,
    public_records: Mapping[str, Mapping[str, Any]],
    gt_records: Mapping[str, Mapping[str, Any]],
    dump_root: Path,
) -> LiveSQLBenchCase:
    public_record = public_records.get(instance_id)
    gt_record = gt_records.get(instance_id)
    if public_record is None:
        raise LiveSQLBenchDataError(f"public data is missing approved instance {instance_id!r}")
    if gt_record is None:
        raise LiveSQLBenchDataError(f"GT data is missing approved instance {instance_id!r}")
    if public_record.get("category") != "Query":
        raise LiveSQLBenchDataError(f"approved instance {instance_id!r} is not a Query task")
    database_name = _required_identifier(public_record, "selected_database", instance_id)
    prompt = _required_string(public_record, "query", instance_id)
    gold_sql = _single_sql(gt_record.get("sol_sql"), instance_id)
    knowledge_ids = _string_sequence(
        gt_record.get("external_knowledge"), "external_knowledge", instance_id
    )
    conditions = public_record.get("conditions")
    if not isinstance(conditions, Mapping) or not isinstance(conditions.get("order"), bool):
        raise LiveSQLBenchDataError(f"public conditions are invalid for {instance_id!r}")
    difficulty = _required_string(public_record, "difficulty_tier", instance_id)
    public_directory = _path_within_root(root, root / "public" / database_name)
    schema_file = _path_within_root(root, public_directory / f"{database_name}_schema.txt")
    knowledge_file = _path_within_root(root, public_directory / f"{database_name}_kb.jsonl")
    column_meaning_file = _path_within_root(
        root, public_directory / f"{database_name}_column_meaning_base.json"
    )
    table_order_file = _path_within_root(
        root, dump_root / f"{database_name}_table_orders.txt"
    )
    for file_path, label in (
        (schema_file, "schema"),
        (knowledge_file, "knowledge base"),
        (column_meaning_file, "column meanings"),
        (table_order_file, "table order"),
    ):
        _require_file(file_path, f"{label} for {instance_id}")
    dump_directory = _path_within_root(root, dump_root / f"{database_name}_template")
    _require_directory(dump_directory, f"dump directory for {instance_id}")
    dump_files = tuple(_path_within_root(root, path) for path in dump_directory.glob("*.sql"))
    if not dump_files:
        raise LiveSQLBenchDataError(f"dump directory has no SQL files for {instance_id!r}")
    return LiveSQLBenchCase(
        instance_id=instance_id,
        database_name=database_name,
        prompt=prompt,
        schema_file=schema_file,
        knowledge_file=knowledge_file,
        column_meaning_file=column_meaning_file,
        gold_sql=gold_sql,
        required_knowledge_ids=knowledge_ids,
        order_matters=conditions["order"],
        difficulty_tier=difficulty,
    )


def _read_jsonl(path: Path) -> tuple[Mapping[str, Any], ...]:
    records: list[Mapping[str, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise LiveSQLBenchDataError(f"{path.name}:{line_number} must be a JSON object")
            records.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise LiveSQLBenchDataError(f"could not read {path}: {error}") from error
    if not records:
        raise LiveSQLBenchDataError(f"{path} contains no records")
    return tuple(records)


def _records_by_id(
    records: Sequence[Mapping[str, Any]], source_name: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        instance_id = record.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise LiveSQLBenchDataError(
                f"{source_name} contains a record without a valid instance_id"
            )
        if instance_id in result:
            raise LiveSQLBenchDataError(
                f"{source_name} contains duplicate instance_id {instance_id!r}"
            )
        result[instance_id] = record
    return result


def _single_jsonl_file(directory: Path, label: str, root: Path) -> Path:
    _require_directory(directory, f"{label} directory")
    candidates = sorted(
        _path_within_root(root, path) for path in directory.glob("*.jsonl") if path.is_file()
    )
    if len(candidates) != 1:
        raise LiveSQLBenchDataError(
            f"{label} directory must contain exactly one JSONL file, found {len(candidates)}"
        )
    return candidates[0]


def _require_matching_case_ids(
    public_records: Mapping[str, Mapping[str, Any]], gt_records: Mapping[str, Mapping[str, Any]]
) -> None:
    public_ids = set(public_records)
    gt_ids = set(gt_records)
    if public_ids == gt_ids:
        return
    raise LiveSQLBenchDataError("public and GT case IDs must match exactly")


def _path_within_root(root: Path, path: Path) -> Path:
    """Reject path traversal and symlink escapes in untrusted data roots."""

    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise LiveSQLBenchDataError(f"could not resolve required path {path}: {error}") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise LiveSQLBenchDataError(f"required path escapes data root: {path}") from error
    return resolved


def _required_identifier(record: Mapping[str, Any], field: str, instance_id: str) -> str:
    value = _required_string(record, field, instance_id)
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise LiveSQLBenchDataError(f"{field} is not a safe identifier for {instance_id!r}")
    return value


def _required_string(record: Mapping[str, Any], field: str, instance_id: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LiveSQLBenchDataError(f"{field} is missing or invalid for {instance_id!r}")
    return value


def _single_sql(value: Any, instance_id: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    if (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], str)
        and value[0].strip()
    ):
        return value[0]
    raise LiveSQLBenchDataError(
        f"GT sol_sql must contain exactly one SQL string for {instance_id!r}"
    )


def _string_sequence(value: Any, field: str, instance_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, (str, int)) for item in value):
        raise LiveSQLBenchDataError(
            f"GT {field} must be a string/integer sequence for {instance_id!r}"
        )
    return tuple(str(item) for item in value)


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise LiveSQLBenchDataError(f"missing {label}: {path}")


def _require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise LiveSQLBenchDataError(f"missing {label}: {path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_query_for_soft_ex(query: str) -> str:
    without_comments = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    without_comments = re.sub(r"--.*?(\r\n|\r|\n)", r"\1", without_comments)
    without_distinct = _DISTINCT_PATTERN.sub("", without_comments)
    return _remove_round_functions(without_distinct).strip()


def _remove_round_functions(query: str) -> str:
    result = query
    while match := _ROUND_PATTERN.search(result):
        opening_index = match.end() - 1
        closing_index = _matching_parenthesis(result, opening_index)
        if closing_index is None:
            break
        first_argument_end = _first_argument_end(result, opening_index + 1, closing_index)
        first_argument = result[opening_index + 1 : first_argument_end].strip()
        result = result[: match.start()] + first_argument + result[closing_index + 1 :]
    return result


def _matching_parenthesis(text: str, opening_index: int) -> int | None:
    depth = 0
    for index in range(opening_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _first_argument_end(text: str, start_index: int, closing_index: int) -> int:
    depth = 0
    for index in range(start_index, closing_index):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
        elif text[index] == "," and depth == 0:
            return index
    return closing_index


def _soft_ex_equivalent(
    actual: QueryResult | None, expected: QueryResult | None, order_matters: bool
) -> bool:
    if actual is None or expected is None:
        return False
    actual_rows = tuple(_preprocess_row(row) for row in actual.rows)
    expected_rows = tuple(_preprocess_row(row) for row in expected.rows)
    return actual_rows == expected_rows if order_matters else set(actual_rows) == set(expected_rows)


def _preprocess_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(_preprocess_value(value) for value in row)


def _preprocess_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Decimal):
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if isinstance(value, float):
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if isinstance(value, (dict, list)):
        return json.dumps(_preprocess_nested(value), sort_keys=True, ensure_ascii=False)
    if isinstance(value, tuple):
        return tuple(_preprocess_value(item) for item in value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _preprocess_nested(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if isinstance(value, float):
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, list):
        return [_preprocess_nested(item) for item in value]
    if isinstance(value, tuple):
        return [_preprocess_nested(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _preprocess_nested(item) for key, item in value.items()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _evaluation(
    case: LiveSQLBenchCase,
    runner_case: RunnerCaseOutput,
    verdict: Verdict,
    detail: str | None,
    usage: dict[str, int | float | None],
) -> CaseEvaluation:
    return CaseEvaluation(
        case_id=case.instance_id,
        slice="fast",
        prompt=case.prompt,
        verdict=verdict,
        candidate_sql=runner_case.candidate_sql,
        expected_query=case.gold_sql,
        detail=detail,
        expected_result=None,
        actual_result=None,
        usage=usage,
    )
