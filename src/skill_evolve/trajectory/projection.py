"""Build the compact, optimizer-facing view from v1 custody evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, Literal

from skill_evolve.trajectory.compressor import _safe_text, canonical_json_bytes
from skill_evolve.trajectory.contracts import (
    CompressedToolCall,
    CompressedTrajectory,
    EpisodeAction,
    EvidenceText,
    FailureRecoveryEpisode,
    FinalSubmissionAttempt,
    ProjectionCandidateOutput,
    ProjectionContext,
    ProjectionEvaluation,
    ProjectionEvidence,
    SkillAccessObservation,
    SkillRuleExcerpt,
    SourceLocation,
)
from skill_evolve.trajectory.dependency import (
    SqlReferences,
    extract_sql_references,
    sample_coverage,
)
from skill_evolve.trajectory.policy import CompressorPolicy

_SQL_FENCE = re.compile(r"```(?:sql|postgresql)\s*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)
_SQL_READONLY_SINGLE_QUOTE = re.compile(
    r"sql-readonly\.ps1\s+-Query\s+'((?:''|[^'])*)'", re.IGNORECASE | re.DOTALL
)
_SQL_READONLY_DOUBLE_QUOTE = re.compile(
    r'sql-readonly\.ps1\s+-Query\s+"((?:`.|[^\"])*)"', re.IGNORECASE | re.DOTALL
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DDL_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ProjectionContextSource:
    """One bounded public context source; its path never enters the projection."""

    role: Literal["schema", "knowledge", "column_meaning"]
    text: str


@dataclass(frozen=True)
class _SqlAction:
    evidence_index: int
    references: SqlReferences
    status: str


@dataclass(frozen=True)
class _ObservedCall:
    call: CompressedToolCall
    step_index: int
    pre_failure_public_context: str | None


def project_context(
    sources: tuple[ProjectionContextSource, ...],
    trajectory: CompressedTrajectory,
    policy: CompressorPolicy,
) -> tuple[ProjectionContext, ...]:
    """Select direct public-context evidence, with a bounded conservative fallback."""

    references = _reference_corpus(trajectory)
    sql_tables = _sql_tables(trajectory)
    return tuple(
        _project_context_source(source, references, sql_tables, policy) for source in sources
    )


def project_evidence(
    trajectory: CompressedTrajectory, policy: CompressorPolicy
) -> tuple[ProjectionEvidence, ...]:
    """Project paired v1 steps into priority-ordered, metadata-free evidence."""

    drafts: list[dict[str, Any]] = []
    sql_actions: list[_SqlAction] = []
    calls = tuple(call for step in trajectory.trajectory for call in step.tool_calls)

    if trajectory.trace.trace_completeness != "complete":
        drafts.append(
            {
                "kind": "trace_state",
                "priority": "P0",
                "outcome": "unknown",
                "detail": {
                    "trace_completeness": trajectory.trace.trace_completeness,
                    "runner_outcome": trajectory.trace.runner_outcome,
                    "missing_terminal_events": list(trajectory.trace.missing_terminal_events),
                },
            }
        )

    call_position = 0
    for step in trajectory.trajectory:
        for message in step.public_assistant_text:
            drafts.append(
                {
                    "kind": "public_assistant_statement",
                    "priority": "P3",
                    "outcome": "unknown",
                    "detail": {"text": _limit(message.text, policy.max_projection_text_bytes)},
                }
            )
        for call in step.tool_calls:
            action = _project_call(
                call,
                _has_later_successful_read(calls[call_position + 1 :]),
                any(
                    later.adapter == call.adapter and later.status == "succeeded"
                    for later in calls[call_position + 1 :]
                ),
                policy,
            )
            drafts.append(action)
            if call.adapter == "sql-readonly":
                query = _sql_query(call)
                if query is not None:
                    sql_actions.append(
                        _SqlAction(
                            evidence_index=len(drafts) - 1,
                            references=extract_sql_references(query),
                            status=call.status,
                        )
                    )
            call_position += 1

    _apply_sample_coverage(drafts, sql_actions)
    selected = _select_evidence(drafts, policy)
    return tuple(
        ProjectionEvidence(
            kind=draft["kind"],
            priority=draft["priority"],
            outcome=draft["outcome"],
            detail=draft["detail"],
        )
        for draft in selected
    )


def project_evidence_v3(
    trajectory: CompressedTrajectory,
    skill_text: str,
    policy: CompressorPolicy,
) -> tuple[ProjectionEvidence | FailureRecoveryEpisode, ...]:
    """Build bounded P0 failure episodes without exposing custody locations."""

    observed_calls = _observed_calls(trajectory)
    calls = tuple(observed.call for observed in observed_calls)
    episode_policy = replace(
        policy, max_projection_text_bytes=policy.max_failure_episode_action_text_bytes
    )
    drafts = [
        _project_call(
            observed.call,
            _has_later_successful_read(calls[index + 1 :]),
            False,
            episode_policy,
            include_later_same_adapter_success=False,
        )
        for index, observed in enumerate(observed_calls)
    ]
    sql_actions = [
        _SqlAction(
            evidence_index=index,
            references=extract_sql_references(query),
            status=observed.call.status,
        )
        for index, observed in enumerate(observed_calls)
        if observed.call.adapter == "sql-readonly"
        if (query := _sql_query(observed.call)) is not None
    ]
    _apply_sample_coverage(drafts, sql_actions)
    subject_read_indices = {
        index
        for index, observed in enumerate(observed_calls)
        if _subject_read_match(observed.call, skill_text) != "unknown"
    }

    items: list[tuple[int, ProjectionEvidence | FailureRecoveryEpisode]] = []
    if trajectory.trace.trace_completeness != "complete":
        items.append(
            (
                -1,
                ProjectionEvidence(
                    kind="trace_state",
                    priority="P0",
                    outcome="unknown",
                    detail={
                        "trace_completeness": trajectory.trace.trace_completeness,
                        "runner_outcome": trajectory.trace.runner_outcome,
                        "missing_terminal_events": list(trajectory.trace.missing_terminal_events),
                    },
                ),
            )
        )

    claimed_post_failure_actions: set[int] = set()
    for index, observed in enumerate(observed_calls):
        if _is_failure(observed.call):
            episode, claimed = _failure_recovery_episode(
                index,
                observed_calls,
                drafts,
                trajectory,
                skill_text,
                policy,
            )
            claimed_post_failure_actions.update(claimed)
            items.append((index, episode))
        elif observed.call.adapter == "read":
            continue
        elif (
            index not in claimed_post_failure_actions and index not in subject_read_indices
        ):
            items.append((index, _as_projection_evidence(drafts[index])))

    return tuple(_select_v3_evidence(items, policy))


def _observed_calls(trajectory: CompressedTrajectory) -> tuple[_ObservedCall, ...]:
    last_public_context: str | None = None
    observed: list[_ObservedCall] = []
    for step_index, step in enumerate(trajectory.trajectory):
        observed.extend(
            _ObservedCall(
                call=call,
                step_index=step_index,
                pre_failure_public_context=last_public_context,
            )
            for call in step.tool_calls
        )
        if step.public_assistant_text:
            last_public_context = step.public_assistant_text[-1].text
    return tuple(observed)


def _failure_recovery_episode(
    failure_index: int,
    observed_calls: tuple[_ObservedCall, ...],
    drafts: list[dict[str, Any]],
    trajectory: CompressedTrajectory,
    skill_text: str,
    policy: CompressorPolicy,
) -> tuple[FailureRecoveryEpisode, set[int]]:
    failed = observed_calls[failure_index]
    post_failure_indices, recovery_observation = _post_failure_window(
        failure_index, observed_calls, policy
    )
    episode = FailureRecoveryEpisode(
        pre_failure_public_context=_bounded_context(
            failed.pre_failure_public_context, policy
        ),
        failed_action=_as_episode_action(drafts[failure_index]),
        post_failure_public_context=_bounded_context(
            _post_failure_public_context(trajectory, failed.call.result), policy
        ),
        post_failure_actions=tuple(
            _as_episode_action(drafts[index]) for index in post_failure_indices
        ),
        recovery_observation=recovery_observation,
        skill_access=_skill_access_before_failure(
            observed_calls[:failure_index],
            trajectory,
            skill_text,
            failed.call,
            policy,
        ),
    )
    return _fit_episode(episode, policy), set(post_failure_indices)


def _post_failure_window(
    failure_index: int,
    observed_calls: tuple[_ObservedCall, ...],
    policy: CompressorPolicy,
) -> tuple[tuple[int, ...], Literal[
    "first_subsequent_success_observed", "no_success_observed", "truncated_before_success"
]]:
    failure_result = observed_calls[failure_index].call.result
    if failure_result is None:
        return (), "no_success_observed"
    selected: list[int] = []
    for index in range(failure_index + 1, len(observed_calls)):
        call = observed_calls[index].call
        if not _definitely_after(call.call, failure_result):
            continue
        if _is_failure(call):
            return tuple(selected), "no_success_observed"
        selected.append(index)
        if call.status == "succeeded":
            return tuple(selected), "first_subsequent_success_observed"
        if len(selected) >= policy.max_failure_episode_actions:
            return tuple(selected), "truncated_before_success"
    return tuple(selected), "no_success_observed"


def _post_failure_public_context(
    trajectory: CompressedTrajectory, failure_result: SourceLocation | None
) -> str | None:
    if failure_result is None or failure_result.turn is None or failure_result.step is None:
        return None
    failure_step = (failure_result.turn, failure_result.step)
    for step in trajectory.trajectory:
        if (step.turn, step.step) > failure_step and step.public_assistant_text:
            return step.public_assistant_text[0].text
    return None


def _definitely_after(location: SourceLocation, boundary: SourceLocation) -> bool:
    """Return only order that the compressed source makes unambiguous."""

    if (
        location.turn is None
        or location.step is None
        or boundary.turn is None
        or boundary.step is None
    ):
        return False
    return (location.turn, location.step, location.seq) > (
        boundary.turn,
        boundary.step,
        boundary.seq,
    )


def _skill_access_before_failure(
    prior_calls: tuple[_ObservedCall, ...],
    trajectory: CompressedTrajectory,
    skill_text: str,
    failed_call: CompressedToolCall,
    policy: CompressorPolicy,
) -> SkillAccessObservation:
    matches = [
        _subject_read_match(observed.call, skill_text)
        for observed in prior_calls
        if observed.call.adapter == "read" and observed.call.status == "succeeded"
    ]
    if "exact" in matches:
        subject_match: Literal["exact", "declared_subject_path", "unknown"] = "exact"
        occurred: Literal["observed", "not_observed", "unknown"] = "observed"
    elif "declared_subject_path" in matches:
        subject_match = "declared_subject_path"
        occurred = "observed"
    else:
        subject_match = "unknown"
        occurred = (
            "not_observed" if trajectory.trace.trace_completeness == "complete" else "unknown"
        )
    rule_excerpt = (
        _skill_rule_excerpt(skill_text, failed_call, policy) if occurred == "observed" else None
    )
    return SkillAccessObservation(
        occurred_before_failure=occurred,
        subject_match=subject_match,
        rule_excerpt=rule_excerpt,
    )


def _subject_read_match(
    call: CompressedToolCall, skill_text: str
) -> Literal["exact", "declared_subject_path", "unknown"]:
    if (
        call.output is not None
        and not call.output.truncated
        and call.output.text == skill_text
    ):
        return "exact"
    target = _summary_text(call.summary.get("target"))
    if target is None:
        return "unknown"
    normalised = target.replace("\\", "/").casefold().strip("/")
    if normalised in {"subject/skill.md", "workspace/subject/skill.md"}:
        return "declared_subject_path"
    return "unknown"


def _skill_rule_excerpt(
    skill_text: str, failed_call: CompressedToolCall, policy: CompressorPolicy
) -> SkillRuleExcerpt | None:
    direct_failure = failed_call.direct_failure
    if direct_failure is None:
        return None
    phrase = _normalise_constraint_text(direct_failure.direct_cause)
    adapter = _normalise_constraint_text(failed_call.adapter)
    if len(phrase) < 4 or len(adapter) < 3:
        return None
    for paragraph in re.split(r"\r?\n\s*\r?\n", skill_text):
        normalised_paragraph = _normalise_constraint_text(paragraph)
        if phrase in normalised_paragraph and adapter in normalised_paragraph:
            return SkillRuleExcerpt(
                text=_limit(paragraph, policy.max_skill_rule_excerpt_bytes),
                selection_basis=(
                    "adapter_and_direct_failure_cause:"
                    f"{failed_call.adapter}:{direct_failure.direct_cause}"
                ),
            )
    return None


def _normalise_constraint_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _fit_episode(
    episode: FailureRecoveryEpisode, policy: CompressorPolicy
) -> FailureRecoveryEpisode:
    fitted = episode
    while _episode_bytes(fitted) > policy.max_failure_episode_bytes and fitted.post_failure_actions:
        actions = fitted.post_failure_actions[:-1]
        observation = fitted.recovery_observation
        if observation == "first_subsequent_success_observed":
            observation = "truncated_before_success"
        fitted = fitted.model_copy(
            update={"post_failure_actions": actions, "recovery_observation": observation}
        )
    if _episode_bytes(fitted) > policy.max_failure_episode_bytes:
        fitted = fitted.model_copy(update={"post_failure_public_context": None})
    if _episode_bytes(fitted) > policy.max_failure_episode_bytes:
        fitted = fitted.model_copy(update={"pre_failure_public_context": None})
    if _episode_bytes(fitted) > policy.max_failure_episode_bytes:
        fitted = fitted.model_copy(
            update={"failed_action": _minimal_failure_action(fitted.failed_action, policy)}
        )
    return fitted


def _episode_bytes(episode: FailureRecoveryEpisode) -> int:
    return len(canonical_json_bytes(episode.model_dump(mode="json")))


def _minimal_failure_action(
    action: EpisodeAction, policy: CompressorPolicy
) -> EpisodeAction:
    detail = action.detail
    compact: dict[str, Any] = {}
    tool = detail.get("tool")
    if isinstance(tool, str):
        compact["tool"] = _limit(tool, 128)
    direct_failure = detail.get("direct_failure")
    if isinstance(direct_failure, dict):
        compact["direct_failure"] = direct_failure
    error = detail.get("error")
    if isinstance(error, str):
        compact["error"] = _limit(error, policy.max_failure_episode_action_text_bytes // 2)
    action_parameters = detail.get("action")
    if isinstance(action_parameters, dict):
        compact["action"] = _compact_action_parameters(action_parameters, policy)
    return action.model_copy(update={"detail": compact})


def _compact_action_parameters(
    parameters: dict[str, Any], policy: CompressorPolicy
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    value_limit = policy.max_failure_episode_action_text_bytes // 4
    for key in sorted(parameters)[:3]:
        value = parameters[key]
        if isinstance(value, str):
            compact[key] = _limit(value, value_limit)
        elif isinstance(value, int):
            compact[key] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            compact[key] = [_limit(item, value_limit) for item in value[:2]]
    return compact


def _bounded_context(value: str | None, policy: CompressorPolicy) -> str | None:
    return (
        _limit(value, policy.max_failure_episode_context_bytes) if value is not None else None
    )


def _is_failure(call: CompressedToolCall) -> bool:
    return call.status == "failed" or call.direct_failure is not None


def _as_episode_action(draft: dict[str, Any]) -> EpisodeAction:
    return EpisodeAction(
        kind=draft["kind"], outcome=draft["outcome"], detail=draft["detail"]
    )


def _as_projection_evidence(draft: dict[str, Any]) -> ProjectionEvidence:
    return ProjectionEvidence(
        kind=draft["kind"],
        priority=draft["priority"],
        outcome=draft["outcome"],
        detail=draft["detail"],
    )


def _select_v3_evidence(
    items: list[tuple[int, ProjectionEvidence | FailureRecoveryEpisode]],
    policy: CompressorPolicy,
) -> list[ProjectionEvidence | FailureRecoveryEpisode]:
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    ordered = sorted(items, key=lambda item: (order[_item_priority(item[1])], item[0]))
    required = [item for _, item in ordered if _item_priority(item) == "P0"]
    optional = [item for _, item in ordered if _item_priority(item) != "P0"]
    remaining = max(0, policy.max_projection_evidence - len(required))
    return required + optional[:remaining]


def _item_priority(item: ProjectionEvidence | FailureRecoveryEpisode) -> str:
    return item.priority


def project_candidate_output(trajectory: CompressedTrajectory) -> ProjectionCandidateOutput:
    """Expose only the candidate SQL accepted by the runner contract."""

    sql = trajectory.candidate_output.sql
    return ProjectionCandidateOutput(sql=sql.text if sql is not None else None)


def project_final_submission_attempt(
    trajectory: CompressedTrajectory, policy: CompressorPolicy
) -> FinalSubmissionAttempt | None:
    """Keep a final public attempt separate when no candidate was submitted."""

    if trajectory.candidate_output.sql is not None:
        return None
    final_message = trajectory.candidate_output.final_message
    if final_message is None:
        return None
    matches = _SQL_FENCE.findall(final_message.text)
    status: Literal[
        "no_sql_fence", "multiple_sql_fences", "empty_sql_fence", "runner_rejected"
    ]
    if not matches:
        status = "no_sql_fence"
    elif len(matches) > 1:
        status = "multiple_sql_fences"
    elif not matches[0].strip():
        status = "empty_sql_fence"
    else:
        status = "runner_rejected"
    return FinalSubmissionAttempt(
        text=_limit(final_message.text, policy.max_projection_text_bytes),
        extraction_status=status,
        not_submitted_reason="runner did not provide candidate SQL",
    )


def project_evaluation(
    trajectory: CompressedTrajectory, policy: CompressorPolicy
) -> ProjectionEvaluation:
    """Remove custody hashes while retaining agent-safe evaluator feedback."""

    evaluation = trajectory.evaluation
    return ProjectionEvaluation(
        slice=evaluation.slice,
        verdict=evaluation.verdict,
        candidate_sql=(
            _limit(evaluation.candidate_sql.text, policy.max_projection_text_bytes)
            if evaluation.candidate_sql is not None
            else None
        ),
        detail=(
            _limit(evaluation.detail.text, policy.max_projection_text_bytes)
            if evaluation.detail is not None
            else None
        ),
        usage=evaluation.usage,
    )


def _project_call(
    call: CompressedToolCall,
    later_read: bool,
    later_same_adapter_success: bool,
    policy: CompressorPolicy,
    *,
    include_later_same_adapter_success: bool = True,
) -> dict[str, Any]:
    if call.status == "failed" or call.direct_failure is not None:
        detail: dict[str, Any] = {
            "tool": call.adapter,
            "action": _action_parameters(call, policy),
            "error": _limit(_evidence_value(call.output), policy.max_projection_text_bytes),
            "direct_failure": (
                {
                    "category": call.direct_failure.category,
                    "cause": call.direct_failure.direct_cause,
                }
                if call.direct_failure is not None
                else None
            ),
        }
        if include_later_same_adapter_success:
            detail["later_same_tool_success"] = later_same_adapter_success
        return {"kind": "tool_failure", "priority": "P0", "outcome": "failed", "detail": detail}

    if call.adapter == "sql-readonly":
        return _project_sql_call(call, policy)
    if call.adapter == "glob" and later_read:
        return {
            "kind": "workspace_discovery",
            "priority": "P3",
            "outcome": _outcome(call.status),
            "detail": {
                "action": "directory discovery",
                "match_count": _nonnegative_summary_value(call.summary.get("match_count")),
                "folded": True,
            },
        }
    if call.adapter == "read":
        return {
            "kind": "file_read",
            "priority": "P1",
            "outcome": _outcome(call.status),
            "detail": {
                "target": _summary_text(call.summary.get("target")),
                "result": "reflected in selected context or retained as bounded evidence",
            },
        }
    return {
        "kind": f"tool_{call.adapter}",
        "priority": "P2",
        "outcome": _outcome(call.status),
        "detail": {
            "tool": call.name,
            "action": _action_parameters(call, policy),
            "result": _limit(_evidence_value(call.output), policy.max_projection_text_bytes),
        },
    }


def _project_sql_call(call: CompressedToolCall, policy: CompressorPolicy) -> dict[str, Any]:
    query = _sql_query(call)
    if query is None:
        return {
            "kind": "sql_unparsed",
            "priority": "P2",
            "outcome": _outcome(call.status),
            "detail": {
                "query": _limit(call.arguments.text, policy.max_projection_text_bytes),
                "result": _sql_result(call.output, policy),
            },
        }
    references = extract_sql_references(query)
    detail: dict[str, Any] = {
        "query": _limit(query, policy.max_projection_text_bytes),
        "parse_status": references.parse_status,
        "tables": list(references.tables),
        "qualified_columns": list(references.qualified_columns),
        "clauses": list(references.clauses),
        "result": _sql_result(call.output, policy),
    }
    if references.parse_status != "parsed":
        return {
            "kind": "sql_unparsed",
            "priority": "P2",
            "outcome": _outcome(call.status),
            "detail": detail,
        }
    return {
        "kind": "sql_query",
        "priority": "P2",
        "outcome": _outcome(call.status),
        "detail": detail,
    }


def _apply_sample_coverage(drafts: list[dict[str, Any]], sql_actions: list[_SqlAction]) -> None:
    for sample_index, sample in enumerate(sql_actions):
        if sample.status != "succeeded" or not sample.references.is_limited_sample:
            continue
        covered: set[str] = set()
        for aggregate in sql_actions[sample_index + 1 :]:
            if aggregate.status == "succeeded":
                covered.update(sample_coverage(sample.references, aggregate.references))
        if not covered:
            continue
        detail = drafts[sample.evidence_index]["detail"]
        detail["coverage"] = (
            "covered" if covered == set(sample.references.sample_fields) else "partial"
        )
        detail["covered_fields"] = sorted(covered)
        result = detail.get("result")
        if isinstance(result, dict):
            result.pop("sample", None)
            result["sample_rows"] = "omitted: later aggregate covers the listed fields"


def _select_evidence(
    drafts: list[dict[str, Any]], policy: CompressorPolicy
) -> list[dict[str, Any]]:
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    indexed = sorted(enumerate(drafts), key=lambda item: (order[item[1]["priority"]], item[0]))
    required = [draft for _, draft in indexed if draft["priority"] == "P0"]
    optional = [draft for _, draft in indexed if draft["priority"] != "P0"]
    remaining = max(0, policy.max_projection_evidence - len(required))
    return required + optional[:remaining]


def _has_later_successful_read(calls: tuple[CompressedToolCall, ...]) -> bool:
    return any(call.adapter == "read" and call.status == "succeeded" for call in calls)


def _action_parameters(call: CompressedToolCall, policy: CompressorPolicy) -> dict[str, Any]:
    try:
        parsed = json.loads(call.arguments.text)
    except json.JSONDecodeError:
        return _unstructured_action_parameters(call)
    if not isinstance(parsed, dict):
        return _unstructured_action_parameters(call)
    permitted = ("command", "description", "limit", "offset", "pattern", "query", "queries")
    result: dict[str, Any] = {}
    for key in permitted:
        value = parsed.get(key)
        if isinstance(value, str):
            result[key] = _limit(_safe_text(value), policy.max_projection_text_bytes)
        elif isinstance(value, int) and value >= 0:
            result[key] = value
        elif (
            key == "queries"
            and isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        ):
            result[key] = [
                _limit(_safe_text(item), policy.max_projection_text_bytes) for item in value
            ]
    return result or {"argument_shape": "structured"}


def _unstructured_action_parameters(call: CompressedToolCall) -> dict[str, Any]:
    argument_sha256 = call.summary.get("arguments_sha256")
    if isinstance(argument_sha256, str):
        return {"argument_sha256": argument_sha256}
    return {"argument_shape": "unstructured"}


def _sql_query(call: CompressedToolCall) -> str | None:
    try:
        parsed = json.loads(call.arguments.text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    for key in ("query", "sql"):
        value = parsed.get(key)
        if isinstance(value, str):
            return _safe_text(value)
    command = parsed.get("command")
    if not isinstance(command, str):
        return None
    single_quote = _SQL_READONLY_SINGLE_QUOTE.search(command)
    if single_quote is not None:
        return _safe_text(single_quote.group(1).replace("''", "'"))
    double_quote = _SQL_READONLY_DOUBLE_QUOTE.search(command)
    if double_quote is not None:
        return _safe_text(re.sub(r"`(.)", r"\1", double_quote.group(1)))
    return None


def _sql_result(output: EvidenceText | None, policy: CompressorPolicy) -> dict[str, Any]:
    text = _evidence_value(output)
    if not text:
        return {"availability": "unavailable"}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"result_excerpt": _limit(text, policy.max_projection_text_bytes)}
    if not isinstance(value, dict):
        return {"result_excerpt": _limit(text, policy.max_projection_text_bytes)}
    columns = value.get("columns")
    rows = value.get("rows")
    result: dict[str, Any] = {}
    if isinstance(columns, list) and all(isinstance(column, str) for column in columns):
        result["columns"] = [_limit(column, 128) for column in columns]
    if isinstance(rows, list):
        result["row_count"] = len(rows)
        result["sample"] = rows[:3]
    return result or {"result_excerpt": _limit(text, policy.max_projection_text_bytes)}


def _project_context_source(
    source: ProjectionContextSource,
    reference_corpus: str,
    sql_tables: set[str],
    policy: CompressorPolicy,
) -> ProjectionContext:
    if source.role == "schema":
        selected, total = _select_schema(source.text, sql_tables, policy)
    else:
        selected, total = _select_records(source.text, reference_corpus, policy)
    if selected:
        return ProjectionContext(
            role=source.role,
            selection="direct_reference",
            text="\n".join(selected),
            selected_count=len(selected),
            omitted_count=max(0, total - len(selected)),
        )
    return ProjectionContext(
        role=source.role,
        selection="fallback",
        text=_limit(source.text, policy.max_context_bytes),
        selected_count=0,
        omitted_count=max(0, total),
    )


def _select_schema(
    text: str, sql_tables: set[str], policy: CompressorPolicy
) -> tuple[list[str], int]:
    blocks = list(_DDL_TABLE.finditer(text))
    if not blocks:
        return [], 0
    selected: list[str] = []
    selected_names: set[str] = set()
    for block in blocks:
        name = _normalise_relation(block.group(1))
        if name in sql_tables:
            selected.append(_limit(block.group(0), policy.max_context_bytes))
            selected_names.add(name)
    if selected_names:
        referenced_by_selected = {
            _normalise_relation(value)
            for block in blocks
            if _normalise_relation(block.group(1)) in selected_names
            for value in re.findall(r"REFERENCES\s+([\w.\"]+)", block.group(2), re.IGNORECASE)
        }
        for block in blocks:
            name = _normalise_relation(block.group(1))
            if name in selected_names:
                continue
            references = {
                _normalise_relation(value)
                for value in re.findall(r"REFERENCES\s+([\w.\"]+)", block.group(2), re.IGNORECASE)
            }
            if name in referenced_by_selected or references & selected_names:
                selected.append(_limit(block.group(0), policy.max_context_bytes))
    return selected[: policy.max_selected_context_records], len(blocks)


def _select_records(
    text: str, reference_corpus: str, policy: CompressorPolicy
) -> tuple[list[str], int]:
    records = _records_from_text(text)
    selected: list[str] = []
    lowered_corpus = reference_corpus.casefold()
    for record in records:
        labels = _record_labels(record)
        if labels and any(_contains_reference(lowered_corpus, label) for label in labels):
            selected.append(_limit(record, policy.max_context_bytes))
    return selected[: policy.max_selected_context_records], len(records)


def _records_from_text(text: str) -> list[str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [line for line in text.splitlines() if line.strip()]
    if isinstance(parsed, list):
        return [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in parsed]
    if isinstance(parsed, dict):
        return [json.dumps(parsed, ensure_ascii=False, sort_keys=True)]
    return [text]


def _record_labels(record: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(record)
    except json.JSONDecodeError:
        return tuple(
            match.group(0) for match in _IDENTIFIER.finditer(record) if len(match.group(0)) >= 3
        )
    if not isinstance(parsed, dict):
        return ()
    labels: list[str] = []
    for key in ("id", "key", "name", "title", "code"):
        value = parsed.get(key)
        if isinstance(value, str) and len(value) >= 2:
            labels.append(value)
    return tuple(labels)


def _contains_reference(corpus: str, label: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_$]){re.escape(label.casefold())}(?![A-Za-z0-9_$])"
    return bool(re.search(pattern, corpus))


def _reference_corpus(trajectory: CompressedTrajectory) -> str:
    values: list[str] = []
    for step in trajectory.trajectory:
        values.extend(message.text for message in step.public_assistant_text)
        values.extend(call.arguments.text for call in step.tool_calls)
    if trajectory.candidate_output.sql is not None:
        values.append(trajectory.candidate_output.sql.text)
    if trajectory.candidate_output.final_message is not None:
        values.append(trajectory.candidate_output.final_message.text)
    if trajectory.evaluation.detail is not None:
        values.append(trajectory.evaluation.detail.text)
    return "\n".join(values)


def _sql_tables(trajectory: CompressedTrajectory) -> set[str]:
    tables: set[str] = set()
    for step in trajectory.trajectory:
        for call in step.tool_calls:
            if call.adapter != "sql-readonly":
                continue
            query = _sql_query(call)
            if query is not None:
                tables.update(
                    _normalise_relation(table) for table in extract_sql_references(query).tables
                )
    if trajectory.candidate_output.sql is not None:
        tables.update(
            _normalise_relation(table)
            for table in extract_sql_references(trajectory.candidate_output.sql.text).tables
        )
    return tables


def _normalise_relation(value: str) -> str:
    return value.replace('"', "").casefold()


def _summary_text(value: object) -> str | None:
    return value.text if isinstance(value, EvidenceText) else None


def _nonnegative_summary_value(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _evidence_value(value: EvidenceText | None) -> str:
    return value.text if value is not None else ""


def _outcome(status: str) -> Literal["succeeded", "failed", "unknown"]:
    if status == "succeeded":
        return "succeeded"
    if status == "failed":
        return "failed"
    return "unknown"


def _limit(value: str, limit: int) -> str:
    encoded = _safe_text(value).encode("utf-8")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    marker = "\n…<truncated>"
    prefix = encoded[: max(0, limit - len(marker.encode("utf-8")))].decode("utf-8", errors="ignore")
    return prefix + marker


def iter_identifiers(value: str) -> Iterable[str]:
    """Expose direct identifier tokens for focused unit tests and future selectors."""

    return (match.group(0) for match in _IDENTIFIER.finditer(value))
