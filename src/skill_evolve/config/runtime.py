"""Apply a deliberately small set of non-secret, machine-local runtime overrides."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from skill_evolve.config.dotenv import read_dotenv_value
from skill_evolve.config.models import DshConfig

DSH_BINARY_ENV = "SKILL_EVOLVE_DSH_BINARY"
DSH_SESSION_LOG_ROOT_ENV = "SKILL_EVOLVE_DSH_SESSION_LOG_ROOT"


def resolve_dsh_config(
    config: DshConfig,
    workspace: Path,
    environ: Mapping[str, str] | None = None,
) -> DshConfig:
    """Resolve the non-secret DSH executable override for one manifest workspace.

    Only the executable and session-log-root overrides are read from ``.env``.
    Provider credentials, ``DSH_HOME``, and all other DSH variables remain exclusively
    DSH's responsibility and can never be copied into the experiment manifest or run
    artifacts.
    """

    environment = os.environ if environ is None else environ
    dotenv_path = workspace / ".env"
    binary = environment.get(DSH_BINARY_ENV) or read_dotenv_value(dotenv_path, DSH_BINARY_ENV)
    session_log_root = (
        environment.get(DSH_SESSION_LOG_ROOT_ENV)
        or read_dotenv_value(dotenv_path, DSH_SESSION_LOG_ROOT_ENV)
        or config.session_log_root
        or str(Path.home() / ".dsh" / "sessions")
    )
    updates: dict[str, str] = {"session_log_root": session_log_root}
    if binary is not None:
        updates["binary"] = binary
    return DshConfig.model_validate(config.model_dump(mode="python") | updates)
