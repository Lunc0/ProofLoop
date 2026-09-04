"""Deterministic, non-executable rendering of semantic Skill edits."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass

from skill_evolve.optimizer.edit_plan_contracts import (
    BlockEdit,
    ConstraintRule,
    HeuristicRule,
    RecoveryRule,
    SemanticEdit,
    SkillBlock,
    SkillEditPlan,
    SkillEditPlanV2,
    WorkflowRule,
)
from skill_evolve.optimizer.skill_map import (
    ParsedHeadingBlock,
    build_legacy_skill_map,
    build_skill_map,
    parse_heading_blocks,
)
from skill_evolve.trajectory.compressor import canonical_json_bytes

RENDER_POLICY_VERSION = "skill-edit-render-policy-v3"
_LEGACY_RENDER_POLICY_VERSION = "skill-edit-render-policy-v2"
_HUNK_PATTERN = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_PRIVATE_PATH_PATTERN = re.compile(r"(?i)(?:[a-z]:[\\/]|/(?:home|users|tmp|var|etc)(?:/|\b))")
_SECRET_PATTERN = re.compile(r"(?i)(?:api[_-]?key|secret|password)\s*[:=]")
_SUPPORTED_TARGET_TYPES = frozenset({"paragraph", "list"})
_SECTION_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_PATTERN = re.compile(r"^[ \t]*(`{3,}|~{3,})")


class EditPlanRenderError(ValueError):
    """A semantic edit cannot be represented as a bounded text change."""


@dataclass(frozen=True)
class RenderedSkill:
    child_bytes: bytes
    patch_bytes: bytes
    changed_edit_ids: tuple[str, ...]
    render_policy_version: str


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    text: str
    edit_id: str


def render_edit_plan(parent_bytes: bytes, plan: SkillEditPlan | SkillEditPlanV2) -> RenderedSkill:
    """Render one source-bound plan without consulting a model or external state."""

    if isinstance(plan, SkillEditPlanV2):
        return _render_v2_edit_plan(parent_bytes, plan)
    return _render_legacy_edit_plan(parent_bytes, plan)


def _render_legacy_edit_plan(parent_bytes: bytes, plan: SkillEditPlan) -> RenderedSkill:
    """Render persisted v1 plans with the retired v2 paragraph/list map."""

    if plan.source.parent_skill_sha256 != _sha256(parent_bytes):
        raise EditPlanRenderError("parent Skill hash does not match the edit plan")
    try:
        parent_text = parent_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EditPlanRenderError("parent Skill must be UTF-8") from error
    if "\x00" in parent_text:
        raise EditPlanRenderError("parent Skill must not contain NUL")

    skill_map = build_legacy_skill_map(parent_bytes)
    if plan.source.skill_map_sha256 != _sha256(
        canonical_json_bytes(skill_map.model_dump(mode="json"))
    ):
        raise EditPlanRenderError("Skill map hash does not match the edit plan")
    blocks = {block.block_id: block for block in skill_map.blocks}
    offsets = _line_offsets(parent_text)
    replacements: list[_Replacement] = []
    for edit in plan.edits:
        replacements.extend(_replacements_for_edit(edit, parent_text, blocks, offsets))
    _validate_replacements(replacements)

    child_text = parent_text
    for replacement in sorted(replacements, key=lambda value: value.start, reverse=True):
        child_text = (
            child_text[: replacement.start]
            + replacement.text
            + child_text[replacement.end :]
        )
    child_bytes = child_text.encode("utf-8")
    _validate_child_text(child_text)
    try:
        build_legacy_skill_map(child_bytes)
    except ValueError as error:
        raise EditPlanRenderError("rendered Skill is not structurally valid") from error
    patch_bytes = _unified_diff(parent_text, child_text)
    if not patch_bytes:
        raise EditPlanRenderError("semantic edits produced an empty patch")
    if apply_unified_diff(parent_bytes, patch_bytes) != child_bytes:
        raise EditPlanRenderError("rendered patch does not reproduce the child Skill")
    return RenderedSkill(
        child_bytes=child_bytes,
        patch_bytes=patch_bytes,
        changed_edit_ids=tuple(edit.edit_id for edit in plan.edits),
        render_policy_version=_LEGACY_RENDER_POLICY_VERSION,
    )


def _render_v2_edit_plan(parent_bytes: bytes, plan: SkillEditPlanV2) -> RenderedSkill:
    if plan.source.parent_skill_sha256 != _sha256(parent_bytes):
        raise EditPlanRenderError("parent Skill hash does not match the edit plan")
    try:
        parent_text = parent_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EditPlanRenderError("parent Skill must be UTF-8") from error
    if "\x00" in parent_text:
        raise EditPlanRenderError("parent Skill must not contain NUL")

    skill_map = build_skill_map(parent_bytes)
    if plan.source.skill_map_sha256 != _sha256(
        canonical_json_bytes(skill_map.model_dump(mode="json"))
    ):
        raise EditPlanRenderError("Skill map hash does not match the edit plan")
    sections = {section.block.block_id: section for section in parse_heading_blocks(parent_bytes)}
    replacements = _v2_replacements(plan.edits, parent_text, sections)
    _validate_replacements(replacements)

    child_text = parent_text
    for replacement in sorted(replacements, key=lambda value: value.start, reverse=True):
        child_text = (
            child_text[: replacement.start]
            + replacement.text
            + child_text[replacement.end :]
        )
    child_bytes = child_text.encode("utf-8")
    _validate_child_text(child_text)
    try:
        build_skill_map(child_bytes)
    except ValueError as error:
        raise EditPlanRenderError("rendered Skill is not structurally valid") from error
    patch_bytes = _unified_diff(parent_text, child_text)
    if not patch_bytes:
        raise EditPlanRenderError("semantic edits produced an empty patch")
    if apply_unified_diff(parent_bytes, patch_bytes) != child_bytes:
        raise EditPlanRenderError("rendered patch does not reproduce the child Skill")
    return RenderedSkill(
        child_bytes=child_bytes,
        patch_bytes=patch_bytes,
        changed_edit_ids=tuple(edit.edit_id for edit in plan.edits),
        render_policy_version=RENDER_POLICY_VERSION,
    )


def _v2_replacements(
    edits: tuple[BlockEdit, ...],
    parent_text: str,
    sections: dict[str, ParsedHeadingBlock],
) -> list[_Replacement]:
    scopes: list[str] = []
    replacements: list[_Replacement] = []
    newline = "\r\n" if "\r\n" in parent_text else "\n"
    for edit in edits:
        if edit.operation == "replace_block":
            target_id = edit.target_block_id
            fragment = edit.replacement_markdown
            if target_id is None or fragment is None:
                raise EditPlanRenderError("replace_block is incomplete")
            target = sections.get(target_id)
            if target is None:
                raise EditPlanRenderError("edit refers to an unknown Skill block")
            _validate_v2_scope(target_id, sections, scopes)
            _validate_v2_fragment(fragment, target.block.heading_level)
            replacements.append(
                _Replacement(
                    target.start,
                    target.end,
                    _normalise_fragment(fragment, newline),
                    edit.edit_id,
                )
            )
            scopes.append(target_id)
            continue

        parent_id = edit.parent_block_id
        fragment = edit.new_block_markdown
        if parent_id is None or fragment is None:
            raise EditPlanRenderError("add_block is incomplete")
        parent = sections.get(parent_id)
        if parent is None:
            raise EditPlanRenderError("edit refers to an unknown Skill block")
        new_level = parent.block.heading_level + 1
        if new_level > 6:
            raise EditPlanRenderError("cannot add a child beneath a level-six heading")
        after = sections.get(edit.after_block_id) if edit.after_block_id is not None else None
        if edit.after_block_id is not None and (after is None or after.parent_id != parent_id):
            raise EditPlanRenderError("after_block_id must be a direct child of parent_block_id")
        _validate_v2_scope(parent_id, sections, scopes)
        _validate_v2_fragment(fragment, new_level)
        insertion = after.end if after is not None else parent.end
        prefix = "" if insertion == 0 or parent_text[insertion - 1] in "\r\n" else newline
        replacements.append(
            _Replacement(
                insertion,
                insertion,
                prefix + _normalise_fragment(fragment, newline),
                edit.edit_id,
            )
        )
        scopes.append(parent_id)
    return replacements


def _validate_v2_scope(
    scope: str,
    sections: dict[str, ParsedHeadingBlock],
    scopes: list[str],
) -> None:
    if any(_block_paths_overlap(scope, existing) for existing in scopes):
        raise EditPlanRenderError("rendered edit ranges overlap")
    if any(
        section.block.permission == "locked" and _block_paths_overlap(scope, block_id)
        for block_id, section in sections.items()
    ):
        raise EditPlanRenderError("locked Skill blocks cannot be rendered")


def _block_paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}.") or right.startswith(f"{left}.")


def _validate_v2_fragment(fragment: str, expected_level: int) -> None:
    first_heading_seen = False
    marker: str | None = None
    for line in fragment.splitlines():
        stripped = line.strip()
        if marker is not None:
            if _is_fence_closing(stripped, marker):
                marker = None
            continue
        heading = _SECTION_HEADING_PATTERN.fullmatch(stripped)
        if not first_heading_seen:
            if not stripped:
                continue
            if heading is None or len(heading.group(1)) != expected_level:
                raise EditPlanRenderError(
                    "section fragment must begin with the expected heading level"
                )
            first_heading_seen = True
            continue
        fence = _FENCE_PATTERN.match(stripped)
        if fence is not None:
            marker = fence.group(1)
            continue
        if heading is not None and len(heading.group(1)) <= expected_level:
            raise EditPlanRenderError("section fragment exceeds its target heading boundary")
    if not first_heading_seen:
        raise EditPlanRenderError("section fragment must contain a heading")


def _is_fence_closing(line: str, marker: str) -> bool:
    return bool(re.fullmatch(rf"{re.escape(marker[0])}{{{len(marker)},}}", line))


def _normalise_fragment(fragment: str, newline: str) -> str:
    normalized = fragment.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    return normalized if normalized.endswith(newline) else f"{normalized}{newline}"


def apply_unified_diff(parent_bytes: bytes, patch_bytes: bytes) -> bytes:
    """Apply the renderer's single-file unified diff and reject malformed hunks."""

    try:
        parent_lines = parent_bytes.decode("utf-8").splitlines(keepends=True)
        patch_lines = patch_bytes.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise EditPlanRenderError("diff inputs must be UTF-8") from error
    if (
        len(patch_lines) < 3
        or patch_lines[0] != "--- a/SKILL.md\n"
        or patch_lines[1] != "+++ b/SKILL.md\n"
    ):
        raise EditPlanRenderError("patch must be a single relative SKILL.md diff")

    output: list[str] = []
    parent_index = 0
    index = 2
    saw_hunk = False
    while index < len(patch_lines):
        match = _HUNK_PATTERN.match(patch_lines[index])
        if match is None:
            raise EditPlanRenderError("patch contains an invalid hunk header")
        saw_hunk = True
        old_start = int(match.group("old_start"))
        target_index = old_start - 1
        if target_index < parent_index or target_index > len(parent_lines):
            raise EditPlanRenderError("patch hunk has an invalid parent location")
        output.extend(parent_lines[parent_index:target_index])
        parent_index = target_index
        index += 1
        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            line = patch_lines[index]
            if not line or line[0] not in {" ", "+", "-"}:
                raise EditPlanRenderError("patch contains an invalid hunk line")
            content = line[1:]
            if line[0] == " ":
                if parent_index >= len(parent_lines) or parent_lines[parent_index] != content:
                    raise EditPlanRenderError("patch context does not match the parent Skill")
                output.append(content)
                parent_index += 1
            elif line[0] == "-":
                if parent_index >= len(parent_lines) or parent_lines[parent_index] != content:
                    raise EditPlanRenderError("patch removal does not match the parent Skill")
                parent_index += 1
            else:
                output.append(content)
            index += 1
    if not saw_hunk:
        raise EditPlanRenderError("patch must contain at least one hunk")
    output.extend(parent_lines[parent_index:])
    return "".join(output).encode("utf-8")


