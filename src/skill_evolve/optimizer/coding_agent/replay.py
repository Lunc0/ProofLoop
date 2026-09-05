"""Deterministic byte-bound replay for coding-agent atomic Skill edits."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass

from skill_evolve.optimizer.coding_agent.contracts import (
    AtomicEditSource,
    AtomicSkillEditDeclaration,
    BoundAtomicEdit,
    BoundAtomicSkillEdits,
)
from skill_evolve.optimizer.edit_plan_renderer import apply_unified_diff
from skill_evolve.optimizer.skill_map import build_skill_map, parse_heading_blocks
from skill_evolve.trajectory.compressor import canonical_json_bytes

MAX_SKILL_BYTES = 512 * 1024
MAX_REPLACEMENT_BYTES = 256 * 1024
_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_PATTERN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_PRIVATE_PATH_PATTERN = re.compile(r"(?i)(?:[a-z]:[\\/]|/(?:home|users|tmp|var|etc)(?:/|\b))")
_SECRET_PATTERN = re.compile(r"(?i)(?:api[_-]?key|secret|password)\s*[:=]")


class AtomicReplayError(ValueError):
    """An atomic declaration cannot be safely bound or replayed."""

    def __init__(self, message: str, *, code: str = "invalid_binding") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AtomicReplayResult:
    bound_edits: BoundAtomicSkillEdits
    full_skill_bytes: bytes
    full_patch_bytes: bytes
    singleton_skill_bytes: dict[str, bytes]
    singleton_patch_bytes: dict[str, bytes]


def bind_atomic_edits(
    parent_bytes: bytes,
    declaration: AtomicSkillEditDeclaration,
    expected_full_bytes: bytes,
    *,
    run_id: str,
    parent_candidate_id: str,
    evidence_bundle_sha256: str,
    invocation_receipt_sha256: str,
    allowed_evidence_refs: frozenset[str],
) -> AtomicReplayResult:
    """Bind agent declarations to one parent and prove exact full-output equivalence."""

    parent_text = _validate_skill_bytes(parent_bytes, label="parent")
    _validate_skill_bytes(expected_full_bytes, label="output")
    skill_map = build_skill_map(parent_bytes, max_input_bytes=MAX_SKILL_BYTES)
    skill_map_sha256 = _sha256(canonical_json_bytes(skill_map.model_dump(mode="json")))
    sections = {section.block.block_id: section for section in parse_heading_blocks(parent_bytes)}
    newline = _newline_for(parent_text)
    total_replacement_bytes = sum(
        len(edit.replacement_markdown.encode("utf-8")) for edit in declaration.edits
    )
    if total_replacement_bytes > MAX_REPLACEMENT_BYTES:
        raise AtomicReplayError("replacement byte budget exceeded", code="invalid_declaration")

    targets: list[str] = []
    bound: list[BoundAtomicEdit] = []
    for index, edit in enumerate(declaration.edits, start=1):
        if not set(edit.evidence_refs).issubset(allowed_evidence_refs):
            raise AtomicReplayError("edit refers to unknown evidence", code="invalid_declaration")
        section = sections.get(edit.target_block_id)
        if section is None:
            raise AtomicReplayError("edit refers to an unknown block")
        if section.block.permission not in {"evolvable", "experimental"}:
            raise AtomicReplayError("edit target is not safely editable", code="locked_target")
        if any(_block_paths_overlap(edit.target_block_id, target) for target in targets):
            raise AtomicReplayError("atomic edit scopes overlap", code="overlapping_edits")
        replacement_bytes = edit.replacement_markdown.encode("utf-8")
        _validate_fragment(edit.replacement_markdown, section.block.heading_level, newline)
        _validate_safe_text(edit.replacement_markdown)
        start = len(parent_text[: section.start].encode("utf-8"))
        end = len(parent_text[: section.end].encode("utf-8"))
        before = parent_bytes[start:end]
        if before == replacement_bytes:
            raise AtomicReplayError("atomic edit produces no change", code="invalid_declaration")
        targets.append(edit.target_block_id)
        bound.append(
            BoundAtomicEdit(
                edit_id=f"edit-{index:03d}",
                operation="replace_block",
                target_block_id=edit.target_block_id,
                resolved_start_byte=start,
                resolved_end_byte=end,
                expected_before_sha256=_sha256(before),
                replacement_markdown=edit.replacement_markdown,
                replacement_sha256=_sha256(replacement_bytes),
                hypothesis=edit.hypothesis,
                evidence_refs=edit.evidence_refs,
            )
        )

    source = AtomicEditSource(
        run_id=run_id,
        parent_candidate_id=parent_candidate_id,
        parent_skill_sha256=_sha256(parent_bytes),
        skill_map_sha256=skill_map_sha256,
        evidence_bundle_sha256=evidence_bundle_sha256,
        invocation_receipt_sha256=invocation_receipt_sha256,
    )
    plan = BoundAtomicSkillEdits(source=source, edits=tuple(bound))
    result = replay_bound_edits(parent_bytes, plan)
    if result.full_skill_bytes != expected_full_bytes:
        raise AtomicReplayError(
            "declared edits do not reproduce output/SKILL.md", code="undeclared_change"
        )
    return result


def replay_bound_edits(
    parent_bytes: bytes, plan: BoundAtomicSkillEdits
) -> AtomicReplayResult:
    """Replay an already bound plan while rechecking every persisted source binding."""

    parent_text = _validate_skill_bytes(parent_bytes, label="parent")
    if _sha256(parent_bytes) != plan.source.parent_skill_sha256:
        raise AtomicReplayError("parent Skill hash does not match bound edits")
    skill_map = build_skill_map(parent_bytes, max_input_bytes=MAX_SKILL_BYTES)
    if _sha256(canonical_json_bytes(skill_map.model_dump(mode="json"))) != (
        plan.source.skill_map_sha256
    ):
        raise AtomicReplayError("SkillMap hash does not match bound edits")

    ranges: list[tuple[int, int]] = []
    for edit in plan.edits:
        start, end = edit.resolved_start_byte, edit.resolved_end_byte
        if end > len(parent_bytes) or _sha256(parent_bytes[start:end]) != (
            edit.expected_before_sha256
        ):
            raise AtomicReplayError("before bytes do not match bound edit")
        if any(
            start < existing_end and existing_start < end
            for existing_start, existing_end in ranges
        ):
            raise AtomicReplayError("bound edit byte ranges overlap", code="overlapping_edits")
        ranges.append((start, end))

    singleton_skills: dict[str, bytes] = {}
    singleton_patches: dict[str, bytes] = {}
    for edit in plan.edits:
        child = _apply(parent_bytes, (edit,))
        _validate_rendered_child(child, parent_text)
        patch = _unified_diff(parent_bytes, child)
        if not patch or apply_unified_diff(parent_bytes, patch) != child:
            raise AtomicReplayError("singleton patch does not replay")
        singleton_skills[edit.edit_id] = child
        singleton_patches[edit.edit_id] = patch

    full = _apply(parent_bytes, plan.edits)
    _validate_rendered_child(full, parent_text)
    full_patch = _unified_diff(parent_bytes, full)
    if not full_patch or apply_unified_diff(parent_bytes, full_patch) != full:
        raise AtomicReplayError("full patch does not replay")
    return AtomicReplayResult(
        bound_edits=plan,
        full_skill_bytes=full,
        full_patch_bytes=full_patch,
        singleton_skill_bytes=singleton_skills,
        singleton_patch_bytes=singleton_patches,
    )


def _apply(parent: bytes, edits: tuple[BoundAtomicEdit, ...]) -> bytes:
    result = parent
    for edit in sorted(edits, key=lambda value: value.resolved_start_byte, reverse=True):
        replacement = edit.replacement_markdown.encode("utf-8")
        result = result[: edit.resolved_start_byte] + replacement + result[edit.resolved_end_byte :]
    return result


def _validate_rendered_child(child: bytes, parent_text: str) -> None:
    child_text = _validate_skill_bytes(child, label="rendered")
    if _newline_for(child_text) != _newline_for(parent_text):
        raise AtomicReplayError("rendered Skill changed newline convention", code="invalid_skill")
    _validate_safe_text(child_text)
    try:
        build_skill_map(child, max_input_bytes=MAX_SKILL_BYTES)
    except ValueError as error:
        raise AtomicReplayError(
            "rendered Skill is structurally invalid", code="invalid_skill"
        ) from error


def _validate_skill_bytes(value: bytes, *, label: str) -> str:
    if not value or len(value) > MAX_SKILL_BYTES or value.startswith(b"\xef\xbb\xbf"):
        raise AtomicReplayError(f"{label} Skill has invalid bytes", code="invalid_skill")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AtomicReplayError(f"{label} Skill must be UTF-8", code="invalid_skill") from error
    if "\x00" in text or ("\r" in text.replace("\r\n", "")):
        raise AtomicReplayError(f"{label} Skill has invalid text", code="invalid_skill")
    return text


def _validate_fragment(fragment: str, level: int, newline: str) -> None:
    if _newline_for(fragment) != newline:
        raise AtomicReplayError(
            "replacement changed newline convention", code="invalid_declaration"
        )
    first_heading = False
    fence: str | None = None
    for line in fragment.splitlines():
        stripped = line.strip()
        if fence is not None:
            if re.fullmatch(rf"{re.escape(fence[0])}{{{len(fence)},}}", stripped):
                fence = None
            continue
        heading = _HEADING_PATTERN.fullmatch(stripped)
        if not first_heading:
            if not stripped:
                continue
            if heading is None or len(heading.group(1)) != level:
                raise AtomicReplayError(
                    "replacement must begin at the target heading level",
                    code="invalid_declaration",
                )
            first_heading = True
            continue
        marker = _FENCE_PATTERN.match(stripped)
        if marker is not None:
            fence = marker.group(1)
        elif heading is not None and len(heading.group(1)) <= level:
            raise AtomicReplayError(
                "replacement exceeds its heading boundary", code="invalid_declaration"
            )
    if not first_heading or not fragment.endswith(newline):
        raise AtomicReplayError(
            "replacement must contain a heading and preserve trailing newline",
            code="invalid_declaration",
        )


def _validate_safe_text(value: str) -> None:
    if "*** Begin Patch" in value or "diff --git " in value:
        raise AtomicReplayError("Skill contains a patch marker", code="invalid_skill")
    if _PRIVATE_PATH_PATTERN.search(value) is not None:
        raise AtomicReplayError("Skill contains a private path", code="invalid_skill")
    if _SECRET_PATTERN.search(value) is not None:
        raise AtomicReplayError("Skill contains secret-like text", code="invalid_skill")


def _newline_for(text: str) -> str:
    if "\r\n" in text:
        if "\n" in text.replace("\r\n", ""):
            raise AtomicReplayError("mixed newline conventions are forbidden", code="invalid_skill")
        return "\r\n"
    return "\n"


def _block_paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}.") or right.startswith(f"{left}.")


def _unified_diff(parent: bytes, child: bytes) -> bytes:
    parent_text = parent.decode("utf-8")
    child_text = child.decode("utf-8")
    return "".join(
        difflib.unified_diff(
            parent_text.splitlines(keepends=True),
            child_text.splitlines(keepends=True),
            fromfile="a/SKILL.md",
            tofile="b/SKILL.md",
        )
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
