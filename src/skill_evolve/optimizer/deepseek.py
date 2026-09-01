"""One no-tool DeepSeek Chat Completions client for optimization advice."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from skill_evolve.config.dotenv import read_dotenv_value
from skill_evolve.config.models import OptimizerConfig
from skill_evolve.optimizer.client import OptimizerClientResponse
from skill_evolve.optimizer.contracts import ProviderUsage

_DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"


class MissingOptimizerApiKeyError(RuntimeError):
    """The configured non-secret key reference could not be resolved."""


class OptimizerRequestError(RuntimeError):
    """The provider did not return one valid, bounded completion."""


HttpOpener = Callable[[Request, float], tuple[int, bytes]]


class DeepSeekChatClient:
    """Call one bounded JSON Chat Completion without exposing tools or secrets."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        dotenv_path: Path | None = None,
        opener: HttpOpener | None = None,
    ) -> None:
        self._environ = environ if environ is not None else os.environ
        self._dotenv_path = (
            dotenv_path if dotenv_path is not None else _default_dotenv_path(environ)
        )
        self._opener = opener if opener is not None else _open_request

    def advise(
        self, system_prompt: str, user_prompt: str, config: OptimizerConfig
    ) -> OptimizerClientResponse:
        api_key = self._api_key(config.api_key_env)
        if api_key is None or not api_key.strip():
            raise MissingOptimizerApiKeyError("optimizer API key is unavailable")
        body = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": config.max_output_tokens,
            "stream": False,
        }
        request = Request(
            _DEEPSEEK_CHAT_COMPLETIONS_URL,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            status, response_bytes = self._opener(request, config.timeout_ms / 1000)
        except (HTTPError, URLError, OSError, TimeoutError) as error:
            raise OptimizerRequestError("optimizer request failed") from error
        if status < 200 or status >= 300:
            raise OptimizerRequestError("optimizer request failed")
        try:
            response = json.loads(response_bytes.decode("utf-8"))
            content = _response_content(response)
            usage = _response_usage(response)
        except (TypeError, UnicodeDecodeError, ValueError) as error:
            raise OptimizerRequestError("optimizer response was invalid") from error
        if len(content.encode("utf-8")) > config.max_output_tokens * 16:
            raise OptimizerRequestError("optimizer response exceeded the configured limit")
        return OptimizerClientResponse(content=content, usage=usage)

    def _api_key(self, key_name: str) -> str | None:
        process_value = self._environ.get(key_name)
        if process_value is not None and process_value.strip():
            return process_value
        if self._dotenv_path is None:
            return None
        return read_dotenv_value(self._dotenv_path, key_name)


def _default_dotenv_path(environ: Mapping[str, str] | None) -> Path | None:
    """Use the operator's current workspace only for the real process environment."""

    if environ is not None:
        return None
    return Path.cwd() / ".env"


def _open_request(request: Request, timeout_seconds: float) -> tuple[int, bytes]:
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return response.status, response.read()


def _response_content(response: object) -> str:
    if not isinstance(response, dict):
        raise ValueError("response must be an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("choice must be an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("choice message must be an object")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip() or "\x00" in content:
        raise ValueError("choice content must be non-empty text")
    return content


def _response_usage(response: object) -> ProviderUsage | None:
    if not isinstance(response, dict):
        return None
    usage = response.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    values: dict[str, int | None] = {}
    for source, target in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(source)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError("usage must contain non-negative integers")
        values[target] = value
    return ProviderUsage(**values)
