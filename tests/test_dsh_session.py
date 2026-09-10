"""Tests for the version-pinned DSH session-log evidence adapter."""

from __future__ import annotations

import json

import pytest

from skill_evolve.runner.dsh_session import (
    DshSessionLogError,
    LoadedDshSessionLog,
    normalise_session_log,
)
from skill_evolve.runner.redaction import redact_json_value


def test_normalise_session_log_preserves_trace_and_provider_usage() -> None:
    evidence = normalise_session_log(_session_log())

    assert evidence.session_id == "session-123"
    assert evidence.final_message == "```sql\nSELECT COUNT(*) FROM customers\n```"
    assert evidence.submitted_sql is None
    assert evidence.usage.input_tokens == 15
    assert evidence.usage.output_tokens == 8
    assert [event.type for event in evidence.trajectory] == [
        "dsh_session_start",
        "dsh_assistant_message",
        "dsh_tool_call",
        "dsh_tool_result",
        "dsh_assistant_message",
        "dsh_turn_end",
    ]


def test_normalise_session_log_ignores_unknown_events_but_records_their_type() -> None:
    events = list(_session_log().events)
    events.insert(2, {"type": "future/telemetry", "seq": 2, "time": 1_700_000_000_001})

    evidence = normalise_session_log(_loaded(events))

    assert evidence.unknown_event_types == ("future/telemetry",)


def test_normalise_session_log_rejects_missing_completed_turn() -> None:
    events = [event for event in _session_log().events if event["type"] != "turn/end"]

    with pytest.raises(DshSessionLogError, match="no completed turn"):
        normalise_session_log(_loaded(events))


def test_normalise_session_log_extracts_only_successful_native_sql_submit() -> None:
    events = list(_session_log().events)
    events[4:4] = [
        {
            "type": "tool/call",
            "seq": 4,
            "time": 1_700_000_000_350,
            "data": {
                "turn": 1,
                "step": 2,
                "callId": "submit-1",
                "name": "sql_submit",
                "arguments": '{"query":"SELECT COUNT(*) FROM customers"}',
            },
        },
        {
            "type": "tool/result",
            "seq": 5,
            "time": 1_700_000_000_375,
            "data": {
                "turn": 1,
                "step": 2,
                "message": {
                    "role": "tool",
                    "id": "submit-result-1",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "submit-1",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        '{"ok":true,"intent":"submit",'
                                        '"phase":"finalize","columns":[],"rows":[]}'
                                    ),
                                }
                            ],
                            "isError": False,
                        }
                    ],
                },
            },
        },
    ]

    evidence = normalise_session_log(_loaded(events))

    assert evidence.submitted_sql == "SELECT COUNT(*) FROM customers"


def test_redaction_removes_secret_key_values_and_inline_bearer_tokens() -> None:
    event = redact_json_value(
        {
            "type": "request/header",
            "data": {"authorization": "Bearer ds-abcdefghijklmnop", "message": "token=abc"},
        }
    )

    assert event == {
        "type": "request/header",
        "data": {"authorization": "<redacted>", "message": "token=<redacted>"},
    }


def _session_log() -> LoadedDshSessionLog:
    return _loaded(
        [
            {
                "type": "session",
                "version": 0,
                "id": "session-123",
                "createdAt": 1_700_000_000_000,
                "cwd": "D:/safe-workspace",
                "delegationDepth": 0,
            },
            {
                "type": "assistant/message",
                "seq": 1,
                "time": 1_700_000_000_100,
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "role": "assistant",
                        "id": "message-1",
                        "content": [{"type": "text", "text": "I will inspect the schema."}],
                    },
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                },
            },
            {
                "type": "tool/call",
                "seq": 2,
                "time": 1_700_000_000_200,
                "data": {
                    "turn": 1,
                    "step": 1,
                    "callId": "call-1",
                    "name": "read",
                    "arguments": "{\"path\":\"schema.sql\"}",
                },
            },
            {
                "type": "tool/result",
                "seq": 3,
                "time": 1_700_000_000_300,
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "role": "tool",
                        "id": "result-1",
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "call-1",
                                "content": [{"type": "text", "text": "customers"}],
                                "isError": False,
                            }
                        ],
                    },
                },
            },
            {
                "type": "assistant/message",
                "seq": 4,
                "time": 1_700_000_000_400,
                "data": {
                    "turn": 1,
                    "step": 2,
                    "message": {
                        "role": "assistant",
                        "id": "message-2",
                        "content": [
                            {
                                "type": "text",
                                "text": "```sql\nSELECT COUNT(*) FROM customers\n```",
                            }
                        ],
                    },
                    "usage": {"inputTokens": 5, "outputTokens": 3},
                },
            },
            {
                "type": "turn/end",
                "seq": 5,
                "time": 1_700_000_000_500,
                "data": {"turn": 1, "reason": {"kind": "completed"}},
            },
        ]
    )


def _loaded(events: list[dict[str, object]]) -> LoadedDshSessionLog:
    return LoadedDshSessionLog(
        events=tuple(events),
        redacted_jsonl=(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events).encode("utf-8")
        ),
    )
