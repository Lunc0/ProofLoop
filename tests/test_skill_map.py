"""Deterministic, fail-closed Skill map coverage."""

from __future__ import annotations

import pytest

from skill_evolve.optimizer.skill_map import build_skill_map
from skill_evolve.trajectory.compressor import canonical_json_bytes


def test_standard_skeleton_maps_permissions_and_is_byte_stable() -> None:
    content = b"""---
name: sql-agent
---
# SQL Skill

## Runtime Contract
Use one read-only SQL statement.

## Core Strategy
### Workflow
Inspect schema before writing SQL.

## Heuristics and Examples
Prefer a narrow query during exploration.

## Legacy Notes
This section has no recognized policy.
"""

    first = build_skill_map(content)
    second = build_skill_map(content)

    assert canonical_json_bytes(first.model_dump(mode="json")) == canonical_json_bytes(
        second.model_dump(mode="json")
    )
    assert [block.block_id for block in first.blocks] == ["b01", "b02", "b03", "b04"]
    assert {block.block_type for block in first.blocks} <= {"paragraph", "list"}
    assert {block.section for block in first.blocks} == {
        "Runtime Contract",
        "Workflow",
        "Heuristics and Examples",
        "Legacy Notes",
    }
    assert _paragraph_permission(first, "Runtime Contract") == "locked"
    assert _paragraph_permission(first, "Workflow") == "evolvable"
    assert _paragraph_permission(first, "Heuristics and Examples") == "experimental"
    assert _paragraph_permission(first, "Legacy Notes") == "guarded"


def test_dbhub_style_unknown_sections_remain_guarded() -> None:
    content = b"""# DBHub SQL Skill

## Instructions
- Discover the schema.
- Run the query.

## Recovery
Retry only after inspecting the direct error.
"""

    skill_map = build_skill_map(content)

    instructions = [
        block for block in skill_map.blocks if block.section == "Instructions"
    ]
    recovery = [
        block for block in skill_map.blocks if block.section == "Recovery"
    ]
    assert {block.permission for block in instructions} == {"guarded"}
    assert {block.permission for block in recovery} == {"evolvable"}


def test_short_block_ids_are_structural_and_do_not_embed_content_hashes() -> None:
    original = b"# Skill\n\n## Workflow\nValidate SQL.\n\n## Notes\nFirst note.\n"
    changed = b"# Skill\n\n## Workflow\nValidate SQL.\n\n## Notes\nChanged note.\n"

    original_map = build_skill_map(original)
    changed_map = build_skill_map(changed)

    assert original_map.blocks == changed_map.blocks
    assert [block.block_id for block in original_map.blocks] == ["b01", "b02"]


@pytest.mark.parametrize(
    "content, message",
    [
        (b"---\nname: broken\n", "frontmatter"),
        (b"# Skill\n\n```sql\nSELECT 1;\n", "fenced code"),
        (b"\xff", "UTF-8"),
    ],
)
def test_unstable_or_invalid_skill_fails_closed(content: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_skill_map(content)


def _paragraph_permission(skill_map, heading: str) -> str:
    matches = [
        block
        for block in skill_map.blocks
        if block.block_type == "paragraph" and block.section == heading
    ]
    assert len(matches) == 1
    return matches[0].permission
