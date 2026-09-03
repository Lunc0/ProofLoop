"""DeepSeek client tests with a fake HTTP transport only."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

import pytest

from skill_evolve.config.models import OptimizerConfig
from skill_evolve.optimizer.deepseek import (
    DeepSeekChatClient,
    MissingOptimizerApiKeyError,
    OptimizerRequestError,
)


def test_missing_api_key_does_not_open_network_connection() -> None:
    opened = False

    def opener(request: Request, timeout: float) -> tuple[int, bytes]:
        nonlocal opened
        del request, timeout
        opened = True
        return 200, b"{}"

    client = DeepSeekChatClient(environ={}, opener=opener)

    with pytest.raises(MissingOptimizerApiKeyError):
        client.advise("system", "user", OptimizerConfig(enabled=True))

    assert not opened


def test_request_is_one_json_completion_without_tools() -> None:
    requests: list[Request] = []

    def opener(request: Request, timeout: float) -> tuple[int, bytes]:
        del timeout
        requests.append(request)
        return (
            200,
            json.dumps(
                {
                    "choices": [{"message": {"content": "{\"summary\": \"ok\"}"}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
                }
            ).encode("utf-8"),
        )

    client = DeepSeekChatClient(environ={"DEEPSEEK_API_KEY": "test-value"}, opener=opener)
    response = client.advise("system", "user", OptimizerConfig(enabled=True))

    assert response.content == '{"summary": "ok"}'
    assert response.usage is not None
    assert response.usage.total_tokens == 7
    assert len(requests) == 1
    body = json.loads(requests[0].data.decode("utf-8"))
    assert requests[0].method == "POST"
    assert requests[0].full_url == "https://api.deepseek.com/chat/completions"
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in body
    assert "tools" not in body


def test_explicit_output_limit_is_sent_and_enforced() -> None:
    requests: list[Request] = []
    oversized_content = "x" * 1_025

    def opener(request: Request, timeout: float) -> tuple[int, bytes]:
        del timeout
        requests.append(request)
        return 200, json.dumps(
            {"choices": [{"message": {"content": oversized_content}}]}
        ).encode("utf-8")

    client = DeepSeekChatClient(environ={"DEEPSEEK_API_KEY": "test-value"}, opener=opener)

    with pytest.raises(OptimizerRequestError, match="configured limit"):
        client.advise("system", "user", OptimizerConfig(enabled=True, max_output_tokens=64))

    body = json.loads(requests[0].data.decode("utf-8"))
    assert body["max_tokens"] == 64


def test_local_dotenv_api_key_is_used_without_persisting_it(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("DEEPSEEK_API_KEY=dotenv-test-key\n", encoding="utf-8")
    requests: list[Request] = []

    def opener(request: Request, timeout: float) -> tuple[int, bytes]:
        del timeout
        requests.append(request)
        return 200, b'{"choices":[{"message":{"content":"{\\"summary\\":\\"ok\\"}"}}]}'

    client = DeepSeekChatClient(environ={}, dotenv_path=dotenv_path, opener=opener)
    client.advise("system", "user", OptimizerConfig(enabled=True))

    assert requests[0].get_header("Authorization") == "Bearer dotenv-test-key"
    assert b"dotenv-test-key" not in requests[0].data


def test_process_api_key_takes_precedence_over_local_dotenv(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("DEEPSEEK_API_KEY=dotenv-test-key\n", encoding="utf-8")
    requests: list[Request] = []

    def opener(request: Request, timeout: float) -> tuple[int, bytes]:
        del timeout
        requests.append(request)
        return 200, b'{"choices":[{"message":{"content":"{\\"summary\\":\\"ok\\"}"}}]}'

    client = DeepSeekChatClient(
        environ={"DEEPSEEK_API_KEY": "process-test-key"},
        dotenv_path=dotenv_path,
        opener=opener,
    )
    client.advise("system", "user", OptimizerConfig(enabled=True))

    assert requests[0].get_header("Authorization") == "Bearer process-test-key"


def test_non_success_response_is_normalized() -> None:
    client = DeepSeekChatClient(
        environ={"DEEPSEEK_API_KEY": "test-value"},
        opener=lambda request, timeout: (429, b"rate limited"),
    )

    with pytest.raises(OptimizerRequestError, match="request failed"):
        client.advise("system", "user", OptimizerConfig(enabled=True))
