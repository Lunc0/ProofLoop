"""Rule-based compression of already-redacted DSH session JSONL evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from skill_evolve.evaluator.core import CaseEvaluation
from skill_evolve.runner.contracts import RunnerCaseOutput
from skill_evolve.runner.dsh_session import DshSessionLogError, load_redacted_session_jsonl
from skill_evolve.runner.redaction import redact_text
from skill_evolve.trajectory.contracts import (
    CandidateOutput,
    CompressedStep,
    CompressedToolCall,
    CompressedTrajectory,
    CompressionStats,
    DirectFailure,
    EvidenceText,
    OrphanToolResult,
    PublicEvaluation,
    SourceLocation,
    TraceState,
    TrajectorySignal,
    TrajectorySource,
)
from skill_evolve.trajectory.policy import DEFAULT_COMPRESSOR_POLICY, CompressorPolicy

_SESSION_VERSION = 0
_RELEVANT_EVENT_TYPES = frozenset({"assistant/message", "tool/call", "tool/result", "turn/end"})
_IGNORED_EVENT_TYPES = frozenset(
    {
        "permission/preset",
        "sandbox/mode",
        "approval/policy",
        "agent/inbox/spliced",
        "turn/start",
        "step/start",
        "step/end",
        "user/message",
        "session/title",
        "request/header",
        "request/context",
        "session/title-llm-request",
        "assistant/chunk",
        "reasoning-chunks",
        "tool-call-chunks",
        "text-chunks",
    }
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:(?:\\|/(?!/))[^\s\"']+|/(?:Users|home|var|tmp)/[^\s\"']+)"
)
_EXIT_CODE_PATTERN = re.compile(r"(?i)(?:\[\s*)?exit\s*code\s*[:=]?\s*(\d+)")
_MULTIPLE_STATEMENTS_PATTERN = re.compile(
    r"(?i)multiple\s+(?:sql\s+)?statements?\s+(?:are\s+)?not\s+allowed"
)
_SQL_SYNTAX_PATTERN = re.compile(r"(?i)syntax\s+error")
_MISSING_RELATION_PATTERN = re.compile(r"(?i)(?:relation|table)\s+.+\s+does\s+not\s+exist")
_MISSING_COLUMN_PATTERN = re.compile(r"(?i)column\s+.+\s+does\s+not\s+exist")
_TIMEOUT_PATTERN = re.compile(r"(?i)timed?\s*out|timeout")


class TrajectoryCompressionError(ValueError):
    """A redacted session cannot be safely projected into agent-visible evidence."""


@dataclass
class _ToolResult:
    location: SourceLocation
    content: Any
    text: str
    is_error: bool
    meta: Mapping[str, Any]


@dataclass
class _ToolCall:
    call_id: str
    name: str
    location: SourceLocation
    arguments: str
    result: _ToolResult | None = None


@dataclass
class _StepContents:
    text: list[EvidenceText]
    calls: list[_ToolCall]


def canonical_json_bytes(value: object) -> bytes:
    """Serialise artifacts deterministically without relying on filesystem writers."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def compress_session_jsonl(
    redacted_jsonl: bytes,
    runner_case: RunnerCaseOutput,
    evaluation: CaseEvaluation,
    *,
    runner_outcome: str,
    deadline_ms: int | None = None,
    policy: CompressorPolicy = DEFAULT_COMPRESSOR_POLICY,
    evidence_ref: str = "custody-only opaque reference",
) -> CompressedTrajectory:
    """Build one reproducible compressed trajectory from runner-owned JSONL.

    ``redacted_jsonl`` must have been emitted by the DSH runner. The function
    accepts incomplete sessions so that actionable timeout evidence is not lost.
    It never inspects evaluator-only fields on ``evaluation``.
    """

    try:
        log = load_redacted_session_jsonl(redacted_jsonl)
    except DshSessionLogError as error:
        raise TrajectoryCompressionError(str(error)) from error
    if len(log.events) > policy.max_events:
        raise TrajectoryCompressionError("session log exceeds trajectory event limit")
    header = _validate_header(log.events[0])
    steps: dict[tuple[int, int], _StepContents] = defaultdict(lambda: _StepContents([], []))
    calls: dict[tuple[int, str], _ToolCall] = {}
    orphan_results: list[OrphanToolResult] = []
    unknown_event_types: Counter[str] = Counter()
    truncated_fields: list[str] = []
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    completed_turn = False
    last_observed: SourceLocation | None = None

    for index, event in _ordered_events(log.events[1:]):
        event_type = _event_type(event)
        if event_type in _IGNORED_EVENT_TYPES:
            continue
        if event_type not in _RELEVANT_EVENT_TYPES:
            unknown_event_types[event_type] += 1
            continue
        if event_type == "assistant/message":
            location, text, usage = _assistant_message(event, index, policy, truncated_fields)
            last_observed = location
            if usage[0] is not None:
                input_tokens.append(usage[0])
            if usage[1] is not None:
                output_tokens.append(usage[1])
            if text is not None:
                steps[_step_key(location)].text.append(text)
            continue
        if event_type == "tool/call":
            tool_call = _tool_call(event, index)
            last_observed = tool_call.location
            key = (tool_call.location.turn or 0, tool_call.call_id)
            if key in calls:
                raise TrajectoryCompressionError("session contains duplicate tool call id")
            calls[key] = tool_call
            steps[_step_key(tool_call.location)].calls.append(tool_call)
            continue
        if event_type == "tool/result":
            call_id, result = _tool_result(event, index)
            last_observed = result.location
            key = (result.location.turn or 0, call_id)
            call = calls.get(key)
            if call is None:
                orphan_results.append(
                    OrphanToolResult(
                        location=result.location,
                        call_id=call_id,
                        output=_bounded_evidence(
                            result.text,
                            policy.max_tool_result_bytes,
                            "orphan_tool_result",
                            truncated_fields,
                        ),
                    )
                )
            elif call.result is None:
                call.result = result
            else:
                raise TrajectoryCompressionError("tool call has multiple results")
            continue
        location, reason = _turn_end(event, index)
        last_observed = location
        completed_turn = completed_turn or reason == "completed"

    trajectory = _compressed_steps(steps, policy, truncated_fields)
    has_public_assistant_text = any(step.public_assistant_text for step in trajectory)
    trace = _trace_state(
        completed_turn,
        has_public_assistant_text,
        trajectory,
        runner_case,
        runner_outcome,
        deadline_ms,
        last_observed,
    )
    candidate_output = CandidateOutput(
        sql=_optional_evidence(
            runner_case.candidate_sql,
            policy.max_tool_arguments_bytes,
            "candidate_sql",
            truncated_fields,
        ),
        final_message=_optional_evidence(
            runner_case.final_message,
            policy.max_assistant_text_bytes,
            "final_message",
            truncated_fields,
        ),
    )
    public_evaluation = _public_evaluation(evaluation, policy, truncated_fields)
    signals = _signals(trajectory, input_tokens, output_tokens, policy)
    missing_results = sum(
        1 for step in trajectory for call in step.tool_calls if call.result is None
    )
    source = TrajectorySource(
        redacted_session_sha256=hashlib.sha256(redacted_jsonl).hexdigest(),
        session_version=header["version"],
        compressor_rule_version=policy.rule_version,
        compressor_policy_sha256=policy.sha256,
        evidence_ref=evidence_ref,
    )
    draft = CompressedTrajectory(
        source=source,
        trace=trace,
        trajectory=trajectory,
        orphan_tool_results=tuple(orphan_results),
        signals=signals,
        candidate_output=candidate_output,
        evaluation=public_evaluation,
        compression=CompressionStats(
            raw_event_count=len(log.events),
            raw_bytes=len(redacted_jsonl),
            compressed_bytes=0,
            artifact_sha256="0" * 64,
            truncated_fields=tuple(sorted(set(truncated_fields))),
            unhandled_event_type_counts=dict(sorted(unknown_event_types.items())),
            orphan_result_count=len(orphan_results),
            missing_result_count=missing_results,
        ),
    )
    return _with_artifact_digest(draft, policy)


