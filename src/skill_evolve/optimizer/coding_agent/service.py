"""Run one isolated Codex edit and publish validated atomic artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from skill_evolve.config.models import OptimizerConfig
from skill_evolve.optimizer.coding_agent.contracts import (
    AtomicEditSource,
    AtomicSkillEditDeclaration,
    CodingAgentEditReceipt,
    CodingAgentEditResult,
    CodingAgentErrorCode,
    CodingAgentStatus,
)
from skill_evolve.optimizer.coding_agent.launcher import (
    CodexLaunchError,
    CodexLaunchSettings,
    ProcessRunner,
    invoke_codex,
)
from skill_evolve.optimizer.coding_agent.prompt import (
    CODING_AGENT_PROMPT_VERSION,
    coding_agent_prompt,
)
from skill_evolve.optimizer.coding_agent.replay import (
    MAX_SKILL_BYTES,
    AtomicReplayError,
    bind_atomic_edits,
)
from skill_evolve.optimizer.coding_agent.workspace import (
    AgentWorkspaceError,
    configured_workspace_root,
    isolated_agent_workspace,
)
from skill_evolve.optimizer.evidence import load_prepared_evolution_input
from skill_evolve.optimizer.skill_map import build_skill_map
from skill_evolve.registry.store import RunDirectory
from skill_evolve.subject import RunSubject
from skill_evolve.trajectory.compressor import canonical_json_bytes
from skill_evolve.trajectory.contracts import EvolutionAgentInputV3

_ARTIFACT_ROOT = "optimizer/coding-agent"
_MAX_DECLARATION_BYTES = 512 * 1024


def edit_with_coding_agent(
    run: RunDirectory,
    subject: RunSubject,
    *,
    run_status: str,
    repository_root: Path,
    candidate_store_root: Path,
    runs_root: Path,
    workspace_root: Path | None = None,
    settings: CodexLaunchSettings | None = None,
    runner: ProcessRunner | None = None,
) -> CodingAgentEditResult:
    """Invoke Codex once, then bind and replay only its two formal output files."""

    _ensure_artifact_root_absent(run)
    if run_status in {"runner_invalid", "evaluator_invalid"}:
        return _terminal(run, "skipped_invalid_evidence")
    evidence_config = OptimizerConfig(enabled=True, max_input_bytes=2_097_152)
    prepared = load_prepared_evolution_input(run, subject, evidence_config)
    if prepared is None:
        return _terminal(run, "skipped_invalid_evidence")
    if not prepared.payload.trajectories:
        return _terminal(run, "skipped_no_failed_trajectories")

    raw_artifacts: dict[str, bytes] = {}
    try:
        root = workspace_root if workspace_root is not None else configured_workspace_root()
        launch_settings = settings if settings is not None else _settings_from_environment()
        evidence_bytes, evidence_refs = _evidence_bundle(prepared.payload)
        skill_map = build_skill_map(subject.content, max_input_bytes=MAX_SKILL_BYTES)
        skill_map_bytes = canonical_json_bytes(skill_map.model_dump(mode="json"))
        manifest_bytes = canonical_json_bytes(
            {
                "schema_version": "coding-agent-input-manifest-v1",
                "run_id": run.run_id,
                "parent_candidate_id": subject.subject_id,
                "parent_skill_sha256": subject.content_sha256,
                "skill_map_sha256": _sha256(skill_map_bytes),
                "evidence_bundle_sha256": _sha256(evidence_bytes),
                "allowed_evidence_refs": sorted(evidence_refs),
                "prompt_version": CODING_AGENT_PROMPT_VERSION,
                "locator_policy_version": skill_map.parser_policy_version,
                "replay_policy_version": "atomic-skill-replay-policy-v1",
            }
        )
        private_roots = _private_roots(repository_root, candidate_store_root, runs_root)
        with isolated_agent_workspace(
            root,
            {
                "SKILL.md": subject.content,
                "skill-map.json": skill_map_bytes + b"\n",
                "evidence.json": evidence_bytes + b"\n",
                "manifest.json": manifest_bytes + b"\n",
            },
            private_roots=private_roots,
        ) as workspace:
            try:
                if runner is None:
                    invocation = invoke_codex(
                        workspace,
                        coding_agent_prompt(),
                        launch_settings,
                    )
                else:
                    invocation = invoke_codex(
                        workspace,
                        coding_agent_prompt(),
                        launch_settings,
                        runner=runner,
                    )
            finally:
                workspace.verify_input_hash()
            invocation_bytes = canonical_json_bytes(invocation.model_dump(mode="json"))
            raw_artifacts["invocation-receipt.json"] = invocation_bytes + b"\n"
            full_bytes = workspace.read_output("SKILL.md", max_bytes=MAX_SKILL_BYTES)
            declaration_bytes = workspace.read_output(
                "atomic-edits.json", max_bytes=_MAX_DECLARATION_BYTES
            )
            raw_artifacts["raw-output/SKILL.md"] = full_bytes
            raw_artifacts["raw-output/atomic-edits.json"] = declaration_bytes
            declaration = AtomicSkillEditDeclaration.model_validate_json(declaration_bytes)
            replay = bind_atomic_edits(
                subject.content,
                declaration,
                full_bytes,
                run_id=run.run_id,
                parent_candidate_id=subject.subject_id,
                evidence_bundle_sha256=_sha256(evidence_bytes),
                invocation_receipt_sha256=_sha256(invocation_bytes),
                allowed_evidence_refs=frozenset(evidence_refs),
            )
    except (ValidationError, json.JSONDecodeError):
        return _terminal(
            run,
            "invalid_output",
            error_code="invalid_declaration",
            artifacts=raw_artifacts,
        )
    except AtomicReplayError as error:
        return _terminal(
            run,
            "invalid_output",
            error_code=_error_code(error.code),
            artifacts=raw_artifacts,
        )
    except (AgentWorkspaceError, CodexLaunchError) as error:
        status: CodingAgentStatus = (
            "unavailable"
            if error.code
            in {
                "workspace_root_missing",
                "unsafe_workspace_root",
                "codex_not_found",
                "codex_version_unsupported",
                "codex_version_invalid",
            }
            else "agent_failed"
        )
        return _terminal(
            run,
            status,
            error_code=_error_code(error.code),
            artifacts=raw_artifacts,
        )
    except (OSError, ValueError):
        return _terminal(
            run,
            "invalid_output",
            error_code="unexpected_error",
            artifacts=raw_artifacts,
        )

    bound_bytes = canonical_json_bytes(replay.bound_edits.model_dump(mode="json"))
    artifacts = dict(raw_artifacts)
    for name, value in replay.singleton_patch_bytes.items():
        artifacts[f"bound/singleton-{name}.patch"] = value
    artifacts["bound/atomic-edits.json"] = bound_bytes + b"\n"
    artifacts["bound/full.patch"] = replay.full_patch_bytes
    receipt = CodingAgentEditReceipt(
        status="validated",
        source=replay.bound_edits.source,
        atomic_edits_sha256=_sha256(bound_bytes),
        full_skill_sha256=_sha256(replay.full_skill_bytes),
        edit_count=len(replay.bound_edits.edits),
    )
    artifacts["receipt.json"] = (
        canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"
    )
    _publish_once(run, artifacts)
    return CodingAgentEditResult(
        receipt=receipt,
        atomic_edits_path=f"{_ARTIFACT_ROOT}/bound/atomic-edits.json",
    )


def _evidence_bundle(payload: EvolutionAgentInputV3) -> tuple[bytes, set[str]]:
    cases: list[dict[str, Any]] = []
    refs: set[str] = set()
    for case_index, case in enumerate(payload.trajectories, start=1):
        value = case.model_dump(mode="json")
        value.pop("case_id")
        raw_evidence = value.pop("evidence")
        projected: list[dict[str, Any]] = []
        for evidence_index, evidence in enumerate(raw_evidence, start=1):
            digest = _sha256(canonical_json_bytes(evidence))[:12]
            ref = f"evidence-{case_index:03d}-{evidence_index:03d}-{digest}"
            refs.add(ref)
            projected.append({"evidence_ref": ref, "value": evidence})
        case_digest = _sha256(canonical_json_bytes(value))[:12]
        cases.append(
            {
                "case_ref": f"case-{case_index:03d}-{case_digest}",
                "value": value,
                "evidence": projected,
            }
        )
    return (
        canonical_json_bytes(
            {
                "schema_version": "coding-agent-evidence-v1",
                "projection_rule_version": payload.projection_rule_version,
                "environment_fingerprint": payload.environment_fingerprint,
                "cases": cases,
            }
        ),
        refs,
    )


def _terminal(
    run: RunDirectory,
    status: CodingAgentStatus,
    *,
    error_code: CodingAgentErrorCode | None = None,
    source: AtomicEditSource | None = None,
    artifacts: Mapping[str, bytes] | None = None,
) -> CodingAgentEditResult:
    receipt = CodingAgentEditReceipt.model_validate(
        {"status": status, "source": source, "error_code": error_code}
    )
    published = dict(artifacts or {})
    published["receipt.json"] = (
        canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"
    )
    _publish_once(run, published)
    return CodingAgentEditResult(receipt=receipt)


def _ensure_artifact_root_absent(run: RunDirectory) -> None:
    target = run.path / _ARTIFACT_ROOT
    if os.path.lexists(target):
        raise ValueError("coding-agent artifacts already exist for this run")


def _publish_once(run: RunDirectory, artifacts: Mapping[str, bytes]) -> None:
    run_root = run.path.resolve(strict=True)
    parent = run_root / "optimizer"
    parent.mkdir(exist_ok=True)
    if parent.is_symlink() or parent.resolve(strict=True).parent != run_root:
        raise ValueError("unsafe coding-agent artifact parent")
    target = parent / "coding-agent"
    if os.path.lexists(target):
        raise ValueError("coding-agent artifacts already exist for this run")
    staging = Path(tempfile.mkdtemp(prefix=".coding-agent-", dir=parent))
    try:
        staging_root = staging.resolve(strict=True)
        for relative_name, value in artifacts.items():
            relative = Path(relative_name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe coding-agent artifact name")
            destination = staging_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = destination.parent.resolve(strict=True)
            resolved_parent.relative_to(staging_root)
            destination.write_bytes(value)
        os.replace(staging_root, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _private_roots(repository: Path, candidate_store: Path, runs: Path) -> tuple[Path, ...]:
    values = [repository, candidate_store, runs]
    heldout = os.environ.get("SKILL_EVOLVE_HELDOUT_ROOT")
    if heldout:
        values.append(Path(heldout))
    return tuple(values)


def _settings_from_environment() -> CodexLaunchSettings:
    timeout_text = os.environ.get("SKILL_EVOLVE_CODEX_TIMEOUT_MS", "300000")
    try:
        timeout = int(timeout_text)
    except ValueError as error:
        raise AgentWorkspaceError("invalid Codex timeout") from error
    return CodexLaunchSettings(
        binary=os.environ.get("SKILL_EVOLVE_CODEX_BINARY", "codex"),
        model=os.environ.get("SKILL_EVOLVE_CODEX_MODEL") or None,
        timeout_ms=timeout,
    )


def _error_code(value: str) -> CodingAgentErrorCode:
    allowed: set[str] = {
        "workspace_root_missing",
        "unsafe_workspace_root",
        "codex_not_found",
        "codex_version_unsupported",
        "codex_version_invalid",
        "request_timeout",
        "process_failed",
        "input_tampered",
        "missing_output",
        "unsafe_output",
        "invalid_declaration",
        "invalid_skill",
        "invalid_binding",
        "locked_target",
        "overlapping_edits",
        "undeclared_change",
        "unexpected_error",
    }
    if value in allowed:
        return cast(CodingAgentErrorCode, value)
    return "unexpected_error"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
