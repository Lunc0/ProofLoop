"""Versioned, deterministic limits and patterns for trajectory compression."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CompressorPolicy:
    """All behaviour that can affect a compressed trajectory's bytes."""

    rule_version: str = "dsh-trajectory-compressor-v1"
    max_events: int = 10_000
    max_assistant_text_bytes: int = 4_096
    max_tool_arguments_bytes: int = 4_096
    max_tool_result_bytes: int = 8_192
    max_summary_text_bytes: int = 1_024
    max_context_bytes: int = 16_384
    max_context_source_bytes: int = 1_048_576
    max_prompt_bytes: int = 8_192
    max_skill_bytes: int = 24_576
    max_trajectory_bytes: int = 524_288
    max_agent_input_bytes: int = 2_097_152
    max_list_items: int = 20
    high_tool_call_count: int = 12
    high_total_tokens: int = 20_000

    def canonical_bytes(self) -> bytes:
        """Return the stable representation used in source evidence."""

        return json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


DEFAULT_COMPRESSOR_POLICY = CompressorPolicy()