def _validate_header(event: Mapping[str, Any]) -> dict[str, int]:
    if event.get("type") != "session":
        raise TrajectoryCompressionError("session log does not begin with a session header")
    version = event.get("version")
    if version != _SESSION_VERSION:
        raise TrajectoryCompressionError("unsupported DSH session-log version")
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise TrajectoryCompressionError("session header has no id")
    return {"version": version}


def _ordered_events(events: Iterable[Mapping[str, Any]]) -> list[tuple[int, Mapping[str, Any]]]:
    indexed = list(enumerate(events, start=1))

    def key(item: tuple[int, Mapping[str, Any]]) -> tuple[int, int, int, int]:
        index, event = item
        data = event.get("data")
        turn = data.get("turn") if isinstance(data, Mapping) else None
        step = data.get("step") if isinstance(data, Mapping) else None
        seq = event.get("seq")
        return (_order_int(turn), _order_int(step), _order_int(seq, index), index)

    return sorted(indexed, key=key)


def _event_type(event: Mapping[str, Any]) -> str:
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise TrajectoryCompressionError("session event has no string type")
    return event_type


def _assistant_message(
    event: Mapping[str, Any],
    index: int,
    policy: CompressorPolicy,
    truncated_fields: list[str],
) -> tuple[SourceLocation, EvidenceText | None, tuple[int | None, int | None]]:
    location = _location(event, index, "assistant/message")
    data = _data_mapping(event, "assistant/message")
    message = data.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        raise TrajectoryCompressionError("assistant/message is not an assistant message")
    text = _public_text(message.get("content"), "assistant/message")
    usage = data.get("usage")
    input_tokens = _optional_nonnegative(usage, "inputTokens")
    output_tokens = _optional_nonnegative(usage, "outputTokens")
    evidence = (
        _bounded_evidence(
            text, policy.max_assistant_text_bytes, "assistant_text", truncated_fields
        )
        if text
        else None
    )
    return location, evidence, (input_tokens, output_tokens)


