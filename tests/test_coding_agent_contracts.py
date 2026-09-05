from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_evolve.optimizer.coding_agent.contracts import AtomicSkillEditDeclaration


def test_atomic_declaration_rejects_unknown_fields_and_duplicate_targets() -> None:
    with pytest.raises(ValidationError):
        AtomicSkillEditDeclaration.model_validate(
            {
                "schema_version": "atomic-skill-edit-declaration-v1",
                "edits": [
                    {
                        "operation": "replace_block",
                        "target_block_id": "b1.1",
                        "replacement_markdown": "## Core Strategy\nOne.\n",
                        "hypothesis": "one",
                        "evidence_refs": ["evidence-001-001-acde12345678"],
                    },
                    {
                        "operation": "replace_block",
                        "target_block_id": "b1.1",
                        "replacement_markdown": "## Core Strategy\nTwo.\n",
                        "hypothesis": "two",
                        "evidence_refs": ["evidence-001-001-acde12345678"],
                    },
                ],
            }
        )

    with pytest.raises(ValidationError):
        AtomicSkillEditDeclaration.model_validate(
            {
                "schema_version": "atomic-skill-edit-declaration-v1",
                "edits": [
                    {
                        "operation": "replace_block",
                        "target_block_id": "b1.1",
                        "replacement_markdown": "## Core Strategy\nOne.\n",
                        "hypothesis": "one",
                        "evidence_refs": ["evidence-001-001-acde12345678"],
                        "parent_skill_sha256": "0" * 64,
                    }
                ],
            }
        )
