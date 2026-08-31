"""Versioned, deterministic limits and patterns for trajectory compression."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CompressorPolicy:
    """All behaviour that can affect a compressed trajectory's bytes."""

    rule_version: str = "dsh-trajectory-compressor-v1"
    projection_rule_version: str = "evolution-projection-v3.1"
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
    max_projection_text_bytes: int = 2_048
    max_projection_evidence: int = 80
    max_selected_context_records: int = 12
    max_failure_episode_context_bytes: int = 512
    max_failure_episode_action_text_bytes: int = 768
    max_failure_episode_actions: int = 3
    max_failure_episode_bytes: int = 4_096
    max_skill_rule_excerpt_bytes: int = 512
    max_list_items: int = 20
    high_tool_call_count: int = 12
    high_total_tokens: int = 20_000

    def canonical_bytes(self) -> bytes:
        """Return the v1 compressor settings used in custody source evidence.

        Projection-only limits deliberately stay out of this representation so
        adding a v2 view does not change an otherwise identical v1 audit
        trajectory's policy fingerprint.
        """

        values = asdict(self)
        for key in (
            "projection_rule_version",
            "max_projection_text_bytes",
            "max_projection_evidence",
            "max_selected_context_records",
            "max_failure_episode_context_bytes",
            "max_failure_episode_action_text_bytes",
            "max_failure_episode_actions",
            "max_failure_episode_bytes",
            "max_skill_rule_excerpt_bytes",
        ):
            values.pop(key)

        return json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


DEFAULT_COMPRESSOR_POLICY = CompressorPolicy()