def _tool_call(event: Mapping[str, Any], index: int) -> _ToolCall:
    location = _location(event, index, "tool/call")
    data = _data_mapping(event, "tool/call")
    call_id = data.get("callId")
    name = data.get("name")
    arguments = data.get("arguments")
    if not isinstance(call_id, str) or not call_id:
        raise TrajectoryCompressionError("tool/call has no callId")
    if not isinstance(name, str) or not name:
        raise TrajectoryCompressionError("tool/call has no name")
    if not isinstance(arguments, str):
        raise TrajectoryCompressionError("tool/call has no string arguments")
    return _ToolCall(call_id=call_id, name=name, location=location, arguments=arguments)


def _tool_result(event: Mapping[str, Any], index: int) -> tuple[str, _ToolResult]:
    location = _location(event, index, "tool/result")
    data = _data_mapping(event, "tool/result")
    message = data.get("message")
    if not isinstance(message, Mapping):
        raise TrajectoryCompressionError("tool/result has no message")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise TrajectoryCompressionError("tool/result has an invalid content block")
    block = content[0]
    if block.get("type") != "tool-result":
        raise TrajectoryCompressionError("tool/result has an unsupported content block")
    call_id = block.get("toolCallId")
    is_error = block.get("isError")
    if not isinstance(call_id, str) or not call_id:
        raise TrajectoryCompressionError("tool/result has no toolCallId")
    if not isinstance(is_error, bool):
        raise TrajectoryCompressionError("tool/result has no isError flag")
    meta = data.get("meta")
    return call_id, _ToolResult(
        location=location,
        content=block.get("content"),
        text=_content_text(block.get("content")),
        is_error=is_error,
        meta=meta if isinstance(meta, Mapping) else {},
    )


def _turn_end(event: Mapping[str, Any], index: int) -> tuple[SourceLocation, str]:
    location = _location(event, index, "turn/end")
    reason = _data_mapping(event, "turn/end").get("reason")
    if not isinstance(reason, Mapping) or not isinstance(reason.get("kind"), str):
        raise TrajectoryCompressionError("turn/end has no reason kind")
    return location, reason["kind"]


def _location(event: Mapping[str, Any], index: int, event_type: str) -> SourceLocation:
    data = _data_mapping(event, event_type)
    raw_seq = event.get("seq")
    seq = (
        raw_seq
        if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) and raw_seq >= 0
        else index
    )
    turn = data.get("turn")
    step = data.get("step")
    return SourceLocation(
        turn=turn if _is_nonnegative_int(turn) else None,
        step=step if _is_nonnegative_int(step) else None,
        seq=seq,
        event_type=event_type,
        observed_time_ms=_optional_event_time(event),
    )


