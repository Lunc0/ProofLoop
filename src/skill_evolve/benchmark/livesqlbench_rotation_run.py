"""Neutral rolling execution and Smoke-gate evidence for LiveSQLBench."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from skill_evolve.benchmark.livesqlbench import (
    LiveSQLBenchCase,
    LiveSQLBenchSuite,
    public_evaluation_dict,
)
from skill_evolve.benchmark.livesqlbench_rotation import (
    ROTATION_TIERS,
    RotationCustody,
    RotationLiveSQLBenchSuite,
    StrictRotationModel,
)
from skill_evolve.benchmark.livesqlbench_run import (
    _EVALUATOR_VERSION,
    _public_trajectory,
    _run_case,
    _write_runner_artifacts,
)
from skill_evolve.config.models import DshConfig, ModelConfig, OptimizerConfig
from skill_evolve.evaluator.core import CaseEvaluation, Verdict
from skill_evolve.optimizer.client import OptimizerClient
from skill_evolve.optimizer.contracts import OptimizationResult
from skill_evolve.optimizer.service import advise_after_evolution
from skill_evolve.registry.store import RunDirectory, RunStore
from skill_evolve.runner.contracts import (
    RawArtifact,
    RunnerCaseOutput,
    RunnerError,
    RunnerExecution,
    RunnerOutput,
)
from skill_evolve.sql.livesqlbench import LiveSQLBenchFixtureError
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.agent_input import build_evolution_agent_input
from skill_evolve.trajectory.compressor import (
    TrajectoryCompressionError,
    canonical_json_bytes,
    compress_session_jsonl,
)
from skill_evolve.trajectory.contracts import CompressedTrajectory

RollingRunStatus = Literal["passed", "task_failure", "runner_invalid", "evaluator_invalid"]


class RotationRunError(RuntimeError):
    """A rolling run cannot safely start or satisfy the Smoke gate."""


class SmokeGateReceipt(StrictRotationModel):
    """Private proof that one immutable subject passed one Smoke manifest."""

    schema_version: Literal["v1"]
    subject: dict[str, str]
    smoke_manifest_sha256: str
    smoke_environment_fingerprint: str
    gate_sha256: str


@dataclass(frozen=True)
class RotationRunOutcome:
    """Public-safe result metadata plus the ordinary redacted evidence directory."""

    run: RunDirectory
    status: RollingRunStatus
    environment_fingerprint: str
    comparison_key: str | None
    evaluations: tuple[CaseEvaluation, ...]
    public_result: dict[str, Any]

    @property
    def exit_code(self) -> int:
        return 2 if self.status in {"runner_invalid", "evaluator_invalid"} else 0


def run_rotation_dsh_suite(
    suite: RotationLiveSQLBenchSuite,
    dsh_config: DshConfig,
    profile_patch: Path | None,
    model: ModelConfig,
    runs_root: Path,
    subject: RunSubject | None,
    optimizer: OptimizerConfig | None = None,
    *,
    optimizer_client: OptimizerClient | None = None,
) -> RotationRunOutcome:
    """Run one frozen rolling manifest without interpreting failures as Skill actions."""

    if dsh_config.runner != "dsh":
        raise RotationRunError("rolling LiveSQLBench runs require dsh.runner: dsh")
    if subject is None:
        raise RotationRunError("rolling LiveSQLBench runs require a RunSubject")
    if suite.kind == "evolution" and not has_successful_smoke_gate(suite.custody, subject):
        raise RotationRunError("Evolution requires a successful Smoke gate for this RunSubject")

    active_optimizer = optimizer if optimizer is not None else OptimizerConfig()
    environment_fingerprint = _environment_fingerprint(suite, dsh_config, model, subject)
    run = RunStore(runs_root).create()
    _write_initial_evidence(
        run, suite, dsh_config, model, active_optimizer, environment_fingerprint, subject
    )
    evaluations: list[CaseEvaluation] = []
    runner_cases: list[RunnerCaseOutput] = []
    trajectories: list[dict[str, Any]] = []
    agent_cases: list[tuple[LiveSQLBenchCase, CompressedTrajectory]] = []
    omitted_agent_cases: list[dict[str, str]] = []
    stderr_parts: list[str] = []
    infrastructure_status: RollingRunStatus | None = None

    for case in suite.cases:
        try:
            runner_case, execution, evaluation = _run_case(
                case,
                cast(LiveSQLBenchSuite, suite),
                dsh_config,
                profile_patch,
                model,
                run,
                environment_fingerprint,
                subject,
            )
        except LiveSQLBenchFixtureError:
            infrastructure_status = "evaluator_invalid"
            break
        except RunnerError:
            infrastructure_status = "runner_invalid"
            break
        evaluations.append(evaluation)
        runner_cases.append(runner_case)
        trajectories.append(_public_trajectory(runner_case))
        _write_runner_artifacts(run, case, execution)
        if suite.kind == "evolution" and not evaluation.passed:
            compressed, omission = _compress_evolution_case(
                runner_case, execution, evaluation, dsh_config.timeout_ms
            )
            if compressed is None:
                assert omission is not None
                omitted_agent_cases.append(omission)
            else:
                agent_cases.append((case, compressed))
        if execution.stderr:
            stderr_parts.append(f"[{case.instance_id}]\n{execution.stderr}")

    if runner_cases:
        runner_output = RunnerOutput(schema_version="v1", cases=tuple(runner_cases))
        runner_output_bytes = (runner_output.model_dump_json(indent=2) + "\n").encode()
        run.write_bytes("raw/runner-output.json", runner_output_bytes)
    if stderr_parts:
        run.write_text("raw/runner.stderr.log", "\n\n".join(stderr_parts))
    run.write_json("trajectory.json", {"cases": trajectories})
    if suite.kind == "evolution":
        _write_evolution_agent_input(
            run,
            suite,
            environment_fingerprint,
            subject,
            tuple(agent_cases),
            tuple(omitted_agent_cases),
        )
    status = _status_for(evaluations, infrastructure_status)
    comparison_key = (
        _comparison_key(suite, dsh_config, model) if suite.kind == "evolution" else None
    )
    optimizer_result: OptimizationResult | None = None
    if suite.kind == "evolution":
        optimizer_result = advise_after_evolution(
            run,
            subject,
            active_optimizer,
            run_status=status,
            client=optimizer_client,
        )
    outcome = _write_final_evidence(
        run,
        suite,
        status,
        environment_fingerprint,
        comparison_key,
        tuple(evaluations),
        subject,
        optimizer_result,
    )
    if suite.kind == "smoke" and status == "passed":
        _write_smoke_gate(
            suite.custody,
            subject,
            suite.manifest.manifest_sha256,
            environment_fingerprint,
        )
    return outcome


def _compress_evolution_case(
    runner_case: RunnerCaseOutput,
    execution: RunnerExecution,
    evaluation: CaseEvaluation,
    timeout_ms: int,
) -> tuple[CompressedTrajectory | None, dict[str, str] | None]:
    """Compress only runner-owned, already-redacted session evidence."""

    session = _session_artifact_for_case(
        execution.raw_artifacts, runner_case.case_id
    )
    if session is None:
        return None, {"case_id": runner_case.case_id, "reason": "session_evidence_missing"}
    runner_outcome = "timeout" if _is_timeout_case(runner_case) else runner_case.status
    try:
        compressed = compress_session_jsonl(
            session.content,
            runner_case,
            evaluation,
            runner_outcome=runner_outcome,
            deadline_ms=timeout_ms if runner_outcome == "timeout" else None,
        )
    except TrajectoryCompressionError:
        return None, {"case_id": runner_case.case_id, "reason": "session_unavailable"}
    if compressed.trace.trace_completeness == "partial_insufficient":
        return None, {"case_id": runner_case.case_id, "reason": "partial_insufficient"}
    return compressed, None


def _session_artifact_for_case(
    artifacts: tuple[RawArtifact, ...], case_id: str
) -> RawArtifact | None:
    target = f"dsh/{case_id}.session.jsonl"
    matches = tuple(artifact for artifact in artifacts if artifact.relative_path == target)
    if len(matches) == 1:
        return matches[0]
    return None


def _is_timeout_case(runner_case: RunnerCaseOutput) -> bool:
    execution = runner_case.agent_execution
    return (
        execution is not None
        and execution.error is not None
        and "timed out" in execution.error.lower()
    )


def _write_evolution_agent_input(
    run: RunDirectory,
    suite: RotationLiveSQLBenchSuite,
    environment_fingerprint: str,
    subject: RunSubject,
    cases: tuple[tuple[LiveSQLBenchCase, CompressedTrajectory], ...],
    omitted_cases: tuple[dict[str, str], ...],
) -> None:
    """Persist agent-visible development evidence without changing benchmark outcomes."""

    try:
        agent_input = build_evolution_agent_input(
            suite.manifest.manifest_sha256,
            environment_fingerprint,
            subject,
            cases,
            omitted_cases,
        )
    except TrajectoryCompressionError:
        run.write_json(
            "agent-input/manifest.json",
            {
                "schema_version": "evolution-coding-agent-input-v3",
                "exposed_for_optimization": True,
                "quality": "unavailable",
                "included_case_count": 0,
                "omitted_case_count": len(omitted_cases),
            },
        )
        return
    payload = canonical_json_bytes(agent_input.model_dump(mode="json"))
    run.write_bytes("agent-input/trajectory.json", payload + b"\n")
    run.write_json(
        "agent-input/manifest.json",
        {
            "schema_version": "evolution-coding-agent-input-v3",
            "exposed_for_optimization": True,
            "manifest_sha256": suite.manifest.manifest_sha256,
            "environment_fingerprint": environment_fingerprint,
            "projection_rule_version": agent_input.projection_rule_version,
            "agent_input_sha256": hashlib.sha256(payload).hexdigest(),
            "included_case_count": len(agent_input.trajectories),
            "omitted_case_count": len(omitted_cases),
        },
    )


def has_successful_smoke_gate(custody: RotationCustody, subject: RunSubject) -> bool:
    """Return whether custody contains a verified 5/5 Smoke receipt for the subject."""

    root = custody.gate_root
    if not root.exists():
        return False
    if root.is_symlink() or not root.is_dir():
        raise RotationRunError("Smoke gate custody is invalid")
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise RotationRunError("Smoke gate custody is invalid")
        receipt = _load_gate(path)
        if receipt.subject == subject.public_binding():
            return True
    return False


def compare_evolution_results(
    first: RotationRunOutcome, second: RotationRunOutcome
) -> Literal["comparable", "incomparable"]:
    """Classify only whether two Evolution results share the same comparison key."""

    if (
        first.comparison_key is None
        or second.comparison_key is None
        or first.comparison_key != second.comparison_key
    ):
        return "incomparable"
    return "comparable"


def _status_for(
    evaluations: list[CaseEvaluation], infrastructure_status: RollingRunStatus | None
) -> RollingRunStatus:
    if infrastructure_status is not None:
        return infrastructure_status
    verdicts = {evaluation.verdict for evaluation in evaluations}
    if Verdict.EVALUATOR_INVALID in verdicts:
        return "evaluator_invalid"
    if Verdict.RUNNER_ERROR in verdicts:
        return "runner_invalid"
    if any(not evaluation.passed for evaluation in evaluations):
        return "task_failure"
    return "passed"


def _write_initial_evidence(
    run: RunDirectory,
    suite: RotationLiveSQLBenchSuite,
    dsh_config: DshConfig,
    model: ModelConfig,
    optimizer: OptimizerConfig,
    environment_fingerprint: str,
    subject: RunSubject,
) -> None:
    binding = subject.public_binding()
    run.write_json(
        "environment.json",
        {
            "environment_fingerprint": environment_fingerprint,
            "benchmark": suite.public_summary(),
            "dsh": dsh_config.model_dump(mode="json"),
            "model": model.model_dump(mode="json"),
            "optimizer": optimizer.model_dump(mode="json"),
            "evaluator": {"version": _EVALUATOR_VERSION},
            "subject": binding,
        },
    )
    run.write_json(
        "run.json",
        {
            "schema_version": "livesqlbench-rotation-v1",
            "run_id": run.run_id,
            "adapter_id": suite.adapter_id,
            "manifest_id": suite.manifest.manifest_id,
            "manifest_sha256": suite.manifest.manifest_sha256,
            "phase": "snapshotted",
            "subject": binding,
        },
    )
    run.materialise_subject(subject)


def _write_final_evidence(
    run: RunDirectory,
    suite: RotationLiveSQLBenchSuite,
    status: RollingRunStatus,
    environment_fingerprint: str,
    comparison_key: str | None,
    evaluations: tuple[CaseEvaluation, ...],
    subject: RunSubject,
    optimizer_result: OptimizationResult | None,
) -> RotationRunOutcome:
    public_evaluations = [public_evaluation_dict(item) for item in evaluations]
    tier_metrics = _tier_metrics(suite, evaluations)
    result: dict[str, Any] = {
        "schema_version": "livesqlbench-rotation-v1",
        "run_id": run.run_id,
        "adapter_id": suite.adapter_id,
        "manifest_id": suite.manifest.manifest_id,
        "manifest_sha256": suite.manifest.manifest_sha256,
        "environment_fingerprint": environment_fingerprint,
        "status": status,
        "subject": subject.public_binding(),
        "summary": {
            "verdicts": dict(sorted(Counter(item.verdict.value for item in evaluations).items()))
        },
        "tiers": tier_metrics,
        "macro_average": _macro_average(tier_metrics),
        "cost_usd": _total_cost(evaluations, len(suite.cases)),
        "cases": public_evaluations,
    }
    if comparison_key is not None:
        result["comparison_key"] = comparison_key
    if optimizer_result is not None:
        receipt = optimizer_result.receipt
        result["optimizer"] = {
            "status": receipt.status,
            "advice_path": optimizer_result.advice_path,
            "provider": receipt.provider,
            "model": receipt.model,
            "prompt_version": receipt.prompt_version,
        }
    run.write_json("evaluation.json", {"cases": public_evaluations})
    run.write_json("result.json", result)
    run.write_text("report.md", _neutral_report(run.run_id, status, tier_metrics, comparison_key))
    return RotationRunOutcome(
        run=run,
        status=status,
        environment_fingerprint=environment_fingerprint,
        comparison_key=comparison_key,
        evaluations=evaluations,
        public_result=result,
    )


def _tier_metrics(
    suite: RotationLiveSQLBenchSuite, evaluations: tuple[CaseEvaluation, ...]
) -> dict[str, dict[str, int | float | None]]:
    tier_by_case = {case.instance_id: case.difficulty_tier for case in suite.cases}
    grouped: dict[str, list[CaseEvaluation]] = {tier: [] for tier in ROTATION_TIERS}
    for evaluation in evaluations:
        tier = tier_by_case.get(evaluation.case_id)
        if tier is None:
            raise RotationRunError("rolling evaluator produced an unexpected case")
        grouped[tier].append(evaluation)
    expected = Counter(case.difficulty_tier for case in suite.cases)
    return {
        tier: {
            "case_count": expected[tier],
            "evaluated_count": len(grouped[tier]),
            "passed_count": sum(item.passed for item in grouped[tier]),
            "pass_rate": (
                sum(item.passed for item in grouped[tier]) / expected[tier]
                if len(grouped[tier]) == expected[tier]
                else None
            ),
        }
        for tier in ROTATION_TIERS
    }


def _macro_average(tier_metrics: dict[str, dict[str, int | float | None]]) -> float | None:
    rates = [tier_metrics[tier]["pass_rate"] for tier in ROTATION_TIERS]
    known_rates: list[float] = []
    for rate in rates:
        if not isinstance(rate, (int, float)):
            return None
        known_rates.append(float(rate))
    return sum(known_rates) / len(known_rates)


def _total_cost(evaluations: tuple[CaseEvaluation, ...], expected_count: int) -> float | None:
    if len(evaluations) != expected_count:
        return None
    values = [item.usage.get("cost_usd") for item in evaluations]
    known_costs: list[float] = []
    for value in values:
        if not isinstance(value, (int, float)):
            return None
        known_costs.append(float(value))
    return sum(known_costs)


def _environment_fingerprint(
    suite: RotationLiveSQLBenchSuite,
    dsh_config: DshConfig,
    model: ModelConfig,
    subject: RunSubject,
) -> str:
    payload = {
        "adapter_id": suite.adapter_id,
        "manifest_sha256": suite.manifest.manifest_sha256,
        "source_digests": suite.source_digests(),
        "dsh": dsh_config.model_dump(mode="json"),
        "model": model.model_dump(mode="json"),
        "evaluator": _EVALUATOR_VERSION,
        "subject": subject.public_binding(),
    }
    return _hash_json(payload)


def _comparison_key(
    suite: RotationLiveSQLBenchSuite, dsh_config: DshConfig, model: ModelConfig
) -> str:
    return _hash_json(
        {
            "adapter_id": suite.adapter_id,
            "manifest_sha256": suite.manifest.manifest_sha256,
            "source_digests": suite.source_digests(),
            "dsh": dsh_config.model_dump(mode="json"),
            "model": model.model_dump(mode="json"),
            "evaluator": _EVALUATOR_VERSION,
        }
    )


def _neutral_report(
    run_id: str,
    status: RollingRunStatus,
    tier_metrics: dict[str, dict[str, int | float | None]],
    comparison_key: str | None,
) -> str:
    lines = [f"# Rolling evaluation {run_id}", "", f"- Status: `{status}`", "", "## Tiers", ""]
    for tier in ROTATION_TIERS:
        metrics = tier_metrics[tier]
        lines.append(
            f"- `{tier}`: {metrics['passed_count']}/{metrics['case_count']} passed; "
            f"evaluated {metrics['evaluated_count']}"
        )
    if comparison_key is not None:
        lines.extend(["", f"- Comparison key: `{comparison_key}`"])
    lines.append("")
    return "\n".join(lines)


def _write_smoke_gate(
    custody: RotationCustody,
    subject: RunSubject,
    smoke_manifest_sha256: str,
    environment_fingerprint: str,
) -> None:
    try:
        custody.gate_root.mkdir(exist_ok=True)
        if custody.gate_root.is_symlink() or not custody.gate_root.is_dir():
            raise RotationRunError("Smoke gate custody is invalid")
        subject_key = _hash_json(subject.public_binding())
        path = custody.gate_root / f"{subject_key}-{smoke_manifest_sha256[:16]}.json"
        draft = SmokeGateReceipt(
            schema_version="v1",
            subject=subject.public_binding(),
            smoke_manifest_sha256=smoke_manifest_sha256,
            smoke_environment_fingerprint=environment_fingerprint,
            gate_sha256="0" * 64,
        )
        receipt = draft.model_copy(update={"gate_sha256": _gate_digest(draft)})
        _write_gate_atomically(path, _gate_bytes(receipt))
    except RotationRunError:
        raise
    except (OSError, ValidationError, ValueError):
        raise RotationRunError("Smoke gate could not be persisted") from None


def _load_gate(path: Path) -> SmokeGateReceipt:
    try:
        payload = path.read_bytes()
        if not payload or len(payload) > 32 * 1024 or b"\x00" in payload:
            raise RotationRunError("Smoke gate custody is invalid")
        receipt = SmokeGateReceipt.model_validate_json(payload)
        if receipt.gate_sha256 != _gate_digest(receipt):
            raise RotationRunError("Smoke gate custody is invalid")
    except RotationRunError:
        raise
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise RotationRunError("Smoke gate custody is invalid") from None
    return receipt


def _gate_digest(receipt: SmokeGateReceipt) -> str:
    return _hash_json(receipt.model_dump(mode="json", exclude={"gate_sha256"}))


def _gate_bytes(receipt: SmokeGateReceipt) -> bytes:
    return (json.dumps(receipt.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode()


def _write_gate_atomically(path: Path, payload: bytes) -> None:
    if os.path.lexists(path):
        return
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        return
    except OSError:
        raise RotationRunError("Smoke gate could not be persisted") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _hash_json(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
