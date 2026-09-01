"""Provider-neutral boundary for one no-tool optimization completion."""

from __future__ import annotations

from typing import Protocol

from skill_evolve.config.models import OptimizerConfig
from skill_evolve.optimizer.contracts import ProviderUsage


class OptimizerClientResponse:
    """Non-secret model content and provider-reported usage."""

    def __init__(self, content: str, usage: ProviderUsage | None = None) -> None:
        self.content = content
        self.usage = usage


class OptimizerClient(Protocol):
    """One stateless, no-tool model request."""

    def advise(
        self, system_prompt: str, user_prompt: str, config: OptimizerConfig
    ) -> OptimizerClientResponse: ...
