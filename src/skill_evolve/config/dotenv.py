"""Read one declared value from a local dotenv file without loading its environment."""

from __future__ import annotations

from pathlib import Path


def read_dotenv_value(dotenv_path: Path, key: str) -> str | None:
    """Return one literal dotenv value without expanding, exporting, or persisting it."""

    try:
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
    except OSError:
        return None
    return None
