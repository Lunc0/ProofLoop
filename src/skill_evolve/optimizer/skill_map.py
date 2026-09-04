"""Deterministic, fail-closed structure mapping for untrusted Skill Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass

from skill_evolve.optimizer.edit_plan_contracts import (
    HeadingSkillBlock,
    HeadingSkillMap,
    SkillBlock,
    SkillBlockType,
    SkillLayer,
    SkillMap,
    SkillPermission,
)

_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_PATTERN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_LIST_PATTERN = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_TABLE_SEPARATOR_PATTERN = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)

_LOCKED_HEADINGS = frozenset(
    {
        "runtime contract",
        "runtime constraints",
        "safety constraints",
        "security constraints",
        "final output contract",
    }
)
_GUARDED_HEADINGS = frozenset(
    {
        "purpose and scope",
        "purpose",
        "scope",
        "activation",
        "activation and scope",
        "when to use",
    }
)
_EVOLVABLE_HEADINGS = frozenset(
    {
        "core strategy",
        "task understanding",
        "task and context parsing",
        "workflow",
        "planning and order",
        "reasoning guidelines",
        "domain reasoning",
        "tool execution",
        "validation",
        "recovery",
        "completion",
        "final submission",
    }
)
_EXPERIMENTAL_HEADINGS = frozenset(
    {
        "heuristics and examples",
        "heuristics",
        "examples",
        "common errors",
        "common mistakes",
        "tips",
    }
)


@dataclass(frozen=True)
class ParsedHeadingBlock:
    """Private source range for one compact heading-map entry."""

    block: HeadingSkillBlock
    start: int
    end: int
    parent_id: str | None


@dataclass(frozen=True)
class _DraftHeading:
    title: str
    level: int
    start: int
    block_id: str
    parent_id: str | None


def build_skill_map(skill_bytes: bytes, *, max_input_bytes: int = 524_288) -> HeadingSkillMap:
    """Build the compact v3 title map exposed to an optimizer model."""

    sections = parse_heading_blocks(skill_bytes, max_input_bytes=max_input_bytes)
    return HeadingSkillMap(blocks=tuple(section.block for section in sections))


def parse_heading_blocks(
    skill_bytes: bytes, *, max_input_bytes: int = 524_288
) -> tuple[ParsedHeadingBlock, ...]:
    """Parse title sections and keep their source ranges private to callers."""

    text, lines, start_index = _read_skill_text(skill_bytes, max_input_bytes)
    offsets = _line_offsets(lines)
    drafts: list[_DraftHeading] = []
    stack: list[_DraftHeading] = []
    child_counts: dict[str | None, int] = {}
    index = start_index
    while index < len(lines):
        stripped = _line_text(lines[index])
        fence = _FENCE_PATTERN.match(stripped)
        if fence is not None:
            index = _find_fence_end(lines, index, fence.group(1)) + 1
            continue
        heading = _HEADING_PATTERN.fullmatch(stripped)
        if heading is None:
            index += 1
            continue

        level = len(heading.group(1))
        title = heading.group(2).strip()
        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        if level == 1:
            parent = None
        elif parent is None:
            raise ValueError("Skill headings must begin beneath a level-one heading")
        parent_id = parent.block_id if parent is not None else None
        child_counts[parent_id] = child_counts.get(parent_id, 0) + 1
        ordinal = child_counts[parent_id]
        block_id = f"b{ordinal}" if parent is None else f"{parent_id}.{ordinal}"
        draft = _DraftHeading(
            title=title,
            level=level,
            start=offsets[index],
            block_id=block_id,
            parent_id=parent_id,
        )
        drafts.append(draft)
        stack.append(draft)
        index += 1

    if not drafts or not any(draft.level == 1 for draft in drafts):
        raise ValueError("Skill content must contain a level-one heading")

    ends = [len(text)] * len(drafts)
    open_indices: list[int] = []
    for index, draft in enumerate(drafts):
        while open_indices and drafts[open_indices[-1]].level >= draft.level:
            ends[open_indices.pop()] = draft.start
        open_indices.append(index)
    return tuple(
        ParsedHeadingBlock(
            block=HeadingSkillBlock(
                block_id=draft.block_id,
                heading=draft.title,
                heading_level=draft.level,
                permission=_heading_permission(draft.title),
            ),
            start=draft.start,
            end=ends[index],
            parent_id=draft.parent_id,
        )
        for index, draft in enumerate(drafts)
    )


def build_legacy_skill_map(skill_bytes: bytes, *, max_input_bytes: int = 524_288) -> SkillMap:
    """Build the retired v2 paragraph/list map for persisted v1 edit plans only."""

    _, lines, index = _read_skill_text(skill_bytes, max_input_bytes)
    blocks: list[SkillBlock] = []
    headings: dict[int, str] = {}
    while index < len(lines):
        stripped = _line_text(lines[index])
        if not stripped:
            index += 1
            continue
        heading = _HEADING_PATTERN.fullmatch(stripped)
        if heading is not None:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            headings = {key: value for key, value in headings.items() if key < level}
            headings[level] = title
            index += 1
            continue
        path = tuple(headings[key] for key in sorted(headings))
        fence = _FENCE_PATTERN.match(stripped)
        if fence is not None:
            index = _find_fence_end(lines, index, fence.group(1)) + 1
            continue
        if _is_table_start(lines, index):
            end = index + 1
            while end + 1 < len(lines) and "|" in _line_text(lines[end + 1]):
                end += 1
            index = end + 1
            continue
        if _LIST_PATTERN.match(stripped) is not None:
            end = index
            while end + 1 < len(lines):
                candidate = _line_text(lines[end + 1])
                if not candidate or _HEADING_PATTERN.fullmatch(candidate):
                    break
                if _FENCE_PATTERN.match(candidate) or _is_table_start(lines, end + 1):
                    break
                if _LIST_PATTERN.match(candidate) is None and not lines[end + 1].startswith(
                    (" ", "\t")
                ):
                    break
                end += 1
            layer, permission = _classify(path, "list")
            blocks.append(
                _make_legacy_block(index, end, path, "list", layer, permission, len(blocks) + 1)
            )
            index = end + 1
            continue
        end = index
        while end + 1 < len(lines):
            candidate = _line_text(lines[end + 1])
            if not candidate:
                break
            if (
                _HEADING_PATTERN.fullmatch(candidate)
                or _FENCE_PATTERN.match(candidate)
                or _LIST_PATTERN.match(candidate)
                or _is_table_start(lines, end + 1)
            ):
                break
            end += 1
        layer, permission = _classify(path, "paragraph")
        blocks.append(
            _make_legacy_block(index, end, path, "paragraph", layer, permission, len(blocks) + 1)
        )
        index = end + 1
    return SkillMap(blocks=tuple(blocks))


def _read_skill_text(skill_bytes: bytes, max_input_bytes: int) -> tuple[str, list[str], int]:
    if not skill_bytes:
        raise ValueError("Skill content must not be empty")
    if len(skill_bytes) > max_input_bytes:
        raise ValueError("Skill content exceeds the configured input limit")
    try:
        text = skill_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Skill content must be valid UTF-8") from error
    if "\x00" in text:
        raise ValueError("Skill content must not contain NUL")
    lines = text.splitlines(keepends=True)
    if not lines:
        raise ValueError("Skill content must contain at least one line")
    index = 0
    if _line_text(lines[0]) == "---":
        index = _find_frontmatter_end(lines) + 1
    return text, lines, index


def _line_text(line: str) -> str:
    return line.rstrip("\r\n").strip()


def _line_offsets(lines: list[str]) -> tuple[int, ...]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return tuple(offsets)


def _find_frontmatter_end(lines: list[str]) -> int:
    for index in range(1, len(lines)):
        if _line_text(lines[index]) == "---":
            return index
    raise ValueError("Skill frontmatter is not closed")


def _find_fence_end(lines: list[str], start: int, marker: str) -> int:
    character = marker[0]
    minimum = len(marker)
    closing = re.compile(rf"^[ \t]*{re.escape(character)}{{{minimum},}}[ \t]*$")
    for index in range(start + 1, len(lines)):
        if closing.fullmatch(lines[index].rstrip("\r\n")):
            return index
    raise ValueError("Skill fenced code block is not closed")


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in _line_text(lines[index])
        and _TABLE_SEPARATOR_PATTERN.fullmatch(_line_text(lines[index + 1])) is not None
    )


def _heading_permission(title: str) -> SkillPermission:
    normalized = _normalize_heading(title)
    if normalized in _LOCKED_HEADINGS:
        return "locked"
    if normalized in _EXPERIMENTAL_HEADINGS:
        return "experimental"
    if normalized in _EVOLVABLE_HEADINGS:
        return "evolvable"
    return "guarded"


def _classify(
    heading_path: tuple[str, ...], block_type: SkillBlockType
) -> tuple[SkillLayer, SkillPermission]:
    if block_type == "frontmatter":
        return "hard_contract", "locked"
    normalized = tuple(_normalize_heading(value) for value in heading_path)
    if any(value in _LOCKED_HEADINGS for value in normalized):
        return "hard_contract", "locked"
    if any(value in _EXPERIMENTAL_HEADINGS for value in normalized):
        return "heuristics", "experimental"
    if any(value in _EVOLVABLE_HEADINGS for value in normalized):
        return "core_strategy", "evolvable"
    if any(value in _GUARDED_HEADINGS for value in normalized):
        return "unknown", "guarded"
    return "unknown", "guarded"


def _normalize_heading(value: str) -> str:
    lowered = value.casefold().strip()
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _make_legacy_block(
    start: int,
    end: int,
    heading_path: tuple[str, ...],
    block_type: SkillBlockType,
    layer: SkillLayer,
    permission: SkillPermission,
    index: int,
) -> SkillBlock:
    return SkillBlock(
        block_id=f"b{index:02d}",
        section=heading_path[-1] if heading_path else "",
        block_type=block_type,
        start_line=start + 1,
        end_line=end + 1,
        layer=layer,
        permission=permission,
    )
