"""Ephemeral HTTP capability for an agent to inspect a disposable database."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from skill_evolve.sql.postgres import QueryExecution

_MAX_REQUEST_BYTES = 64 * 1024


class ReadOnlyExecutor(Protocol):
    def execute_read_only(self, query: str) -> QueryExecution: ...


class ReadOnlyQueryService:
    """Serve only read-only query execution against an already-isolated fixture."""

    def __init__(self, executor: ReadOnlyExecutor) -> None:
        self._executor = executor
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ReadOnlyQueryService:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - required HTTP handler name
                service._handle_post(self)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.close()

    @property
    def endpoint(self) -> str:
        if self._server is None:
            raise RuntimeError("read-only query service has not started")
        host, port = self._server.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return f"http://{host}:{port}/query"

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path != "/query":
            _write_json(handler, HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        raw_length = handler.headers.get("Content-Length")
        try:
            content_length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            _write_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        if content_length < 1 or content_length > _MAX_REQUEST_BYTES:
            _write_json(
                handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid request size"}
            )
            return
        try:
            body = json.loads(handler.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid JSON request"})
            return
        if not isinstance(body, dict) or not isinstance(body.get("query"), str):
            _write_json(
                handler, HTTPStatus.BAD_REQUEST, {"error": "request requires a string query"}
            )
            return
        execution = self._executor.execute_read_only(body["query"])
        if not execution.ok:
            _write_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": execution.error, "unsafe": execution.unsafe},
            )
            return
        assert execution.result is not None
        _write_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "columns": list(execution.result.columns),
                "rows": [list(row) for row in execution.result.rows],
            },
        )


def _write_json(
    handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]
) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