def _data_mapping(event: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    data = event.get("data")
    if not isinstance(data, Mapping):
        raise TrajectoryCompressionError(f"{label} has no data object")
    return data


def _optional_event_time(event: Mapping[str, Any]) -> int | None:
    value = event.get("time")
    return value if _is_nonnegative_int(value) else None


def _step_key(location: SourceLocation) -> tuple[int, int]:
    if location.turn is None or location.step is None:
        raise TrajectoryCompressionError(f"{location.event_type} has no turn/step")
    return location.turn, location.step


def _public_text(content: object, label: str) -> str:
    if not isinstance(content, list):
        raise TrajectoryCompressionError(f"{label} content is not a list")
    values: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise TrajectoryCompressionError(f"{label} content contains a non-object")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise TrajectoryCompressionError(f"{label} text has no string value")
            values.append(text)
    return "\n".join(values).strip()


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    values.append(text)
        if values:
            return "\n".join(values)
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)


def _optional_nonnegative(usage: object, key: str) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(key)
    return value if _is_nonnegative_int(value) else None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _order_int(value: object, fallback: int = 2**31 - 1) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def _compressed_steps(
    steps: Mapping[tuple[int, int], _StepContents],
    policy: CompressorPolicy,
    truncated_fields: list[str],
) -> tuple[CompressedStep, ...]:
    result: list[CompressedStep] = []
    for (turn, step), contents in sorted(steps.items()):
        result.append(
            CompressedStep(
                turn=turn,
                step=step,
                public_assistant_text=tuple(contents.text),
                tool_calls=tuple(
                    _compressed_call(call, policy, truncated_fields) for call in contents.calls
                ),
            )
        )
    return tuple(result)


def _compressed_call(
    call: _ToolCall, policy: CompressorPolicy, truncated_fields: list[str]
) -> CompressedToolCall:
    result = call.result
    adapter = _adapter_name(call.name, call.arguments)
    arguments = _bounded_evidence(
        call.arguments, policy.max_tool_arguments_bytes, "tool_arguments", truncated_fields
    )
    output = (
        _bounded_evidence(
            result.text, policy.max_tool_result_bytes, "tool_result", truncated_fields
        )
        if result is not None
        else None
    )
    direct_failure = _direct_failure(result)
    status: Literal["succeeded", "failed", "unknown"]
    if result is None:
        status = "unknown"
    elif direct_failure is not None:
        status = "failed"
    else:
        status = "succeeded"
    return CompressedToolCall(
        call_id=call.call_id,
        name=call.name,
        adapter=adapter,
        call=call.location,
        result=result.location if result is not None else None,
        arguments=arguments,
        output=output,
        status=status,
        direct_failure=direct_failure,
        summary=_tool_summary(adapter, call.name, call.arguments, result, policy, truncated_fields),
    )


def _adapter_name(name: str, arguments: str) -> str:
    parsed = _json_object(arguments)
    command = parsed.get("command") if parsed is not None else None
    if name == "pwsh" and isinstance(command, str) and "sql-readonly" in command.lower():
        return "sql-readonly"
    if name in {"read", "glob", "pwsh", "sql-readonly", "web_search"}:
        return name
    return "generic"


