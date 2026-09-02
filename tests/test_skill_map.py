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
    assert first.blocks[0].block_type == "frontmatter"
    assert first.blocks[0].permission == "locked"
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
        block for block in skill_map.blocks if block.heading_path[-1:] == ("Instructions",)
    ]
    recovery = [
        block for block in skill_map.blocks if block.heading_path[-1:] == ("Recovery",)
    ]
    assert {block.permission for block in instructions} == {"guarded"}
    assert {block.permission for block in recovery} == {"evolvable"}


def test_every_block_id_is_bound_to_the_complete_skill_hash() -> None:
    original = b"# Skill\n\n## Workflow\nValidate SQL.\n\n## Notes\nFirst note.\n"
    changed = b"# Skill\n\n## Workflow\nValidate SQL.\n\n## Notes\nChanged note.\n"

    original_map = build_skill_map(original)
    changed_map = build_skill_map(changed)

    assert original_map.blocks[0].content_sha256 == changed_map.blocks[0].content_sha256
    assert original_map.blocks[0].block_id != changed_map.blocks[0].block_id


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
        if block.block_type == "paragraph" and block.heading_path[-1:] == (heading,)
    ]
    assert len(matches) == 1
    return matches[0].permission
