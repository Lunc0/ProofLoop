"""Deterministic, agent-safe projections of redacted DSH trajectory evidence."""

from skill_evolve.trajectory.agent_input import build_evolution_agent_input
from skill_evolve.trajectory.compressor import (
    TrajectoryCompressionError,
    compress_session_jsonl,
)
from skill_evolve.trajectory.policy import DEFAULT_COMPRESSOR_POLICY, CompressorPolicy

__all__ = [
    "DEFAULT_COMPRESSOR_POLICY",
    "CompressorPolicy",
    "build_evolution_agent_input",
    "TrajectoryCompressionError",
    "compress_session_jsonl",
]
