"""Apply a deliberately small set of non-secret, machine-local runtime overrides."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from skill_evolve.config.models import DshConfig

DSH_BINARY_ENV = "SKILL_EVOLVE_DSH_BINARY"


def resolve_dsh_config(
    config: DshConfig,
    workspace: Path,
    environ: Mapping[str, str] | None = None,
) -> DshConfig:
    """Resolve the non-secret DSH executable override for one manifest workspace.

    Only ``SKILL_EVOLVE_DSH_BINARY`` is read from ``.env``. Provider credentials,
    ``DSH_HOME``, and all other DSH variables remain exclusively DSH's responsibility
    and can never be copied into the experiment manifest or run artifacts.
    """

    environment = os.environ if environ is None else environ
    binary = environment.get(DSH_BINARY_ENV) or _read_dotenv_value(workspace, DSH_BINARY_ENV)
    if binary is None:
        return config
    return DshConfig.model_validate(config.model_dump(mode="python") | {"binary": binary})


def _read_dotenv_value(workspace: Path, key: str) -> str | None:
    """Read one exact non-secret key without expanding or loading the full dotenv file."""

    dotenv_path = workspace / ".env"
    if not dotenv_path.is_file():
        return None
    with dotenv_path.open(encoding="utf-8") as dotenv_file:
        for line in dotenv_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            declared_key, raw_value = stripped.split("=", maxsplit=1)
            if declared_key.strip().removeprefix("export ").strip() != key:
                continue
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    return None
