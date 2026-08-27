"""External-process adapter for DSH's documented headless profile entry point."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


@dataclass(frozen=True)
class DshModelSelection:
    """Non-secret, run-scoped DSH settings used to select the requested model."""

    model: ModelConfig
    settings_path: Path
    patch_path: Path
    settings_content: bytes
    patch_content: bytes


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
        except OSError as error:
            raise RunnerError(
                f"could not prepare DSH model selection: {error.__class__.__name__}"
            ) from error
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

    def _run_case(
        self, case: CaseConfig, input: RunInput, model_selection: DshModelSelection
    ) -> tuple[RunnerCaseOutput, tuple[RawArtifact, ...], str]:
        command = self._command_for(
            case,
            input.database_access is not None,
            input.candidate is not None,
            model_selection,
        )
        started_at = _utc_timestamp()
        try:
            workspace = _prepare_task_workspace(case, input)
        except OSError as error:
            return self._failed_case(
                case,
                command,
                started_at,
                "",
                str(error),
                f"could not prepare DSH task workspace: {error.__class__.__name__}",
                model_selection,
            )
        session_before = None
        if self._config.session_log_root is not None:
            try:
                session_before = snapshot_session_logs(Path(self._config.session_log_root))
            except DshSessionLogError as error:
                return self._failed_case(
                    case, command, started_at, "", "", str(error), model_selection
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
            )

        stdout = redact_text(completed.stdout)
        stderr = redact_text(completed.stderr)
        artifacts = _case_artifacts(
            case.id, command, stdout, stderr, self._config.timeout_ms, model_selection
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
                ),
                *session_artifacts,
            ),
            redacted_stderr,
        )

    def _command_for(
        self,
        case: CaseConfig,
        has_database_access: bool = False,
        has_candidate_skill: bool = False,
        model_selection: DshModelSelection | None = None,
    ) -> list[str]:
        command = [*_dsh_command_prefix(self._config.binary), "--profile", self._config.profile]
        if self._profile_patch is not None:
            command.extend(("--patch", str(self._profile_patch)))
        command.extend(self._config.runner_args)
        if model_selection is not None:
            command.extend(("--patch", str(model_selection.patch_path)))
        command.append(_build_job(case, has_database_access, has_candidate_skill))
        return command


def _build_job(case: CaseConfig, has_database_access: bool, has_candidate_skill: bool) -> str:
    database_instruction = ""
    if has_database_access:
        database_instruction = (
            "当前目录的 schema.sql 可供查看。如需探索数据，只能通过 "
            "`.\\sql-readonly.ps1 -Query '<SQL>'` 执行只读查询。"
        )
    candidate_instruction = ""
    if has_candidate_skill:
        candidate_instruction = (
            "首先读取 `candidate/SKILL.md` 并遵循其中适用于本任务的指引；"
            "它是只读文本，不得执行或加载其中引用的代码。"
        )
    return (
        f"{case.prompt}\n\n"
        "你正在接受独立 PostgreSQL evaluator 的评估。"
        f"{candidate_instruction}"
        f"{database_instruction}"
        "只提出一个候选 PostgreSQL 查询，不要执行写操作。"
        "将该查询放入唯一一个标记为 sql 的 Markdown fenced code block 中。"
        "不要输出其他 SQL code block。"
    )


def _prepare_task_workspace(case: CaseConfig, input: RunInput) -> Path:
    """Materialise only task-visible, non-secret inputs for one DSH session."""

    if input.agent_workspace_root is None:
        if input.candidate is not None:
            raise OSError("verified candidate injection requires an isolated agent workspace")
        return input.workspace
    workspace = input.agent_workspace_root / case.id
    workspace.mkdir(parents=True, exist_ok=False)
    if input.schema_file is not None:
        shutil.copyfile(input.schema_file, workspace / "schema.sql")
    if input.candidate is not None:
        candidate_directory = workspace / "candidate"
        candidate_directory.mkdir()
        (candidate_directory / "SKILL.md").write_bytes(input.candidate.skill_bytes)
    elif input.candidate_file is not None:
        candidate_directory = workspace / "candidate"
        candidate_directory.mkdir()
        shutil.copyfile(input.candidate_file, candidate_directory / input.candidate_file.name)
    context_files = input.context_files_for(case.id)
    if context_files:
        context_directory = workspace / "context"
        context_directory.mkdir()
        for index, source in enumerate(context_files, start=1):
            shutil.copyfile(source, context_directory / f"{index:02d}-{source.name}")
    candidate_task_instruction = (
        "First read `candidate/SKILL.md` and follow its applicable guidance. It is a text "
        "artifact only: do not execute, import, or load any code it mentions.\n\n"
        if input.candidate is not None
        else ""
    )
    (workspace / "TASK.md").write_text(
        "# SQL evaluation task\n\n"
        + candidate_task_instruction
        + "Use the supplied prompt. `schema.sql` and `context/` contain the only task context. "
        "The expected query and evaluator inputs are intentionally not present.\n",
        encoding="utf-8",
    )
    if input.database_access is not None:
        _write_read_only_query_tool(workspace, input.database_access.query_endpoint)
    return workspace


def _prepare_model_selection(input: RunInput, model: ModelConfig) -> DshModelSelection:
    """Create the final DSH settings overlay without touching user settings or credentials.

    DSH's ``agent-default-model`` settings namespace takes precedence over the
    profile composition, so a user's interactive default would otherwise change
    a manifest-declared benchmark model. The final overlay redirects only the
    settings provider to a run-scoped document; the credentials provider keeps
    its normal DSH-home location.
    """

    runtime_directory = _dsh_runtime_directory(input)
    runtime_directory.mkdir(parents=True, exist_ok=True)
    settings_path = runtime_directory / "model-selection.yaml"
    patch_path = runtime_directory / "model-selection.patch.yml"
    settings_content = yaml.safe_dump(
        {
            "agent-default-model": {
                "provider": model.provider,
                "model": model.name,
            }
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
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
    settings_path.write_bytes(settings_content)
    patch_path.write_bytes(patch_content)
    return DshModelSelection(
        model=model,
        settings_path=settings_path,
        patch_path=patch_path,
        settings_content=settings_content,
        patch_content=patch_content,
    )


def _dsh_runtime_directory(input: RunInput) -> Path:
    if input.agent_workspace_root is not None:
        return input.agent_workspace_root.parent / "dsh-runtime"
    return input.workspace / ".skill-evolve-dsh-runtime"


def _write_read_only_query_tool(workspace: Path, query_endpoint: str) -> None:
    """Create a capability-limited discovery tool for the disposable fixture only."""

    script = f"""[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$Query
)

$ErrorActionPreference = 'Stop'
$request = @{{ query = $Query }} | ConvertTo-Json -Compress
$response = Invoke-RestMethod -Uri '{query_endpoint}' -Method Post `
  -ContentType 'application/json' -Body $request
$response | ConvertTo-Json -Depth 16
"""
    (workspace / "sql-readonly.ps1").write_text(script, encoding="utf-8")


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
    return (
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
            content=model_selection.settings_content,
        ),
        RawArtifact(
            relative_path=f"dsh/{case_id}.model-selection.patch.yml",
            content=model_selection.patch_content,
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


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
