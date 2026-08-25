"""External-process adapter for DSH's documented headless profile entry point."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from skill_evolve.config.models import CaseConfig, DshConfig
from skill_evolve.runner.contracts import (
    AgentExecution,
    RawArtifact,
    RunInput,
    RunnerCaseOutput,
    RunnerExecution,
    RunnerOutput,
    TrajectoryEvent,
)

_SQL_BLOCK_PATTERN = re.compile(r"```(?:sql|postgresql)\s*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)
_ASSIGNMENT_SECRET_PATTERN = re.compile(
    r"(?im)(\b(?:api[_-]?key|password|secret|token|authorization)\b\s*[:=]\s*)([^\s'\"]+)"
)
_BEARER_SECRET_PATTERN = re.compile(r"(?i)(\bbearer\s+)([^\s'\"]+)")
_KEY_PREFIX_SECRET_PATTERN = re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{12,}\b")


class DshRunner:
    """Run each requested case in a fresh, non-interactive DSH session.

    DSH prints its final answer to stdout. This adapter deliberately relies only on
    that public CLI boundary until a local session-log schema is captured and pinned.
    """

    def __init__(self, config: DshConfig, profile_patch: Path | None) -> None:
        self._config = config
        self._profile_patch = profile_patch

    def run(self, input: RunInput) -> RunnerExecution:
        case_outputs: list[RunnerCaseOutput] = []
        raw_artifacts: list[RawArtifact] = []
        stderr_parts: list[str] = []
        for case in input.cases:
            output, artifacts, stderr = self._run_case(case, input.workspace)
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
        self, case: CaseConfig, workspace: Path
    ) -> tuple[RunnerCaseOutput, tuple[RawArtifact, ...], str]:
        command = self._command_for(case)
        started_at = _utc_timestamp()
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
            )
        except OSError as error:
            return self._failed_case(
                case,
                command,
                started_at,
                "",
                str(error),
                f"could not start DSH: {error.__class__.__name__}",
            )

        stdout = _redact(completed.stdout)
        stderr = _redact(completed.stderr)
        artifacts = _case_artifacts(case.id, command, stdout, stderr, self._config.timeout_ms)
        if completed.returncode != 0:
            return (
                _runner_error_output(
                    case,
                    started_at,
                    stdout,
                    f"DSH exited with code {completed.returncode}",
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
    ) -> tuple[RunnerCaseOutput, tuple[RawArtifact, ...], str]:
        redacted_stdout = _redact(stdout)
        redacted_stderr = _redact(stderr)
        return (
            _runner_error_output(case, timestamp, redacted_stdout, reason),
            _case_artifacts(
                case.id, command, redacted_stdout, redacted_stderr, self._config.timeout_ms
            ),
            redacted_stderr,
        )

    def _command_for(self, case: CaseConfig) -> list[str]:
        command = [*_dsh_command_prefix(self._config.binary), "--profile", self._config.profile]
        if self._profile_patch is not None:
            command.extend(("--patch", str(self._profile_patch)))
        command.extend(self._config.runner_args)
        command.append(_build_job(case))
        return command


def _build_job(case: CaseConfig) -> str:
    return (
        f"{case.prompt}\n\n"
        "你正在接受独立 PostgreSQL evaluator 的评估。"
        "只提出一个候选 PostgreSQL 查询，不要执行写操作。"
        "将该查询放入唯一一个标记为 sql 的 Markdown fenced code block 中。"
        "不要输出其他 SQL code block。"
    )


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
    case_id: str, command: list[str], stdout: str, stderr: str, timeout_ms: int
) -> tuple[RawArtifact, ...]:
    invocation = json.dumps(
        {"argv": command[:-1], "timeout_ms": timeout_ms},
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
    )


def _extract_candidate_sql(stdout: str) -> tuple[str | None, str | None]:
    blocks = [block.strip() for block in _SQL_BLOCK_PATTERN.findall(stdout) if block.strip()]
    if len(blocks) > 1:
        return None, "DSH final answer contains multiple SQL code blocks"
    return (blocks[0], None) if blocks else (None, None)


def _redact(value: str) -> str:
    value = _ASSIGNMENT_SECRET_PATTERN.sub(r"\1<redacted>", value)
    value = _BEARER_SECRET_PATTERN.sub(r"\1<redacted>", value)
    return _KEY_PREFIX_SECRET_PATTERN.sub("<redacted>", value)


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
