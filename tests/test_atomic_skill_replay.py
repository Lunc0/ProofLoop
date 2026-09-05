from __future__ import annotations

import hashlib
import json

import pytest

from skill_evolve.optimizer.coding_agent.contracts import AtomicSkillEditDeclaration
from skill_evolve.optimizer.coding_agent.replay import AtomicReplayError, bind_atomic_edits

EVIDENCE_REF = "evidence-001-001-acde12345678"
PARENT = (
    "# 演示\n\n"
    "## Core Strategy\n\n"
    "Old guidance.\n\n"
    "## Validation\n\n"
    "Validate output.\n"
).encode()


def _declaration(*, target: str = "b1.1", replacement: str) -> AtomicSkillEditDeclaration:
    return AtomicSkillEditDeclaration.model_validate_json(
        json.dumps(
            {
            "schema_version": "atomic-skill-edit-declaration-v1",
            "edits": [
                {
                    "operation": "replace_block",
                    "target_block_id": target,
                    "replacement_markdown": replacement,
                    "hypothesis": "Add a missing verification step",
                    "evidence_refs": [EVIDENCE_REF],
                }
            ],
            }
        )
    )


def test_bind_replays_exact_full_and_singleton_bytes_with_unicode_parent() -> None:
    replacement = "## Core Strategy\n\nNew guidance.\n\n"
    expected = PARENT.replace(b"## Core Strategy\n\nOld guidance.\n\n", replacement.encode())
    result = bind_atomic_edits(
        PARENT,
        _declaration(replacement=replacement),
        expected,
        run_id="run-1",
        parent_candidate_id="demo/parent",
        evidence_bundle_sha256=hashlib.sha256(b"evidence").hexdigest(),
        invocation_receipt_sha256=hashlib.sha256(b"receipt").hexdigest(),
        allowed_evidence_refs=frozenset({EVIDENCE_REF}),
    )

    edit = result.bound_edits.edits[0]
    assert result.full_skill_bytes == expected
    assert result.singleton_skill_bytes["edit-001"] == expected
    assert PARENT[edit.resolved_start_byte : edit.resolved_end_byte].startswith(
        b"## Core Strategy"
    )


def test_bind_rejects_undeclared_change() -> None:
    replacement = "## Core Strategy\n\nNew guidance.\n\n"
    expected = PARENT.replace(b"Validate output.", b"Undeclared.")
    with pytest.raises(AtomicReplayError, match="do not reproduce") as error:
        bind_atomic_edits(
            PARENT,
            _declaration(replacement=replacement),
            expected,
            run_id="run-1",
            parent_candidate_id="demo/parent",
            evidence_bundle_sha256=hashlib.sha256(b"evidence").hexdigest(),
            invocation_receipt_sha256=hashlib.sha256(b"receipt").hexdigest(),
            allowed_evidence_refs=frozenset({EVIDENCE_REF}),
        )
    assert error.value.code == "undeclared_change"


def test_bind_rejects_locked_target_and_unknown_evidence() -> None:
    locked_parent = PARENT + b"\n## Safety Constraints\n\nDo not change.\n"
    with pytest.raises(AtomicReplayError) as locked:
        bind_atomic_edits(
            locked_parent,
            _declaration(target="b1.3", replacement="## Safety Constraints\n\nChanged.\n"),
            locked_parent,
            run_id="run-1",
            parent_candidate_id="demo/parent",
            evidence_bundle_sha256="0" * 64,
            invocation_receipt_sha256="1" * 64,
            allowed_evidence_refs=frozenset({EVIDENCE_REF}),
        )
    assert locked.value.code == "locked_target"

    with pytest.raises(AtomicReplayError) as unknown:
        bind_atomic_edits(
            PARENT,
            _declaration(replacement="## Core Strategy\n\nChanged.\n\n"),
            PARENT,
            run_id="run-1",
            parent_candidate_id="demo/parent",
            evidence_bundle_sha256="0" * 64,
            invocation_receipt_sha256="1" * 64,
            allowed_evidence_refs=frozenset(),
        )
    assert unknown.value.code == "invalid_declaration"
