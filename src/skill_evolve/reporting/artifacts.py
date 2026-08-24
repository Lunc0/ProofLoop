"""Derive concise, reviewable artifacts from immutable run evidence."""

from __future__ import annotations

from collections import Counter

import yaml

from skill_evolve.attribution.core import Attribution
from skill_evolve.evaluator.core import CaseEvaluation


def build_report(
    run_id: str,
    fingerprint: str,
    attribution: Attribution,
    evaluations: tuple[CaseEvaluation, ...],
) -> str:
    counts = Counter(evaluation.verdict.value for evaluation in evaluations)
    known_costs: list[float] = []
    missing_costs = 0
    for evaluation in evaluations:
        cost = evaluation.usage["cost_usd"]
        if cost is None:
            missing_costs += 1
        else:
            known_costs.append(float(cost))
    total_cost = sum(known_costs)
    lines = [
        f"# Run {run_id}",
        "",
        f"- Status: `{attribution.status.value}`",
        f"- Attribution: `{attribution.kind.value}` — {attribution.reason}",
        f"- Environment fingerprint: `{fingerprint}`",
        f"- Requested cases: {len(evaluations)}",
        f"- Known cost: `${total_cost:.6f}`; unknown cost cases: {missing_costs}",
        "",
        "## Verdicts",
        "",
    ]
    lines.extend(f"- `{verdict}`: {count}" for verdict, count in sorted(counts.items()))
    lines.extend(["", "## Cases", "", "| Case | Slice | Verdict | Detail |", "|---|---|---|---|"])
    for evaluation in evaluations:
        detail = (evaluation.detail or "").replace("|", "\\|")
        lines.append(
            f"| `{evaluation.case_id}` | `{evaluation.slice}` | "
            f"`{evaluation.verdict.value}` | {detail} |"
        )
    lines.extend(["", "## Next action", "", attribution.next_action, ""])
    return "\n".join(lines)


def build_regression_draft(
    run_id: str,
    fingerprint: str,
    evaluations: tuple[CaseEvaluation, ...],
) -> str:
    """Create a review-only YAML draft; it never mutates the maintained suite."""

    cases = [
        {
            "id": f"{evaluation.case_id}-regression",
            "slice": "fast",
            "prompt": evaluation.prompt,
            "expected_query": evaluation.expected_query,
            "source": {
                "run_id": run_id,
                "environment_fingerprint": fingerprint,
                "observed_candidate_sql": evaluation.candidate_sql,
                "verdict": evaluation.verdict.value,
                "detail": evaluation.detail,
            },
        }
        for evaluation in evaluations
        if not evaluation.passed
    ]
    return yaml.safe_dump(
        {"schema_version": "v1-draft", "review_required": True, "cases": cases},
        sort_keys=False,
        allow_unicode=True,
    )


def build_skill_change_suggestion(evaluations: tuple[CaseEvaluation, ...]) -> str:
    lines = [
        "# Skill change suggestion",
        "",
        "Review these objective failures before changing the Skill:",
        "",
    ]
    for evaluation in evaluations:
        if not evaluation.passed:
            lines.extend(
                [
                    f"- `{evaluation.case_id}`: `{evaluation.verdict.value}`",
                    f"  - Evidence: {evaluation.detail or 'see evaluation.json'}",
                ]
            )
    lines.extend(["", "After an intentional Skill update, run the same manifest again.", ""])
    return "\n".join(lines)


def build_repair_checklist(attribution: Attribution, detail: str | None) -> str:
    lines = [
        "# Experiment paused: repair checklist",
        "",
        f"- Reason: `{attribution.reason}`",
        f"- Affected cases: {', '.join(attribution.case_ids) or 'before case evaluation'}",
        "",
        "Before rerunning:",
        "",
        "1. Fix the runner, evaluator, fixture, schema, seed, or expected query evidence.",
        "2. Change the relevant evaluator/data version in the manifest.",
        "3. Validate the manifest and start a new run; do not modify the Skill based on this run.",
        "",
    ]
    if detail:
        lines.extend(["## Captured error", "", "```text", detail, "```", ""])
    return "\n".join(lines)
