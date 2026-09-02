"""Deterministic, fail-closed structure mapping for untrusted Skill Markdown."""

from __future__ import annotations

import hashlib
import re

from skill_evolve.optimizer.edit_plan_contracts import (
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


def build_skill_map(skill_bytes: bytes, *, max_input_bytes: int = 524_288) -> SkillMap:
    """Map one exact UTF-8 Skill byte string without interpreting its instructions."""

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
    blocks: list[SkillBlock] = []
    headings: dict[int, str] = {}
    index = 0
    skill_sha256 = hashlib.sha256(skill_bytes).hexdigest()

    if _line_text(lines[0]) == "---":
        closing = _find_frontmatter_end(lines)
        blocks.append(
            _make_block(
                skill_sha256,
                lines,
                0,
                closing,
                (),
                "frontmatter",
                "hard_contract",
                "locked",
            )
        )
        index = closing + 1

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
            path = tuple(headings[key] for key in sorted(headings))
            layer, permission = _classify(path, "heading")
            blocks.append(
                _make_block(
                    skill_sha256,
                    lines,
                    index,
                    index,
                    path,
                    "heading",
                    layer,
                    permission,
                )
            )
            index += 1
            continue

        path = tuple(headings[key] for key in sorted(headings))
        fence = _FENCE_PATTERN.match(stripped)
        if fence is not None:
            end = _find_fence_end(lines, index, fence.group(1))
            layer, permission = _classify(path, "fenced_code")
            blocks.append(
                _make_block(
                    skill_sha256,
                    lines,
                    index,
                    end,
                    path,
                    "fenced_code",
                    layer,
                    permission,
                )
            )
            index = end + 1
            continue

        if _is_table_start(lines, index):
            end = index + 1
            while end + 1 < len(lines) and "|" in _line_text(lines[end + 1]):
                end += 1
            layer, permission = _classify(path, "table")
            blocks.append(
                _make_block(
                    skill_sha256,
                    lines,
                    index,
                    end,
                    path,
                    "table",
                    layer,
                    permission,
                )
            )
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
                _make_block(
                    skill_sha256,
                    lines,
                    index,
                    end,
                    path,
                    "list",
                    layer,
                    permission,
                )
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
            _make_block(
                skill_sha256,
                lines,
                index,
                end,
                path,
                "paragraph",
                layer,
                permission,
            )
        )
        index = end + 1

    if not blocks:
        raise ValueError("Skill content did not produce any stable blocks")
    return SkillMap(
        skill_sha256=skill_sha256,
        line_count=len(lines),
        blocks=tuple(blocks),
    )


def _line_text(line: str) -> str:
    return line.rstrip("\r\n").strip()


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


def _make_block(
    skill_sha256: str,
    lines: list[str],
    start: int,
    end: int,
    heading_path: tuple[str, ...],
    block_type: SkillBlockType,
    layer: SkillLayer,
    permission: SkillPermission,
) -> SkillBlock:
    content = "".join(lines[start : end + 1]).encode("utf-8")
    content_sha256 = hashlib.sha256(content).hexdigest()
    identity = "\x1f".join(
        (
            skill_sha256,
            "/".join(heading_path),
            block_type,
            str(start + 1),
            str(end + 1),
            content_sha256,
        )
    ).encode("utf-8")
    block_id = f"block-{hashlib.sha256(identity).hexdigest()[:16]}"
    return SkillBlock(
        block_id=block_id,
        heading_path=heading_path,
        block_type=block_type,
        start_line=start + 1,
        end_line=end + 1,
        content_sha256=content_sha256,
        layer=layer,
        permission=permission,
    )
