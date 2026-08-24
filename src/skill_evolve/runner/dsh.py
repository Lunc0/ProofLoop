"""Reserved external-process adapter for a pinned real DSH runtime."""

from __future__ import annotations

from skill_evolve.runner.contracts import RunInput, RunnerError, RunnerExecution


class DshRunner:
    """Placeholder for the explicitly deferred real DSH integration."""

    def run(self, input: RunInput) -> RunnerExecution:
        del input
        raise RunnerError("the real DSH runner is not implemented in v0.1; use dsh.runner: fake")
