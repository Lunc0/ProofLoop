"""Bounded LLM advice and semantic planning for Evolution evidence."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "DeepSeekChatClient",
    "OptimizationAdvice",
    "OptimizationResult",
    "OptimizerClient",
    "OptimizerClientResponse",
    "OptimizerReceipt",
    "OptimizerStatus",
    "advise_after_evolution",
    "materialize_after_evolution",
]

_EXPORTS = {
    "DeepSeekChatClient": ("skill_evolve.optimizer.deepseek", "DeepSeekChatClient"),
    "OptimizationAdvice": ("skill_evolve.optimizer.contracts", "OptimizationAdvice"),
    "OptimizationResult": ("skill_evolve.optimizer.contracts", "OptimizationResult"),
    "OptimizerClient": ("skill_evolve.optimizer.client", "OptimizerClient"),
    "OptimizerClientResponse": ("skill_evolve.optimizer.client", "OptimizerClientResponse"),
    "OptimizerReceipt": ("skill_evolve.optimizer.contracts", "OptimizerReceipt"),
    "OptimizerStatus": ("skill_evolve.optimizer.contracts", "OptimizerStatus"),
    "advise_after_evolution": ("skill_evolve.optimizer.service", "advise_after_evolution"),
    "materialize_after_evolution": (
        "skill_evolve.optimizer.materialization_service",
        "materialize_after_evolution",
    ),
}


def __getattr__(name: str) -> Any:
    """Defer exports that depend on candidate and registry modules."""

    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    return getattr(import_module(module_name), attribute_name)
