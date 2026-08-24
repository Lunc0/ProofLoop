"""Deterministic runner used to verify the workflow without DSH credentials."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from skill_evolve.runner.contracts import RunInput, RunnerError, RunnerExecution, RunnerOutput


class FakeRunner:
    """Read a frozen RunnerOutput JSON file and select the requested cases."""

    def __init__(self, output_file: Path) -> None:
        self._output_file = output_file

    def run(self, input: RunInput) -> RunnerExecution:
        raw_output = self._output_file.read_bytes()
        try:
            output = RunnerOutput.model_validate_json(raw_output)
        except ValidationError as error:
            raise RunnerError(f"invalid fake runner output: {error}") from error

        cases_by_id = {case.case_id: case for case in output.cases}
        if len(cases_by_id) != len(output.cases):
            raise RunnerError("fake runner output contains duplicate case_id values")
        requested_ids = [case.id for case in input.cases]
        missing = [case_id for case_id in requested_ids if case_id not in cases_by_id]
        if missing:
            raise RunnerError(
                f"fake runner output is missing requested cases: {', '.join(missing)}"
            )
        selected = tuple(cases_by_id[case_id] for case_id in requested_ids)
        return RunnerExecution(
            output=RunnerOutput(schema_version="v1", cases=selected), raw_output=raw_output
        )