def _replacements_for_edit(
    edit: SemanticEdit,
    parent_text: str,
    blocks: dict[str, SkillBlock],
    offsets: tuple[int, ...],
) -> list[_Replacement]:
    target_ids = edit.target_block_ids
    anchor_id = edit.placement_after_block_id
    target_blocks = [_get_editable_block(blocks, block_id) for block_id in target_ids]
    anchor = _get_editable_block(blocks, anchor_id) if anchor_id is not None else None
    newline = "\r\n" if "\r\n" in parent_text else "\n"
    rule_text = _rule_markdown(edit, newline)

    if edit.operation == "add_rule":
        insertion_block = anchor or (target_blocks[0] if len(target_blocks) == 1 else None)
        if insertion_block is None:
            raise EditPlanRenderError("add_rule requires one editable target or anchor")
        _, end = _block_span(insertion_block, offsets)
        return [_Replacement(end, end, f"{newline}{rule_text}", edit.edit_id)]
    if edit.operation == "remove_rule":
        if not target_blocks:
            raise EditPlanRenderError("remove_rule requires a target block")
        return [
            _Replacement(*_block_span(block, offsets), "", edit.edit_id)
            for block in target_blocks
        ]
    if edit.operation == "merge_rules":
        if len(target_blocks) < 2:
            raise EditPlanRenderError("merge_rules requires at least two target blocks")
        spans = [_block_span(block, offsets) for block in target_blocks]
        start = min(value[0] for value in spans)
        end = max(value[1] for value in spans)
        merged_target_ids = {block.block_id for block in target_blocks}
        for block in blocks.values():
            block_start, block_end = _block_span(block, offsets)
            if (
                start <= block_start
                and block_end <= end
                and block.block_id not in merged_target_ids
            ):
                raise EditPlanRenderError("merge_rules targets must be contiguous")
        return [_Replacement(start, end, rule_text, edit.edit_id)]
    if edit.operation == "reorder_workflow":
        if not isinstance(edit.rule, WorkflowRule):
            raise EditPlanRenderError("reorder_workflow requires a workflow rule")
        if len(target_blocks) != 1 or anchor is not None:
            raise EditPlanRenderError("reorder_workflow requires exactly one target block")
        return [_Replacement(*_block_span(target_blocks[0], offsets), rule_text, edit.edit_id)]
    if len(target_blocks) != 1 or anchor is not None:
        raise EditPlanRenderError(f"{edit.operation} requires exactly one target block")
    return [_Replacement(*_block_span(target_blocks[0], offsets), rule_text, edit.edit_id)]