def _tool_summary(
    adapter: str,
    name: str,
    arguments: str,
    result: _ToolResult | None,
    policy: CompressorPolicy,
    truncated_fields: list[str],
) -> dict[str, Any]:
    parsed = _json_object(arguments)
    summary: dict[str, Any] = {
        "untrusted_evidence": True,
        "arguments_sha256": hashlib.sha256(_safe_text(arguments).encode("utf-8")).hexdigest(),
        "deduplication_sha256": _deduplication_sha256(adapter, name, arguments),
    }
    if result is not None:
        summary["result_sha256"] = hashlib.sha256(
            _safe_text(result.text).encode("utf-8")
        ).hexdigest()
    if adapter == "read" and parsed is not None:
        target = _read_target(parsed)
        if target is not None:
            summary["target"] = _summary_evidence(
                target, "read_target", policy, truncated_fields
            )
        _copy_nonnegative(parsed, summary, "offset")
        _copy_nonnegative(parsed, summary, "limit")
    elif adapter == "glob" and parsed is not None:
        _copy_summary_text(parsed, summary, "pattern", policy, truncated_fields)
        if result is not None:
            paths = result.meta.get("paths")
            if isinstance(paths, list) and all(isinstance(path, str) for path in paths):
                ordered = sorted(_safe_text(path) for path in paths)
                summary["match_count"] = len(ordered)
                summary["sample_paths"] = [
                    _summary_evidence(path, "glob_path", policy, truncated_fields)
                    for path in ordered[: policy.max_list_items]
                ]
                summary["omitted_count"] = max(0, len(ordered) - policy.max_list_items)
    elif adapter == "sql-readonly" and parsed is not None:
        command = parsed.get("command")
        if isinstance(command, str):
            summary["command_sha256"] = hashlib.sha256(
                _safe_text(command).encode("utf-8")
            ).hexdigest()
    elif adapter == "web_search" and parsed is not None:
        queries = parsed.get("queries")
        if isinstance(queries, list) and all(isinstance(query, str) for query in queries):
            summary["queries"] = [
                _summary_evidence(query, "web_query", policy, truncated_fields)
                for query in queries[: policy.max_list_items]
            ]
            summary["omitted_query_count"] = max(0, len(queries) - policy.max_list_items)
        if result is not None:
            summary["retrieved_at_ms"] = result.location.observed_time_ms
            sources = result.meta.get("sources")
            if isinstance(sources, list):
                public_sources = _web_sources(sources, policy, truncated_fields)
                summary["source_count"] = len(public_sources)
                summary["sources"] = public_sources[: policy.max_list_items]
                summary["omitted_source_count"] = max(
                    0, len(public_sources) - policy.max_list_items
                )
    elif adapter == "pwsh" and parsed is not None:
        _copy_summary_text(parsed, summary, "description", policy, truncated_fields)
        command = parsed.get("command")
        if isinstance(command, str):
            summary["command_sha256"] = hashlib.sha256(
                _safe_text(command).encode("utf-8")
            ).hexdigest()
    return summary


def _read_target(source: Mapping[str, object]) -> str | None:
    for key in ("path", "file_path"):
        value = source.get(key)
        if isinstance(value, str):
            return _safe_path_reference(value)
    return None


def _copy_summary_text(
    source: Mapping[str, object],
    destination: dict[str, Any],
    key: str,
    policy: CompressorPolicy,
    truncated_fields: list[str],
) -> None:
    value = source.get(key)
    if isinstance(value, str):
        destination[key] = _summary_evidence(value, f"summary_{key}", policy, truncated_fields)


