"""Confined per-invocation workspace management for the coding agent."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from skill_evolve.trajectory.compressor import canonical_json_bytes


class AgentWorkspaceError(ValueError):
    """The configured workspace or one of its artifacts is unsafe."""

    def __init__(self, message: str, *, code: str = "unsafe_workspace_root") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AgentWorkspace:
    root: Path
    input_dir: Path
    output_dir: Path
    output_real_root: Path
    output_identity: tuple[int, int]
    input_sha256: str

    def verify_input_hash(self) -> None:
        if _hash_tree(self.input_dir) != self.input_sha256:
            raise AgentWorkspaceError("agent input changed during execution", code="input_tampered")

    def read_output(self, name: str, *, max_bytes: int) -> bytes:
        if name not in {"SKILL.md", "atomic-edits.json"}:
            raise AgentWorkspaceError("unknown formal output", code="unsafe_output")
        try:
            output_real_root = self.output_dir.resolve(strict=True)
        except OSError as error:
            raise AgentWorkspaceError(
                "formal output directory changed", code="unsafe_output"
            ) from error
        if (
            self.output_dir.is_symlink()
            or output_real_root != self.output_real_root
            or _directory_identity(self.output_dir) != self.output_identity
        ):
            raise AgentWorkspaceError(
                "formal output directory changed", code="unsafe_output"
            )
        path = self.output_dir / name
        if not path.exists():
            raise AgentWorkspaceError("formal output is missing", code="missing_output")
        if path.is_symlink() or not path.is_file():
            raise AgentWorkspaceError("formal output is not a regular file", code="unsafe_output")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.output_real_root)
        except (OSError, ValueError) as error:
            raise AgentWorkspaceError(
                "formal output escaped output directory", code="unsafe_output"
            ) from error
        stat = path.stat()
        if stat.st_nlink != 1 or stat.st_size <= 0 or stat.st_size > max_bytes:
            raise AgentWorkspaceError(
                "formal output has unsafe size or links", code="unsafe_output"
            )
        value = path.read_bytes()
        if _directory_identity(self.output_dir) != self.output_identity:
            raise AgentWorkspaceError(
                "formal output directory changed", code="unsafe_output"
            )
        return value


@contextmanager
def isolated_agent_workspace(
    workspace_root: Path,
    inputs: Mapping[str, bytes],
    *,
    private_roots: tuple[Path, ...],
) -> Iterator[AgentWorkspace]:
    """Create and later remove one workspace beneath an explicitly dedicated root."""

    root = _validate_workspace_root(workspace_root, private_roots)
    path = Path(tempfile.mkdtemp(prefix="generation-", dir=root))
    try:
        input_dir = path / "input"
        output_dir = path / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        for name, value in inputs.items():
            if name not in {"SKILL.md", "skill-map.json", "evidence.json", "manifest.json"}:
                raise AgentWorkspaceError("unexpected workspace input name")
            destination = input_dir / name
            destination.write_bytes(value)
        workspace = AgentWorkspace(
            root=path,
            input_dir=input_dir,
            output_dir=output_dir,
            output_real_root=output_dir.resolve(strict=True),
            output_identity=_directory_identity(output_dir),
            input_sha256=_hash_tree(input_dir),
        )
        yield workspace
    finally:
        _remove_workspace(path, root)


def configured_workspace_root() -> Path:
    value = os.environ.get("SKILL_EVOLVE_AGENT_WORKSPACE_ROOT")
    if value is None or not value.strip():
        raise AgentWorkspaceError(
            "SKILL_EVOLVE_AGENT_WORKSPACE_ROOT is required", code="workspace_root_missing"
        )
    path = Path(value)
    if not path.is_absolute():
        raise AgentWorkspaceError("agent workspace root must be absolute")
    return path


def _validate_workspace_root(root: Path, private_roots: tuple[Path, ...]) -> Path:
    requested = root.absolute()
    if not requested.exists() or requested.is_symlink() or not requested.is_dir():
        raise AgentWorkspaceError("agent workspace root must be an existing real directory")
    resolved = requested.resolve(strict=True)
    for private in private_roots:
        private_resolved = private.absolute().resolve(strict=False)
        if _overlaps(resolved, private_resolved):
            raise AgentWorkspaceError("agent workspace root overlaps a private root")
    return resolved


def _hash_tree(root: Path) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted(root.iterdir(), key=lambda value: value.name):
        if path.is_symlink() or not path.is_file():
            raise AgentWorkspaceError(
                "input tree contains a non-regular file", code="input_tampered"
            )
        entries.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return hashlib.sha256(canonical_json_bytes(entries)).hexdigest()


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _directory_identity(path: Path) -> tuple[int, int]:
    stat = path.stat(follow_symlinks=False)
    return stat.st_dev, stat.st_ino


def _remove_workspace(path: Path, root: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise AgentWorkspaceError("refusing to remove workspace outside dedicated root") from error
    shutil.rmtree(resolved)
