"""Tests for the non-secret agent query capability."""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from skill_evolve.sql.postgres import QueryExecution, QueryResult
from skill_evolve.sql.readonly_service import ReadOnlyQueryService


def test_readonly_query_service_returns_executor_result() -> None:
    executor = StubExecutor()
    with ReadOnlyQueryService(executor) as service:
        payload = _post(service.endpoint, "SELECT id FROM customers")

    assert executor.queries == ["SELECT id FROM customers"]
    assert payload == {"ok": True, "columns": ["id"], "rows": [[1]]}


def test_readonly_query_service_rejects_executor_error() -> None:
    executor = StubExecutor(
        QueryExecution(error="read-only policy rejected keyword: DELETE", unsafe=True)
    )
    with ReadOnlyQueryService(executor) as service, pytest.raises(HTTPError) as raised:
        _post(service.endpoint, "DELETE FROM customers")

    assert raised.value.code == 400
    assert executor.queries == ["DELETE FROM customers"]


class StubExecutor:
    def __init__(self, response: QueryExecution | None = None) -> None:
        self.queries: list[str] = []
        self._response = response or QueryExecution(
            result=QueryResult(columns=("id",), rows=((1,),))
        )

    def execute_read_only(self, query: str) -> QueryExecution:
        self.queries.append(query)
        return self._response


def _post(endpoint: str, query: str) -> dict[str, object]:
    request = Request(
        endpoint,
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:  # noqa: S310 - local service endpoint from test
        value = json.loads(response.read())
    assert isinstance(value, dict)
    return value
