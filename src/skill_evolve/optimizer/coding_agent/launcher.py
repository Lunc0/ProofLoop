"""Codex CLI launcher with a fixed fine-grained permission profile."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_evolve.optimizer.coding_agent.contracts import CodingAgentInvocationReceipt
from skill_evolve.optimizer.coding_agent.workspace import AgentWorkspace

MIN_CODEX_VERSION = (0, 138, 0)
_VERSION_PATTERN = re.compile(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)")


class CodexLaunchError(RuntimeError):
    """Codex could not be invoked under the required contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CodexLaunchSettings:
    binary: str = "codex"
    model: str | None = None
    timeout_ms: int = 300_000

    def __post_init__(self) -> None:
        if not self.binary.strip() or "\x00" in self.binary:
            raise ValueError("Codex binary must be non-empty")
        if self.model is not None and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model
        ) is None:
            raise ValueError("Codex model has an invalid format")
        if not 1_000 <= self.timeout_ms <= 3_600_000:
            raise ValueError("Codex timeout must be between 1s and 1h")


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def invoke_codex(
    workspace: AgentWorkspace,
    prompt: str,
    settings: CodexLaunchSettings,
    *,
    runner: ProcessRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> CodingAgentInvocationReceipt:
    """Run one non-interactive Codex turn and return only non-secret process facts."""

    binary = (
        shutil.which(settings.binary)
        if not Path(settings.binary).is_absolute()
        else settings.binary
    )
    if binary is None or not Path(binary).is_file():
        raise CodexLaunchError("Codex CLI was not found", code="codex_not_found")
    env = dict(environment) if environment is not None else _minimal_environment()
    active_runner = runner if runner is not None else _run_process
    version = _codex_version(str(binary), active_runner, env)
    args = codex_exec_argv(str(binary), workspace.root, settings.model)
    try:
        completed = active_runner(
            args,
            cwd=workspace.root,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=settings.timeout_ms / 1000,
            check=False,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise CodexLaunchError("Codex CLI timed out", code="request_timeout") from error
    workspace.verify_input_hash()
    if completed.returncode != 0:
        raise CodexLaunchError("Codex CLI exited unsuccessfully", code="process_failed")
    return CodingAgentInvocationReceipt(
        codex_cli_version=version,
        model=settings.model,
        exit_code=completed.returncode,
        input_sha256=workspace.input_sha256,
    )


def codex_exec_argv(binary: str, workspace: Path, model: str | None) -> list[str]:
    """Build the auditable argv; never add `--sandbox` to this permission-profile run."""

    config = [
        'default_permissions="skill-optimizer"',
        'permissions.skill-optimizer.filesystem.":minimal"="read"',
        'permissions.skill-optimizer.filesystem.":workspace_roots"."."="read"',
        'permissions.skill-optimizer.filesystem.":workspace_roots".output="write"',
        "permissions.skill-optimizer.network.enabled=false",
        'approval_policy="never"',
        'web_search="disabled"',
        "features.apps=false",
        "features.multi_agent=false",
        "features.memories=false",
        "features.hooks=false",
        'shell_environment_policy.inherit="none"',
        "check_for_update_on_startup=false",
    ]
    args = [str(binary), "exec"]
    for value in config:
        args.extend(("-c", value))
    args.extend(
        (
            "--strict-config",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
            "--cd",
            str(workspace),
        )
    )
    if model is not None:
        args.extend(("--model", model))
    args.append("-")
    return args


def _codex_version(binary: str, runner: ProcessRunner, env: Mapping[str, str]) -> str:
    try:
        completed = runner(
            [binary, "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CodexLaunchError(
            "Codex CLI version probe failed", code="codex_version_invalid"
        ) from error
    match = _VERSION_PATTERN.search(completed.stdout)
    if completed.returncode != 0 or match is None:
        raise CodexLaunchError("Codex CLI version is invalid", code="codex_version_invalid")
    version = tuple(int(part) for part in match.groups())
    if version < MIN_CODEX_VERSION:
        raise CodexLaunchError("Codex CLI version is unsupported", code="codex_version_unsupported")
    return ".".join(match.groups())


def _minimal_environment() -> dict[str, str]:
    names: Sequence[str] = (
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def _run_process(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run one process in a new group so a timeout can terminate its descendants."""

    timeout = kwargs.pop("timeout", None)
    input_value = kwargs.pop("input", None)
    kwargs.pop("capture_output", None)
    kwargs.pop("check", None)
    creationflags = (
        int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    )
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_value is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(input=input_value, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        process.communicate()
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            shell=False,
        )
        return
    with suppress(ProcessLookupError):
        os.kill(-process.pid, 9)
