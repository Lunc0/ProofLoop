"""Compact heading SkillMap coverage."""

from __future__ import annotations

import pytest

from skill_evolve.optimizer.skill_map import build_skill_map, parse_heading_blocks
from skill_evolve.trajectory.compressor import canonical_json_bytes


def test_heading_tree_is_compact_stable_and_ignores_fenced_headings() -> None:
    content = b"""---
name: sql-agent
---
# SQL Skill

## Runtime Contract
Use one read-only SQL statement.

## Core Strategy
### Workflow
Inspect schema before writing SQL.

```markdown
## This is not a Skill section
```

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
    assert [block.block_id for block in first.blocks] == [
        "b1",
        "b1.1",
        "b1.2",
        "b1.2.1",
        "b1.3",
        "b1.4",
    ]
    assert [(block.heading, block.heading_level) for block in first.blocks] == [
        ("SQL Skill", 1),
        ("Runtime Contract", 2),
        ("Core Strategy", 2),
        ("Workflow", 3),
        ("Heuristics and Examples", 2),
        ("Legacy Notes", 2),
    ]
    assert [block.permission for block in first.blocks] == [
        "guarded",
        "locked",
        "evolvable",
        "evolvable",
        "experimental",
        "guarded",
    ]
    assert set(first.blocks[0].model_dump()) == {
        "block_id",
        "heading",
        "heading_level",
        "permission",
    }
    sections = {section.block.block_id: section for section in parse_heading_blocks(content)}
    assert content.decode("utf-8")[sections["b1.2"].start : sections["b1.2"].end].startswith(
        "## Core Strategy"
    )
    assert content.decode("utf-8")[sections["b1.2"].start : sections["b1.2"].end].endswith(
        "``\n\n"
    )


def test_heading_paths_do_not_depend_on_body_text() -> None:
    original = b"# Skill\n\n## Workflow\nValidate SQL.\n\n## Notes\nFirst note.\n"
    changed = b"# Skill\n\n## Workflow\nValidate SQL.\n\n## Notes\nChanged note.\n"

    original_map = build_skill_map(original)
    changed_map = build_skill_map(changed)

    assert original_map == changed_map
    assert [block.block_id for block in original_map.blocks] == ["b1", "b1.1", "b1.2"]


@pytest.mark.parametrize(
    "content, message",
    [
        (b"---\nname: broken\n", "frontmatter"),
        (b"# Skill\n\n```sql\nSELECT 1;\n", "fenced code"),
        (b"## Only a subsection\n", "beneath a level-one"),
        (b"plain text only\n", "level-one heading"),
        (b"\xff", "UTF-8"),
    ],
)
def test_invalid_skill_map_inputs_fail_closed(content: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_skill_map(content)
