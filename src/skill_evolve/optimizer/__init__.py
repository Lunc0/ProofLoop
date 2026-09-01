"""Bounded, advice-only LLM integration for Evolution evidence."""

from skill_evolve.optimizer.client import OptimizerClient, OptimizerClientResponse
from skill_evolve.optimizer.contracts import (
    OptimizationAdvice,
    OptimizationResult,
    OptimizerReceipt,
    OptimizerStatus,
)
from skill_evolve.optimizer.deepseek import DeepSeekChatClient
from skill_evolve.optimizer.service import advise_after_evolution

__all__ = [
    "DeepSeekChatClient",
    "OptimizationAdvice",
    "OptimizationResult",
    "OptimizerClient",
    "OptimizerClientResponse",
    "OptimizerReceipt",
    "OptimizerStatus",
    "advise_after_evolution",
]
