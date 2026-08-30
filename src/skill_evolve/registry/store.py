"""Append-only filesystem storage for run evidence and rerunnable inputs."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from skill_evolve.candidate.contracts import ResolvedCandidate
from skill_evolve.config.loader import ResolvedManifest
from skill_evolve.subject import RunSubject


class RunNotFoundError(FileNotFoundError):
    """The requested run id is absent from this workspace registry."""


@dataclass(frozen=True)
class RunDirectory:
    run_id: str
    path: Path

    def write_json(self, relative_path: str, value: Any) -> Path:
        destination = self._destination(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        return destination

    def write_text(self, relative_path: str, value: str) -> Path:
        destination = self._destination(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value, encoding="utf-8")
        return destination

    def write_bytes(self, relative_path: str, value: bytes) -> Path:
        destination = self._destination(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value)
        return destination

    def materialise_manifest(self, resolved: ResolvedManifest) -> Path:
        """Copy every declared input and rewrite the manifest to reference the copies."""

        inputs = self.path / "inputs"
        inputs.mkdir(exist_ok=True)
        replacements: dict[Path, str] = {}
        ordered_inputs: list[tuple[str, Path]] = [
            ("database-schema", resolved.schema_file),
            ("database-seed", resolved.seed_file),
        ]
        if resolved.fake_output is not None:
            ordered_inputs.append(("dsh-fake-output", resolved.fake_output))
        if resolved.candidate_digest_file is not None:
            ordered_inputs.append(("candidate", resolved.candidate_digest_file))
        if resolved.profile_patch is not None:
            ordered_inputs.append(("dsh-profile-patch", resolved.profile_patch))
        ordered_inputs.extend(
            (f"tool-{tool_id}", path) for tool_id, path in resolved.tool_manifests
        )
        ordered_inputs.extend(
            (f"case-{case_id}-context-{index}", path)
            for case_id, paths in resolved.case_context_files
            for index, path in enumerate(paths, start=1)
        )
        for index, (label, source) in enumerate(ordered_inputs, start=1):
            destination = inputs / f"{index:02d}-{label}-{source.name}"
            shutil.copyfile(source, destination)
            replacements[source] = destination.relative_to(self.path).as_posix()

        data = resolved.manifest.model_dump(mode="json", by_alias=True)
        _replace_manifest_paths(data, resolved, replacements)
        output = self.path / "manifest.resolved.yaml"
        output.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return output

    def materialise_candidate(self, candidate: ResolvedCandidate) -> None:
        """Retain exactly the verified Skill bytes; NOTICE.md is never a run input."""

        self.write_bytes("inputs/candidate-SKILL.md", candidate.skill_bytes)
        self.write_json("inputs/candidate.json", candidate.public_binding())

    def materialise_subject(self, subject: RunSubject) -> None:
        """Retain generic evaluated bytes without candidate-store provenance."""

        self.write_bytes(f"inputs/subject-{subject.filename}", subject.content)
        self.write_json("inputs/subject.json", subject.public_binding())

    def _destination(self, relative_path: str) -> Path:
        destination = (self.path / relative_path).resolve()
        try:
            destination.relative_to(self.path.resolve())
        except ValueError as error:
            raise ValueError("run artifact path must stay inside the run directory") from error
        return destination


class RunStore:
    """Creates and locates immutable run directories under a workspace root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def create(self) -> RunDirectory:
        self._root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
        path = self._root / run_id
        path.mkdir()
        return RunDirectory(run_id=run_id, path=path)

    def open(self, run_id: str) -> RunDirectory:
        path = (self._root / run_id).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as error:
            raise RunNotFoundError(f"invalid run id: {run_id}") from error
        if not path.is_dir():
            raise RunNotFoundError(f"run not found: {run_id}")
        return RunDirectory(run_id=run_id, path=path)


def _replace_manifest_paths(
    data: dict[str, Any], resolved: ResolvedManifest, replacements: dict[Path, str]
) -> None:
    candidate = data["candidate"]
    if resolved.candidate_digest_file is not None:
        candidate["digest_file"] = replacements[resolved.candidate_digest_file]
    dsh = data["environment"]["dsh"]
    if resolved.profile_patch is not None:
        dsh["profile_patch"] = replacements[resolved.profile_patch]
    if resolved.fake_output is not None:
        dsh["fake_output"] = replacements[resolved.fake_output]
    database = data["environment"]["database"]
    database["schema"] = replacements[resolved.schema_file]
    database["seed"] = replacements[resolved.seed_file]
    for tool, (tool_id, source) in zip(
        data["environment"]["tools"], resolved.tool_manifests, strict=True
    ):
        if tool["id"] != tool_id:
            raise ValueError("tool manifest ordering changed during resolution")
        tool["manifest"] = replacements[source]
    for case, (case_id, sources) in zip(
        data["cases"], resolved.case_context_files, strict=True
    ):
        if case["id"] != case_id:
            raise ValueError("case context ordering changed during resolution")
        case["context_files"] = [replacements[source] for source in sources]
