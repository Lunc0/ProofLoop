"""External-process adapter for DSH's documented headless profile entry point."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from skill_evolve.config.models import CaseConfig, DshConfig, ModelConfig
from skill_evolve.runner.contracts import (
    AgentExecution,
    RawArtifact,
    RunInput,
    RunnerCaseOutput,
    RunnerError,
    RunnerExecution,
    RunnerOutput,
    TrajectoryEvent,
)
from skill_evolve.runner.dsh_session import (
    DshSessionLogError,
    LoadedDshSessionLog,
    SessionLogSignature,
    find_changed_session_logs,
    load_session_log,
    normalise_session_log,
    snapshot_session_logs,
)
from skill_evolve.runner.redaction import redact_text

_SQL_BLOCK_PATTERN = re.compile(r"```(?:sql|postgresql)\s*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)
_CREDENTIAL_FIELD_PATTERN = re.compile(
    r"(?:api[_-]?key|access[_-]?token|token|secret|password|authorization)$", re.IGNORECASE
)
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_HARNESS_POLICY_VERSION = "dsh-sql-harness-policy-v1"
_SQL_HARNESS_PLUGIN_FILENAME = "sql_harness_policy.mjs"
_SQL_HARNESS_ALLOWED_TOOLS = ("read", "grep", "sql_probe", "sql_submit")


@dataclass(frozen=True)
class DshModelSelection:
    """Run-scoped selection and short-lived provider settings for one DSH invocation."""

    model: ModelConfig
    patch_path: Path
    selection_content: bytes
    patch_content: bytes
    temporary_directory: tempfile.TemporaryDirectory[str]

    def cleanup(self) -> None:
        self.temporary_directory.cleanup()


class DshModelSettingsError(ValueError):
    """DSH user settings cannot safely supply a provider configuration."""


@dataclass(frozen=True)
class DshSqlHarnessPolicy:
    """Run-scoped native SQL tool and policy overlay for one DSH case."""

    patch_path: Path
    patch_content: bytes
    plugin_content: bytes
    metadata_content: bytes


class DshRunner:
    """Run each requested case in a fresh, non-interactive DSH session.

    DSH prints its final answer to stdout. When configured with a session-log root,
    this adapter uses the observed, version-pinned append-only session log as the
    authoritative trajectory evidence.
    """

    def __init__(
        self, config: DshConfig, profile_patch: Path | None, model: ModelConfig
    ) -> None:
        self._config = config
        self._profile_patch = profile_patch
        self._model = model

    def run(self, input: RunInput) -> RunnerExecution:
        try:
            model_selection = _prepare_model_selection(input, self._model)
        except (DshModelSettingsError, OSError, yaml.YAMLError) as error:
            raise RunnerError(
                f"could not prepare DSH model selection: {error.__class__.__name__}"
            ) from error
        try:
            case_outputs: list[RunnerCaseOutput] = []
            raw_artifacts: list[RawArtifact] = []
            stderr_parts: list[str] = []
            for case in input.cases:
                output, artifacts, stderr = self._run_case(case, input, model_selection)
                case_outputs.append(output)
                raw_artifacts.extend(artifacts)
                if stderr:
                    stderr_parts.append(f"[{case.id}]\n{stderr}")

            runner_output = RunnerOutput(schema_version="v1", cases=tuple(case_outputs))
            return RunnerExecution(
                output=runner_output,
                raw_output=(runner_output.model_dump_json(indent=2) + "\n").encode("utf-8"),
                stderr="\n\n".join(stderr_parts),
                raw_artifacts=tuple(raw_artifacts),
            )
        finally:
            model_selection.cleanup()

    def _run_case(
        self, case: CaseConfig, input: RunInput, model_selection: DshModelSelection
    ) -> tuple[RunnerCaseOutput, tuple[RawArtifact, ...], str]:
        started_at = _utc_timestamp()
        sql_policy: DshSqlHarnessPolicy | None = None
        command = self._command_for(
            case,
            input.database_access is not None,
            input.subject.filename if input.subject is not None else None,
            model_selection,
        )
        try:
            workspace, resource_paths = _prepare_task_workspace(case, input)
            sql_policy = _prepare_sql_harness_policy(case, input, resource_paths)
            command = self._command_for(
                case,
                input.database_access is not None,
                input.subject.filename if input.subject is not None else None,
                model_selection,
                sql_policy,
            )
        except (OSError, ValueError, yaml.YAMLError) as error:
            return self._failed_case(
                case,
                command,
                started_at,
                "",
                str(error),
                f"could not prepare DSH task runtime: {error.__class__.__name__}",
                model_selection,
            )
        session_before = None
        if self._config.session_log_root is not None:
            try:
                session_before = snapshot_session_logs(Path(self._config.session_log_root))
            except DshSessionLogError as error:
                return self._failed_case(
                    case,
                    command,
                    started_at,
                    "",
                    "",
                    str(error),
                    model_selection,
                    sql_policy=sql_policy,
                )
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._config.timeout_ms / 1_000,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _as_text(error.stdout)
            stderr = _as_text(error.stderr)
            return self._failed_case(
                case,
                command,
                started_at,
                stdout,
                stderr,
                f"DSH timed out after {self._config.timeout_ms} ms",
                model_selection,
                _failure_session_artifacts(
                    case.id,
                    session_before,
                    self._config.session_log_root,
                    workspace,
                ),
                sql_policy,
            )
        except OSError as error:
            return self._failed_case(
                case,
                command,
                started_at,
                "",
                str(error),
                f"could not start DSH: {error.__class__.__name__}",
                model_selection,
                sql_policy=sql_policy,
            )

        stdout = redact_text(completed.stdout)
        stderr = redact_text(completed.stderr)
        artifacts = _case_artifacts(
            case.id,
            command,
            stdout,
            stderr,
            self._config.timeout_ms,
            model_selection,
            sql_policy,
        )
        if completed.returncode != 0:
            return (
                _runner_error_output(
                    case,
                    started_at,
                    stdout,
                    f"DSH exited with code {completed.returncode}",
                ),
                (
                    *artifacts,
                    *_failure_session_artifacts(
                        case.id,
                        session_before,
                        self._config.session_log_root,
                        workspace,
                    ),
                ),
                stderr,
            )

        if session_before is not None:
            try:
                session_path, session_log = _session_log_for_workspace(
                    find_changed_session_logs(
                        session_before, Path(self._config.session_log_root or "")
                    ),
                    workspace,
                )
                artifacts = (
                    *artifacts,
                    RawArtifact(
                        relative_path=f"dsh/{case.id}.session.jsonl",
                        content=session_log.redacted_jsonl,
                    ),
                )
                evidence = normalise_session_log(session_log)
            except DshSessionLogError as error:
                return _runner_error_output(case, started_at, stdout, str(error)), artifacts, stderr
            candidate_sql, extraction_error = _extract_candidate_sql(evidence.final_message)
            if extraction_error is not None:
                return (
                    _runner_error_output(
                        case, started_at, evidence.final_message, extraction_error
                    ),
                    artifacts,
                    stderr,
                )
            binding_error = _sql_submission_binding_error(
                candidate_sql, evidence.submitted_sql, input.database_access is not None
            )
            if binding_error is not None:
                return (
                    _runner_error_output(
                        case, started_at, evidence.final_message, binding_error
                    ),
                    artifacts,
                    stderr,
                )
            trajectory = (
                TrajectoryEvent(
                    timestamp=started_at,
                    type="dsh_invocation",
                    payload={"argv": command[:-1], "timeout_ms": self._config.timeout_ms},
                ),
                *evidence.trajectory,
            )
            if evidence.unknown_event_types:
                trajectory = (
                    *trajectory,
                    TrajectoryEvent(
                        timestamp=_utc_timestamp(),
                        type="dsh_unknown_event_types",
                        payload={"types": list(evidence.unknown_event_types)},
                    ),
                )
            return (
                RunnerCaseOutput(
                    case_id=case.id,
                    status="completed",
                    final_message=evidence.final_message,
                    candidate_sql=candidate_sql,
                    trajectory=trajectory,
                    agent_execution=AgentExecution(status="ok", result=evidence.final_message),
                    usage=evidence.usage,
                ),
                artifacts,
                stderr,
            )

        if input.database_access is not None:
            return (
                _runner_error_output(
                    case,
                    started_at,
                    stdout,
                    "native SQL run requires authoritative DSH session evidence",
                ),
                artifacts,
                stderr,
            )
        candidate_sql, extraction_error = _extract_candidate_sql(stdout)
        if extraction_error is not None:
            return (
                _runner_error_output(case, started_at, stdout, extraction_error),
                artifacts,
                stderr,
            )
        return (
            RunnerCaseOutput(
                case_id=case.id,
                status="completed",
                final_message=stdout or None,
                candidate_sql=candidate_sql,
                trajectory=(
                    TrajectoryEvent(
                        timestamp=started_at,
                        type="dsh_invocation",
                        payload={"argv": command[:-1], "timeout_ms": self._config.timeout_ms},
                    ),
                    TrajectoryEvent(
                        timestamp=_utc_timestamp(),
                        type="assistant_final",
                        payload={"content": stdout},
                    ),
                ),
                agent_execution=AgentExecution(status="ok", result=stdout or None),
            ),
            artifacts,
            stderr,
        )

    def _failed_case(
        self,
        case: CaseConfig,
        command: list[str],
        timestamp: str,
        stdout: str,
        stderr: str,
        reason: str,
        model_selection: DshModelSelection,
        session_artifacts: tuple[RawArtifact, ...] = (),
        sql_policy: DshSqlHarnessPolicy | None = None,
    ) -> tuple[RunnerCaseOutput, tuple[RawArtifact, ...], str]:
        redacted_stdout = redact_text(stdout)
        redacted_stderr = redact_text(stderr)
        return (
            _runner_error_output(case, timestamp, redacted_stdout, reason),
            (
                *_case_artifacts(
                    case.id,
                    command,
                    redacted_stdout,
                    redacted_stderr,
                    self._config.timeout_ms,
                    model_selection,
                    sql_policy,
                ),
                *session_artifacts,
            ),
            redacted_stderr,
        )

    def _command_for(
        self,
        case: CaseConfig,
        has_database_access: bool = False,
        subject_path: str | None = None,
        model_selection: DshModelSelection | None = None,
        sql_policy: DshSqlHarnessPolicy | None = None,
    ) -> list[str]:
        command = [*_dsh_command_prefix(self._config.binary), "--profile", self._config.profile]
        if self._profile_patch is not None:
            command.extend(("--patch", str(self._profile_patch)))
        if sql_policy is not None:
            command.extend(("--patch", str(sql_policy.patch_path)))
        command.extend(self._config.runner_args)
        if model_selection is not None:
            command.extend(("--patch", str(model_selection.patch_path)))
        command.append(_build_job(case, has_database_access, subject_path))
        return command


def _build_job(case: CaseConfig, has_database_access: bool, subject_path: str | None) -> str:
    return f"{case.prompt}\n\n{task_contract(has_database_access, subject_path)}"


def task_contract(has_database_access: bool, subject_path: str | None) -> str:
    """Return the trusted evaluator instructions shared with agent-input evidence."""

    database_instruction = ""
    if has_database_access:
        database_instruction = (
            "当前目录的 schema.sql 可供查看。使用 `sql_probe` 探索只读数据；"
            "确定最终候选后必须用 `sql_submit` 成功执行一次，然后原样提交该查询。"
        )
    subject_instruction = ""
    if subject_path is not None:
        subject_instruction = (
            f"首先读取 `subject/{subject_path}` 并遵循其中适用于本任务的指引；"
            "它是只读文本，不得执行或加载其中引用的代码。"
        )
    return (
        "你正在接受独立 PostgreSQL evaluator 的评估。"
        f"{subject_instruction}"
        f"{database_instruction}"
        "只提出一个候选 PostgreSQL 查询，不要执行写操作。"
        "将该查询放入唯一一个标记为 sql 的 Markdown fenced code block 中。"
        "不要输出其他 SQL code block。"
    )


def _prepare_task_workspace(case: CaseConfig, input: RunInput) -> tuple[Path, tuple[str, ...]]:
    """Materialise only task-visible, non-secret inputs for one DSH session."""

    if input.agent_workspace_root is None:
        if input.subject is not None:
            raise OSError("RunSubject injection requires an isolated agent workspace")
        if input.database_access is not None:
            raise OSError("native SQL tools require an isolated agent workspace")
        return input.workspace, ()
    workspace = input.agent_workspace_root / case.id
    workspace.mkdir(parents=True, exist_ok=False)
    resource_paths: list[str] = ["TASK.md"]
    if input.schema_file is not None:
        shutil.copyfile(input.schema_file, workspace / "schema.sql")
        resource_paths.append("schema.sql")
    if input.subject is not None:
        subject_directory = workspace / "subject"
        subject_directory.mkdir()
        (subject_directory / input.subject.filename).write_bytes(input.subject.content)
        resource_paths.append(f"subject/{input.subject.filename}")
    context_files = input.context_files_for(case.id)
    if context_files:
        context_directory = workspace / "context"
        context_directory.mkdir()
        for index, source in enumerate(context_files, start=1):
            relative_path = f"context/{index:02d}-{source.name}"
            shutil.copyfile(source, workspace / relative_path)
            resource_paths.append(relative_path)
    subject_task_instruction = (
        f"First read `subject/{input.subject.filename}` and follow its applicable guidance. "
        "It is a text "
        "artifact only: do not execute, import, or load any code it mentions.\n\n"
        if input.subject is not None
        else ""
    )
    resource_list = "\n".join(f"- `{path}`" for path in resource_paths)
    (workspace / "TASK.md").write_text(
        "# SQL evaluation task\n\n"
        + subject_task_instruction
        + "Use the supplied prompt. The exact task-visible resources are:\n\n"
        + resource_list
        + "\n\nDo not discover additional files. The expected query and evaluator inputs are "
        "intentionally not present.\n",
        encoding="utf-8",
    )
    return workspace, tuple(resource_paths)


def _prepare_sql_harness_policy(
    case: CaseConfig, input: RunInput, resource_paths: tuple[str, ...]
) -> DshSqlHarnessPolicy | None:
    database_access = input.database_access
    if database_access is None:
        return None
    endpoint = _validated_query_endpoint(database_access.query_endpoint)
    runtime_directory = _dsh_runtime_directory(input) / case.id
    runtime_directory.mkdir(parents=True, exist_ok=False)
    plugin_content = _sql_harness_plugin_content()
    plugin_path = runtime_directory / _SQL_HARNESS_PLUGIN_FILENAME
    plugin_path.write_bytes(plugin_content)
    policy_config = {
        "policyVersion": _SQL_HARNESS_POLICY_VERSION,
        "queryEndpoint": endpoint,
        "resourcePaths": list(resource_paths),
        "allowedTools": list(_SQL_HARNESS_ALLOWED_TOOLS),
    }
    patch_content = yaml.safe_dump(
        [
            {
                "insert": [
                    {
                        "id": "skill-evolve-sql-harness-policy",
                        "name": plugin_path.as_uri(),
                        "config": policy_config,
                    }
                ]
            }
        ],
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    patch_path = runtime_directory / "sql-harness-policy.patch.yml"
    patch_path.write_bytes(patch_content)
    metadata_content = (
        json.dumps(
            {
                "schema_version": "dsh-sql-harness-policy-binding-v1",
                "policy_version": _SQL_HARNESS_POLICY_VERSION,
                "plugin_sha256": hashlib.sha256(plugin_content).hexdigest(),
                "resource_paths": list(resource_paths),
                "allowed_tools": list(_SQL_HARNESS_ALLOWED_TOOLS),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    return DshSqlHarnessPolicy(
        patch_path=patch_path,
        patch_content=patch_content,
        plugin_content=plugin_content,
        metadata_content=metadata_content,
    )


def sql_harness_policy_identity() -> dict[str, str]:
    """Return the stable public identity bound into SQL benchmark fingerprints."""

    return {
        "policy_version": _SQL_HARNESS_POLICY_VERSION,
        "plugin_sha256": hashlib.sha256(_sql_harness_plugin_content()).hexdigest(),
    }


def _sql_harness_plugin_content() -> bytes:
    return (
        resources.files("skill_evolve.runner.dsh_plugins")
        .joinpath(_SQL_HARNESS_PLUGIN_FILENAME)
        .read_bytes()
    )


def _validated_query_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("query endpoint is invalid") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or parsed.path != "/query"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("query endpoint must be a loopback HTTP /query URL")
    return value


def _prepare_model_selection(input: RunInput, model: ModelConfig) -> DshModelSelection:
    """Create a model lock while retaining non-secret DSH provider route settings."""

    runtime_directory = _dsh_runtime_directory(input)
    runtime_directory.mkdir(parents=True, exist_ok=True)
    selection_path = runtime_directory / "model-selection.yaml"
    patch_path = runtime_directory / "model-selection.patch.yml"
    selection_content = _model_selection_content(model)
    selection_path.write_bytes(selection_content)
    temporary_directory = tempfile.TemporaryDirectory(prefix="skill-evolve-dsh-settings-")
    try:
        settings_path = Path(temporary_directory.name) / "settings.yaml"
        settings_path.write_text(
            yaml.safe_dump(
                {
                    **_provider_settings_from(_dsh_settings_path()),
                    "agent-default-model": {
                        "provider": model.provider,
                        "model": model.name,
                    },
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        patch_content = yaml.safe_dump(
            [
                {
                    "id": "settings",
                    "config": {
                        "path": str(settings_path),
                        "watch": False,
                    },
                }
            ],
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        patch_path.write_bytes(patch_content)
        return DshModelSelection(
            model=model,
            patch_path=patch_path,
            selection_content=selection_content,
            patch_content=patch_content,
            temporary_directory=temporary_directory,
        )
    except Exception:
        temporary_directory.cleanup()
        raise


def _model_selection_content(model: ModelConfig) -> bytes:
    return yaml.safe_dump(
        {
            "agent-default-model": {
                "provider": model.provider,
                "model": model.name,
            }
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def _provider_settings_from(settings_path: Path) -> dict[str, Any]:
    if not settings_path.is_file():
        return {}
    parsed = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise DshModelSettingsError("DSH settings root must be a mapping")
    provider_settings: dict[str, Any] = {}
    for namespace, value in parsed.items():
        if not isinstance(namespace, str) or not namespace.startswith("llm-"):
            continue
        _validate_provider_settings(value, (namespace,))
        provider_settings[namespace] = value
    return provider_settings


def _validate_provider_settings(value: Any, path: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise DshModelSettingsError("DSH provider settings keys must be strings")
            child_path = (*path, key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized_key == "apikeyenv":
                if not isinstance(child, str) or not _ENVIRONMENT_VARIABLE_PATTERN.fullmatch(child):
                    raise DshModelSettingsError(
                        "DSH provider setting "
                        f"{'.'.join(child_path)} must be an environment variable name"
                    )
                continue
            if _CREDENTIAL_FIELD_PATTERN.search(key):
                raise DshModelSettingsError(
                    f"DSH provider setting {'.'.join(child_path)} must not contain a credential"
                )
            _validate_provider_settings(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_provider_settings(child, (*path, str(index)))


def _dsh_settings_path() -> Path:
    dsh_home = os.environ.get("DSH_HOME")
    return Path(dsh_home) / "settings.yaml" if dsh_home else Path.home() / ".dsh" / "settings.yaml"


def _dsh_runtime_directory(input: RunInput) -> Path:
    if input.agent_workspace_root is not None:
        return input.agent_workspace_root.parent / "dsh-runtime"
    return input.workspace / ".skill-evolve-dsh-runtime"


def _session_log_for_workspace(
    candidates: tuple[Path, ...], workspace: Path
) -> tuple[Path, LoadedDshSessionLog]:
    matches: list[tuple[Path, LoadedDshSessionLog]] = []
    expected_workspace = workspace.resolve()
    for path in candidates:
        session_log = load_session_log(path)
        if _session_log_workspace(session_log) == expected_workspace:
            matches.append((path, session_log))
    if len(matches) != 1:
        raise DshSessionLogError(
            "expected exactly one changed DSH session log for workspace "
            f"{workspace}, found {len(matches)}"
        )
    return matches[0]


def _session_log_workspace(session_log: LoadedDshSessionLog) -> Path | None:
    if not session_log.events:
        return None
    workspace = session_log.events[0].get("cwd")
    if not isinstance(workspace, str) or not workspace:
        return None
    return Path(workspace).resolve()


def _dsh_command_prefix(binary: str) -> list[str]:
    """Resolve npm's Windows shim to its Node entry without invoking a shell.

    ``subprocess`` cannot execute ``dsh.ps1`` and invoking npm's ``dsh.cmd`` with
    an untrusted benchmark prompt would hand that prompt to ``cmd.exe``. The npm
    package exposes a stable JavaScript entry next to the shim, so call Node with
    that entry as ordinary argv instead.
    """

    if os.name != "nt":
        return [binary]
    shim = _find_windows_npm_shim(binary)
    if shim is None:
        return [binary]
    entry = shim.parent / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    if not entry.is_file():
        return [binary]
    node = shim.parent / "node.exe"
    return [str(node) if node.is_file() else "node", str(entry)]


def _find_windows_npm_shim(binary: str) -> Path | None:
    configured = Path(binary)
    if configured.suffix.lower() == ".cmd" and configured.is_file():
        return configured
    if configured.suffix:
        return None
    resolved = shutil.which(f"{binary}.cmd")
    return Path(resolved) if resolved is not None else None


def _runner_error_output(
    case: CaseConfig, timestamp: str, stdout: str, reason: str
) -> RunnerCaseOutput:
    return RunnerCaseOutput(
        case_id=case.id,
        status="runner_error",
        final_message=stdout or None,
        trajectory=(
            TrajectoryEvent(
                timestamp=timestamp,
                type="dsh_runner_error",
                payload={"reason": reason},
            ),
        ),
        agent_execution=AgentExecution(status="error", error=reason),
    )


def _case_artifacts(
    case_id: str,
    command: list[str],
    stdout: str,
    stderr: str,
    timeout_ms: int,
    model_selection: DshModelSelection,
    sql_policy: DshSqlHarnessPolicy | None = None,
) -> tuple[RawArtifact, ...]:
    invocation = json.dumps(
        {
            "argv": command[:-1],
            "timeout_ms": timeout_ms,
            "model": {
                "provider": model_selection.model.provider,
                "name": model_selection.model.name,
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    artifacts = (
        RawArtifact(
            relative_path=f"dsh/{case_id}.invocation.json",
            content=(invocation + "\n").encode("utf-8"),
        ),
        RawArtifact(
            relative_path=f"dsh/{case_id}.stdout.log", content=stdout.encode("utf-8")
        ),
        RawArtifact(
            relative_path=f"dsh/{case_id}.stderr.log", content=stderr.encode("utf-8")
        ),
        RawArtifact(
            relative_path=f"dsh/{case_id}.model-selection.yaml",
            content=model_selection.selection_content,
        ),
        RawArtifact(
            relative_path=f"dsh/{case_id}.model-selection.patch.yml",
            content=model_selection.patch_content,
        ),
    )
    if sql_policy is None:
        return artifacts
    return (
        *artifacts,
        RawArtifact(
            relative_path=f"dsh/{case_id}.sql-harness-policy.patch.yml",
            content=sql_policy.patch_content,
        ),
        RawArtifact(
            relative_path=f"dsh/{case_id}.sql-harness-policy.json",
            content=sql_policy.metadata_content,
        ),
    )


def _failure_session_artifacts(
    case_id: str,
    session_before: dict[Path, SessionLogSignature] | None,
    session_log_root: str | None,
    workspace: Path,
) -> tuple[RawArtifact, ...]:
    """Preserve a changed DSH session even when its turn did not complete.

    An interrupted session cannot be normalised into a completed trajectory, but its
    redacted event stream remains essential failure evidence. Capture failures are
    kept as a separate artifact so the primary process failure is not obscured.
    """

    if session_before is None or session_log_root is None:
        return ()
    try:
        _, session_log = _session_log_for_workspace(
            find_changed_session_logs(session_before, Path(session_log_root)), workspace
        )
    except DshSessionLogError as error:
        return (
            RawArtifact(
                relative_path=f"dsh/{case_id}.session-capture-error.log",
                content=(redact_text(str(error)) + "\n").encode("utf-8"),
            ),
        )
    return (
        RawArtifact(
            relative_path=f"dsh/{case_id}.session.jsonl",
            content=session_log.redacted_jsonl,
        ),
    )


def _extract_candidate_sql(stdout: str) -> tuple[str | None, str | None]:
    blocks = [block.strip() for block in _SQL_BLOCK_PATTERN.findall(stdout) if block.strip()]
    if len(blocks) > 1:
        return None, "DSH final answer contains multiple SQL code blocks"
    return (blocks[0], None) if blocks else (None, None)


def _sql_submission_binding_error(
    candidate_sql: str | None, submitted_sql: str | None, required: bool
) -> str | None:
    if not required:
        return None
    if submitted_sql is None:
        return "DSH session has no successful sql_submit evidence"
    if candidate_sql is None:
        return "DSH final answer has no SQL candidate to bind to sql_submit"
    if _normalise_bound_sql(candidate_sql) != _normalise_bound_sql(submitted_sql):
        return "DSH final SQL does not match the successful sql_submit query"
    return None


def _normalise_bound_sql(value: str) -> str:
    normalized = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return normalized


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