def _get_editable_block(blocks: dict[str, SkillBlock], block_id: str) -> SkillBlock:
    block = blocks.get(block_id)
    if block is None:
        raise EditPlanRenderError("edit refers to an unknown Skill block")
    if block.permission == "locked":
        raise EditPlanRenderError("locked Skill blocks cannot be rendered")
    if block.block_type not in _SUPPORTED_TARGET_TYPES:
        raise EditPlanRenderError("renderer only supports paragraph and list targets")
    return block


def _block_span(block: SkillBlock, offsets: tuple[int, ...]) -> tuple[int, int]:
    return offsets[block.start_line - 1], offsets[block.end_line]


def _line_offsets(text: str) -> tuple[int, ...]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return tuple(offsets)


def _rule_markdown(edit: SemanticEdit, newline: str) -> str:
    rule = edit.rule
    if isinstance(rule, WorkflowRule):
        sentence = (
            f"When {rule.trigger}, {rule.action} Verify: {rule.validation} "
            f"Then {rule.exit_or_next}"
        )
    elif isinstance(rule, RecoveryRule):
        sentence = (
            f"When {rule.failure_signal}, {rule.diagnosis} Then {rule.recovery_action} "
            f"Use {rule.retry_or_alternative} Verify: {rule.validation} "
            f"Stop when {rule.stop_condition}"
        )
    elif isinstance(rule, ConstraintRule):
        sentence = (
            f"For {rule.scope}, {rule.requirement_or_prohibition} Priority: {rule.precedence} "
            f"If violated: {rule.violation_handling}"
        )
    elif isinstance(rule, HeuristicRule):
        sentence = f"When {rule.applicability}, {rule.recommendation} Boundary: {rule.boundary}"
    else:  # pragma: no cover - Pydantic discriminator prevents this branch.
        raise EditPlanRenderError("unsupported rule definition")
    return f"- **{edit.primary_module} rule:** {sentence}{newline}"


def _validate_replacements(replacements: list[_Replacement]) -> None:
    if not replacements:
        raise EditPlanRenderError("edit plan has no renderable edits")
    previous_end = -1
    for replacement in sorted(replacements, key=lambda value: (value.start, value.end)):
        if replacement.start < previous_end:
            raise EditPlanRenderError("rendered edit ranges overlap")
        previous_end = replacement.end


def _validate_child_text(child_text: str) -> None:
    if "\x00" in child_text:
        raise EditPlanRenderError("rendered Skill contains NUL")
    if "*** Begin Patch" in child_text or "diff --git " in child_text:
        raise EditPlanRenderError("rendered Skill contains a patch marker")
    if _PRIVATE_PATH_PATTERN.search(child_text) is not None:
        raise EditPlanRenderError("rendered Skill contains a private path")
    if _SECRET_PATTERN.search(child_text) is not None:
        raise EditPlanRenderError("rendered Skill contains secret-like text")


def _unified_diff(parent_text: str, child_text: str) -> bytes:
    lines = difflib.unified_diff(
        parent_text.splitlines(keepends=True),
        child_text.splitlines(keepends=True),
        fromfile="a/SKILL.md",
        tofile="b/SKILL.md",
    )
    return "".join(lines).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