def _web_sources(
    values: list[object], policy: CompressorPolicy, truncated_fields: list[str]
) -> list[dict[str, EvidenceText]]:
    public_sources: list[tuple[str, str, str | None]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        title = value.get("title")
        url = value.get("url")
        snippet = value.get("snippet")
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        public_sources.append(
            (
                _safe_text(title),
                _safe_text(url),
                _safe_text(snippet) if isinstance(snippet, str) else None,
            )
        )
    sources: list[dict[str, EvidenceText]] = []
    for title, url, snippet in sorted(public_sources, key=lambda source: (source[1], source[0])):
        source = {
            "title": _summary_evidence(title, "web_title", policy, truncated_fields),
            "url": _summary_evidence(url, "web_url", policy, truncated_fields),
        }
        if snippet is not None:
            source["snippet"] = _summary_evidence(
                snippet, "web_snippet", policy, truncated_fields
            )
        sources.append(source)
    return sources


def _summary_evidence(
    value: str, field: str, policy: CompressorPolicy, truncated_fields: list[str]
) -> EvidenceText:
    return _bounded_evidence(value, policy.max_summary_text_bytes, field, truncated_fields)


def _deduplication_sha256(adapter: str, name: str, arguments: str) -> str:
    parsed = _json_object(arguments)
    safe_arguments: object = (
        _safe_text(arguments) if parsed is None else _safe_identity_value(parsed)
    )
    payload = {"adapter": adapter, "name": name, "arguments": safe_arguments}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _safe_identity_value(value: object, key: str | None = None) -> object:
    if isinstance(value, str):
        if key in {"file_path", "path"}:
            return _safe_path_reference(value)
        return _safe_text(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_identity_value(child_value, str(child_key))
            for child_key, child_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_safe_identity_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(str(value))


def _safe_path_reference(value: str) -> str:
    normalised = value.replace("\\", "/")
    components = tuple(component for component in normalised.split("/") if component)
    for index, component in enumerate(components):
        if component.lower() == "agent-workspace" and index + 1 < len(components):
            return "workspace/" + "/".join(components[index + 1 :])
    if _ABSOLUTE_PATH_PATTERN.search(value):
        basename = components[-1] if components else "unknown"
        return "external/" + _safe_text(basename)
    return _safe_text(normalised)


def _copy_nonnegative(source: Mapping[str, object], destination: dict[str, Any], key: str) -> None:
    value = source.get(key)
    if _is_nonnegative_int(value):
        destination[key] = value


def _json_object(value: str) -> Mapping[str, object] | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _direct_failure(result: _ToolResult | None) -> DirectFailure | None:
    if result is None:
        return None
    if result.is_error:
        return _failure("tool_execution", "tool_result_is_error", ("is_error",))
    structured = _structured_failure(result.content)
    if structured is not None:
        return structured
    text = _safe_text(result.text)
    if _MULTIPLE_STATEMENTS_PATTERN.search(text):
        return _failure("invalid_request", "multiple_sql_statements", ("result_text",))
    if _SQL_SYNTAX_PATTERN.search(text):
        return _failure("sql_execution", "sql_syntax_error", ("result_text",))
    if _MISSING_RELATION_PATTERN.search(text):
        return _failure("sql_execution", "missing_relation", ("result_text",))
    if _MISSING_COLUMN_PATTERN.search(text):
        return _failure("sql_execution", "missing_column", ("result_text",))
    if _TIMEOUT_PATTERN.search(text):
        return _failure("timeout", "tool_timeout", ("result_text",))
    exit_code = _EXIT_CODE_PATTERN.search(text)
    if exit_code is not None and int(exit_code.group(1)) != 0:
        return _failure("tool_execution", "nonzero_exit_code", ("result_text.exit_code",))
    return None


def _structured_failure(content: object) -> DirectFailure | None:
    if not isinstance(content, Mapping):
        return None
    status = content.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failed", "timeout"}:
        return _failure("tool_execution", f"structured_status_{status.lower()}", ("status",))
    ok = content.get("ok")
    if ok is False:
        return _failure("tool_execution", "structured_ok_false", ("ok",))
    exit_code = content.get("exit_code", content.get("exitCode"))
    if _is_nonnegative_int(exit_code) and exit_code != 0:
        return _failure("tool_execution", "nonzero_exit_code", ("exit_code",))
    sqlstate = content.get("sqlstate", content.get("sqlState"))
    if isinstance(sqlstate, str) and sqlstate:
        return _failure("sql_execution", "sqlstate", ("sqlstate",))
    return None


def _failure(category: str, cause: str, fields: tuple[str, ...]) -> DirectFailure:
    return DirectFailure(
        category=category,
        direct_cause=cause,
        confidence="high",
        evidence_fields=fields,
    )


def _trace_state(
    completed_turn: bool,
    has_public_assistant_text: bool,
    trajectory: tuple[CompressedStep, ...],
    runner_case: RunnerCaseOutput,
    runner_outcome: str,
    deadline_ms: int | None,
    last_observed: SourceLocation | None,
) -> TraceState:
    has_action = any(step.tool_calls or step.public_assistant_text for step in trajectory)
    has_candidate = bool(runner_case.candidate_sql or runner_case.final_message)
    missing: list[str] = []
    if not completed_turn:
        missing.append("turn/end")
    if not has_public_assistant_text:
        missing.append("final_assistant_text")
    if completed_turn and has_public_assistant_text:
        completeness: Literal["complete", "partial_actionable", "partial_insufficient"] = "complete"
    elif has_action or has_candidate:
        completeness = "partial_actionable"
    else:
        completeness = "partial_insufficient"
    return TraceState(
        trace_completeness=completeness,
        runner_outcome=runner_outcome,
        deadline_ms=deadline_ms,
        last_observed=last_observed,
        missing_terminal_events=tuple(missing),
    )


def _public_evaluation(
    evaluation: CaseEvaluation, policy: CompressorPolicy, truncated_fields: list[str]
) -> PublicEvaluation:
    return PublicEvaluation(
        case_id=evaluation.case_id,
        slice=evaluation.slice,
        verdict=evaluation.verdict.value,
        candidate_sql=_optional_evidence(
            evaluation.candidate_sql,
            policy.max_tool_arguments_bytes,
            "evaluation_candidate_sql",
            truncated_fields,
        ),
        detail=_optional_evidence(
            evaluation.detail,
            policy.max_tool_result_bytes,
            "evaluation_detail",
            truncated_fields,
        ),
        usage=dict(sorted(evaluation.usage.items())),
    )


def _signals(
    trajectory: tuple[CompressedStep, ...],
    input_tokens: list[int],
    output_tokens: list[int],
    policy: CompressorPolicy,
) -> tuple[TrajectorySignal, ...]:
    calls = [call for step in trajectory for call in step.tool_calls]
    signals: list[TrajectorySignal] = []
    failures = [call for call in calls if call.status == "failed"]
    if failures:
        signals.append(
            TrajectorySignal(
                kind="tool_failure",
                count=len(failures),
                call_ids=tuple(call.call_id for call in failures),
            )
        )
    grouped: dict[tuple[str, str], list[CompressedToolCall]] = defaultdict(list)
    for call in calls:
        deduplication_sha256 = call.summary.get("deduplication_sha256")
        if not isinstance(deduplication_sha256, str):
            raise AssertionError("compressed tool call is missing a deduplication hash")
        grouped[(call.adapter, deduplication_sha256)].append(call)
    for (adapter, _), group in sorted(grouped.items()):
        if len(group) > 1:
            signals.append(
                TrajectorySignal(
                    kind="repeated_exact_tool_call",
                    count=len(group),
                    call_ids=tuple(call.call_id for call in group),
                    detail={"tool_adapter": adapter},
                )
            )
    post_failure_successes = [
        call
        for call in calls
        if call.status == "succeeded"
        and any(
            failed.adapter == call.adapter
            and failed.result is not None
            and failed.result.seq < call.call.seq
            for failed in failures
        )
    ]
    if post_failure_successes:
        signals.append(
            TrajectorySignal(
                kind="post_failure_same_adapter_success",
                count=len(post_failure_successes),
                call_ids=tuple(call.call_id for call in post_failure_successes),
                detail={"relationship": "temporal_only"},
            )
        )
    if len(calls) > policy.high_tool_call_count:
        signals.append(
            TrajectorySignal(kind="high_tool_call_count", count=len(calls))
        )
    total_tokens = sum(input_tokens) + sum(output_tokens)
    if total_tokens > policy.high_total_tokens:
        signals.append(
            TrajectorySignal(
                kind="high_token_count",
                count=total_tokens,
                detail={"input_tokens": sum(input_tokens), "output_tokens": sum(output_tokens)},
            )
        )
    return tuple(signals)


def _optional_evidence(
    value: str | None, limit: int, field: str, truncated_fields: list[str]
) -> EvidenceText | None:
    if not value:
        return None
    return _bounded_evidence(value, limit, field, truncated_fields)


def _bounded_evidence(
    value: str, limit: int, field: str, truncated_fields: list[str]
) -> EvidenceText:
    safe = _safe_text(value)
    encoded = safe.encode("utf-8")
    text = _truncate_utf8(safe, limit)
    truncated = len(text.encode("utf-8")) < len(encoded)
    if truncated:
        truncated_fields.append(field)
    return EvidenceText(
        text=text,
        sha256=hashlib.sha256(encoded).hexdigest(),
        original_bytes=len(encoded),
        truncated=truncated,
    )


def _safe_text(value: str) -> str:
    redacted = redact_text(value)
    return _ABSOLUTE_PATH_PATTERN.sub("<absolute-path>", redacted)


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "\n…<truncated>"
    marker_bytes = marker.encode("utf-8")
    permitted = max(0, limit - len(marker_bytes))
    prefix = encoded[:permitted].decode("utf-8", errors="ignore")
    return prefix + marker


def _with_artifact_digest(
    artifact: CompressedTrajectory, policy: CompressorPolicy
) -> CompressedTrajectory:
    current = artifact
    for _ in range(3):
        payload = canonical_json_bytes(current.model_dump(mode="json"))
        stats = current.compression.model_copy(update={"compressed_bytes": len(payload)})
        current = current.model_copy(update={"compression": stats})
    payload_without_digest = current.model_dump(
        mode="json", exclude={"compression": {"artifact_sha256"}}
    )
    digest = hashlib.sha256(canonical_json_bytes(payload_without_digest)).hexdigest()
    final = current.model_copy(
        update={"compression": current.compression.model_copy(update={"artifact_sha256": digest})}
    )
    if len(canonical_json_bytes(final.model_dump(mode="json"))) > policy.max_trajectory_bytes:
        raise TrajectoryCompressionError("compressed trajectory exceeds size limit")
    return final
