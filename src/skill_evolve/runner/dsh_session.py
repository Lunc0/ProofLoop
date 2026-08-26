"""Load, redact, and normalise the observed DSH append-only session-log protocol."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skill_evolve.runner.contracts import TrajectoryEvent, Usage
from skill_evolve.runner.redaction import redact_json_value

_SESSION_LOG_FILENAME = "session.jsonl.zstd"
_ZSTD_MAGIC = bytes((0x28, 0xB5, 0x2F, 0xFD))
_MAX_COMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = 20 * 1024 * 1024
_OBSERVED_SESSION_VERSION = 0
_OBSERVED_EVENT_TYPES = frozenset(
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
        "assistant/message",
        "reasoning-chunks",
        "tool-call-chunks",
        "text-chunks",
        "tool/call",
        "tool/result",
        "turn/end",
    }
)

_NODE_DECOMPRESS_SCRIPT = f"""
const fs = require('node:fs');
const zlib = require('node:zlib');
const source = fs.readFileSync(process.argv[1]);
const magic = Buffer.from([{', '.join(str(value) for value in _ZSTD_MAGIC)}]);
const maximum = {_MAX_DECOMPRESSED_BYTES};
if (source.length > {_MAX_COMPRESSED_BYTES}) {{
  throw new Error('session log exceeds compressed size limit');
}}
const offsets = [];
for (let offset = 0; (offset = source.indexOf(magic, offset)) >= 0; offset += magic.length) {{
  offsets.push(offset);
}}
if (offsets.length === 0) throw new Error('session log contains no zstd frames');
offsets.push(source.length);
const chunks = [];
let total = 0;
for (let index = 0; index < offsets.length - 1; index += 1) {{
  const frame = zlib.zstdDecompressSync(
    source.subarray(offsets[index], offsets[index + 1]),
    {{ maxOutputLength: maximum }},
  );
  total += frame.length;
  if (total > maximum) throw new Error('session log exceeds decompressed size limit');
  chunks.push(frame);
}}
process.stdout.write(Buffer.concat(chunks));
"""


class DshSessionLogError(RuntimeError):
    """A session log cannot be used as complete, trustworthy runner evidence."""


@dataclass(frozen=True)
class SessionLogSignature:
    size: int
    modified_ns: int


@dataclass(frozen=True)
class LoadedDshSessionLog:
    events: tuple[dict[str, Any], ...]
    redacted_jsonl: bytes


@dataclass(frozen=True)
class SessionEvidence:
    session_id: str
    final_message: str
    trajectory: tuple[TrajectoryEvent, ...]
    usage: Usage
    unknown_event_types: tuple[str, ...]


def snapshot_session_logs(root: Path) -> dict[Path, SessionLogSignature]:
    """Collect only DSH session-log files from the declared root."""

    if not root.is_dir():
        raise DshSessionLogError(f"DSH session-log root is unavailable: {root}")
    snapshots: dict[Path, SessionLogSignature] = {}
    for path in root.rglob(_SESSION_LOG_FILENAME):
        try:
            stat = path.stat()
        except OSError as error:
            raise DshSessionLogError(f"could not stat DSH session log: {error}") from error
        snapshots[path] = SessionLogSignature(size=stat.st_size, modified_ns=stat.st_mtime_ns)
    return snapshots


def find_new_session_log(
    before: dict[Path, SessionLogSignature], root: Path
) -> Path:
    """Require exactly one new or changed log because DSH runs are serial."""

    changed = find_changed_session_logs(before, root)
    if len(changed) != 1:
        raise DshSessionLogError(
            f"expected exactly one changed DSH session log, found {len(changed)}"
        )
    return changed[0]


def find_changed_session_logs(
    before: dict[Path, SessionLogSignature], root: Path
) -> tuple[Path, ...]:
    """Return every changed log so callers can bind evidence to a task workspace."""

    after = snapshot_session_logs(root)
    return tuple(path for path, signature in after.items() if before.get(path) != signature)


def load_session_log(path: Path, node_binary: str = "node") -> LoadedDshSessionLog:
    """Decode a concatenated-zstd JSONL log using the Node runtime required by DSH."""

    node = shutil.which(node_binary) or node_binary
    try:
        completed = subprocess.run(
            [node, "-e", _NODE_DECOMPRESS_SCRIPT, str(path)],
            check=False,
            capture_output=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DshSessionLogError(
            f"could not decode DSH session log: {type(error).__name__}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DshSessionLogError(f"could not decode DSH session log: {detail[:300]}")
    if len(completed.stdout) > _MAX_DECOMPRESSED_BYTES:
        raise DshSessionLogError("session log exceeds decompressed size limit")
    return _load_jsonl(completed.stdout.decode("utf-8", errors="strict"))


def normalise_session_log(log: LoadedDshSessionLog) -> SessionEvidence:
    """Convert only the observed protocol into stable internal trajectory events."""

    if not log.events:
        raise DshSessionLogError("session log is empty")
    header = log.events[0]
    if header.get("type") != "session":
        raise DshSessionLogError("session log does not begin with a session header")
    if header.get("version") != _OBSERVED_SESSION_VERSION:
        raise DshSessionLogError("unsupported DSH session-log version")
    session_id = _required_string(header, "id", "session header")
    created_at = _timestamp(header.get("createdAt"), "session header createdAt")

    trajectory: list[TrajectoryEvent] = [
        TrajectoryEvent(
            timestamp=created_at,
            type="dsh_session_start",
            payload={"session_id": session_id, "version": _OBSERVED_SESSION_VERSION},
        )
    ]
    final_messages: list[str] = []
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    unknown_event_types: set[str] = set()
    completed_turn = False

    for event in log.events[1:]:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise DshSessionLogError("session event has no string type")
        if event_type not in _OBSERVED_EVENT_TYPES:
            unknown_event_types.add(event_type)
            continue
        if event_type == "assistant/message":
            message, usage = _normalise_assistant_message(event)
            trajectory.append(message)
            if usage.input_tokens is not None:
                input_tokens.append(usage.input_tokens)
            if usage.output_tokens is not None:
                output_tokens.append(usage.output_tokens)
            content = message.payload["content"]
            if content:
                final_messages.append(content)
        elif event_type == "tool/call":
            trajectory.append(_normalise_tool_call(event))
        elif event_type == "tool/result":
            trajectory.append(_normalise_tool_result(event))
        elif event_type == "turn/end":
            completed_turn = _normalise_turn_end(event, trajectory) or completed_turn

    if not completed_turn:
        raise DshSessionLogError("session log has no completed turn")
    if not final_messages:
        raise DshSessionLogError("session log has no assistant final message")
    return SessionEvidence(
        session_id=session_id,
        final_message=final_messages[-1],
        trajectory=tuple(trajectory),
        usage=Usage(
            input_tokens=sum(input_tokens) if input_tokens else None,
            output_tokens=sum(output_tokens) if output_tokens else None,
            cost_usd=None,
        ),
        unknown_event_types=tuple(sorted(unknown_event_types)),
    )


def _load_jsonl(value: str) -> LoadedDshSessionLog:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise DshSessionLogError(f"session log line {line_number} is not JSON") from error
        if not isinstance(parsed, dict):
            raise DshSessionLogError(f"session log line {line_number} is not an object")
        redacted = redact_json_value(parsed)
        if not isinstance(redacted, dict):
            raise AssertionError("redacted session event must remain an object")
        events.append(redacted)
    redacted_jsonl = "".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for event in events
    ).encode("utf-8")
    return LoadedDshSessionLog(events=tuple(events), redacted_jsonl=redacted_jsonl)


def _normalise_assistant_message(event: dict[str, Any]) -> tuple[TrajectoryEvent, Usage]:
    data = _required_mapping(event, "data", "assistant/message")
    message = _required_mapping(data, "message", "assistant/message data")
    if message.get("role") != "assistant":
        raise DshSessionLogError("assistant/message has a non-assistant role")
    content = _content_text(message.get("content"), "assistant/message content")
    usage_data = data.get("usage")
    usage = Usage()
    if usage_data is not None:
        if not isinstance(usage_data, dict):
            raise DshSessionLogError("assistant/message usage is not an object")
        usage = Usage(
            input_tokens=_optional_nonnegative_int(usage_data.get("inputTokens"), "inputTokens"),
            output_tokens=_optional_nonnegative_int(usage_data.get("outputTokens"), "outputTokens"),
        )
    return (
        TrajectoryEvent(
            timestamp=_timestamp(event.get("time"), "assistant/message time"),
            type="dsh_assistant_message",
            payload={
                "seq": _required_nonnegative_int(event.get("seq"), "assistant/message seq"),
                "turn": _required_nonnegative_int(data.get("turn"), "assistant/message turn"),
                "step": _required_nonnegative_int(data.get("step"), "assistant/message step"),
                "content": content,
            },
        ),
        usage,
    )


def _normalise_tool_call(event: dict[str, Any]) -> TrajectoryEvent:
    data = _required_mapping(event, "data", "tool/call")
    return TrajectoryEvent(
        timestamp=_timestamp(event.get("time"), "tool/call time"),
        type="dsh_tool_call",
        payload={
            "seq": _required_nonnegative_int(event.get("seq"), "tool/call seq"),
            "turn": _required_nonnegative_int(data.get("turn"), "tool/call turn"),
            "step": _required_nonnegative_int(data.get("step"), "tool/call step"),
            "call_id": _required_string(data, "callId", "tool/call data"),
            "name": _required_string(data, "name", "tool/call data"),
            "arguments": _required_string(data, "arguments", "tool/call data"),
        },
    )


def _normalise_tool_result(event: dict[str, Any]) -> TrajectoryEvent:
    data = _required_mapping(event, "data", "tool/result")
    message = _required_mapping(data, "message", "tool/result data")
    return TrajectoryEvent(
        timestamp=_timestamp(event.get("time"), "tool/result time"),
        type="dsh_tool_result",
        payload={
            "seq": _required_nonnegative_int(event.get("seq"), "tool/result seq"),
            "turn": _required_nonnegative_int(data.get("turn"), "tool/result turn"),
            "step": _required_nonnegative_int(data.get("step"), "tool/result step"),
            "call_id": _tool_result_call_id(message),
            "content": message.get("content"),
            "is_error": _tool_result_is_error(message),
        },
    )


def _normalise_turn_end(event: dict[str, Any], trajectory: list[TrajectoryEvent]) -> bool:
    data = _required_mapping(event, "data", "turn/end")
    reason = _required_mapping(data, "reason", "turn/end data")
    kind = _required_string(reason, "kind", "turn/end reason")
    trajectory.append(
        TrajectoryEvent(
            timestamp=_timestamp(event.get("time"), "turn/end time"),
            type="dsh_turn_end",
            payload={
                "seq": _required_nonnegative_int(event.get("seq"), "turn/end seq"),
                "turn": _required_nonnegative_int(data.get("turn"), "turn/end turn"),
                "reason": kind,
            },
        )
    )
    return kind == "completed"


def _content_text(value: Any, label: str) -> str:
    if not isinstance(value, list):
        raise DshSessionLogError(f"{label} is not a list")
    text_blocks: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            raise DshSessionLogError(f"{label} contains a non-object block")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise DshSessionLogError(f"{label} text block has no string text")
            text_blocks.append(text)
    return "\n".join(text_blocks).strip()


def _tool_result_call_id(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise DshSessionLogError("tool/result message has an invalid content block")
    block = content[0]
    if block.get("type") != "tool-result":
        raise DshSessionLogError("tool/result message has an unsupported content block")
    return _required_string(block, "toolCallId", "tool/result content")


def _tool_result_is_error(message: dict[str, Any]) -> bool:
    content = message["content"]
    assert isinstance(content, list) and isinstance(content[0], dict)
    value = content[0].get("isError")
    if not isinstance(value, bool):
        raise DshSessionLogError("tool/result content has no boolean isError")
    return value


def _required_mapping(value: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise DshSessionLogError(f"{label} has no object {key}")
    return nested


def _required_string(value: dict[str, Any], key: str, label: str) -> str:
    nested = value.get(key)
    if not isinstance(nested, str) or not nested:
        raise DshSessionLogError(f"{label} has no non-empty string {key}")
    return nested


def _required_nonnegative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DshSessionLogError(f"{label} is not a non-negative integer")
    return value


def _optional_nonnegative_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _required_nonnegative_int(value, label)


def _timestamp(value: Any, label: str) -> str:
    milliseconds = _required_nonnegative_int(value, label)
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat().replace("+00:00", "Z")
