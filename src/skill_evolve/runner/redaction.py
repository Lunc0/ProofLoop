"""Redact sensitive values before runner evidence is persisted."""

from __future__ import annotations

import re
from typing import Any

_ASSIGNMENT_SECRET_PATTERN = re.compile(
    r"(?im)(\b(?:api[_-]?key|password|secret|token|authorization)\b\s*[:=]\s*)([^\s'\"]+)"
)
_BEARER_SECRET_PATTERN = re.compile(r"(?i)(\bbearer\s+)([^\s'\"]+)")
_KEY_PREFIX_SECRET_PATTERN = re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{12,}\b")
_SENSITIVE_KEY_FRAGMENTS = ("api_key", "apikey", "password", "secret", "authorization")


def redact_text(value: str) -> str:
    """Remove common credential forms without changing unrelated evidence."""

    value = _ASSIGNMENT_SECRET_PATTERN.sub(r"\1<redacted>", value)
    value = _BEARER_SECRET_PATTERN.sub(r"\1<redacted>", value)
    return _KEY_PREFIX_SECRET_PATTERN.sub("<redacted>", value)


def redact_json_value(value: Any, key: str | None = None) -> Any:
    """Recursively redact both credential-shaped keys and inline credential text."""

    if key is not None:
        normalised = key.lower().replace("-", "_")
        is_singular_token = "token" in normalised and not normalised.endswith("tokens")
        has_sensitive_fragment = any(
            fragment in normalised for fragment in _SENSITIVE_KEY_FRAGMENTS
        )
        if has_sensitive_fragment or is_singular_token:
            return "<redacted>"
    if isinstance(value, dict):
        return {
            str(child_key): redact_json_value(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_json_value(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
