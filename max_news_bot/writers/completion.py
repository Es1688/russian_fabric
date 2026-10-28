"""Generic LLM completion call for rubric pipelines (plan section 8).

Rubric pipelines need an OpenAI-compatible transport with caller-supplied
prompts: the generative pipeline for its posts and the safety checker for
its yes/no question (plan section 7). Since the legacy news writer went
away with the factory contour, this module owns the whole request path:
the transport helpers, the error taxonomy (config vs infra, P0.3) and the
``SKIP_MARKER`` a pipeline may answer instead of a post.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp


_MAX_ERROR_LEN = 300

# Broken key, balance or model: retrying the same request is pointless.
_CONFIG_STATUSES = frozenset({401, 402, 403, 404})

# The word a generation may answer instead of a post: the pipeline treats
# it as a deliberate skip, not as post text.
SKIP_MARKER = "SKIP"


def _truncate(text: str) -> str:
    return text[:_MAX_ERROR_LEN]


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"status={status}")
        self.status = status


class _BadResponseError(Exception):
    pass


def _normalize_content(raw: Any) -> str | None:
    """Accept both shapes allowed by the OpenAI chat spec: a plain string
    or a list of content parts. Some OpenAI-compatible providers return
    text parts; non-text parts are dropped. Returns None for anything else
    (null included) so the caller can reject it."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(
            part.get("text") or ""
            for part in raw
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return None


@dataclass(frozen=True)
class CompletionResult:
    """One successful completion: text plus usage for the daily summary."""

    content: str
    tokens: int = 0
    model: str = ""


class LlmCompletionError(Exception):
    """Completion failed; ``kind`` follows the factory taxonomy (P0.3).

    "config" — wrong key, balance or model: retrying is pointless and the
    caller must alert immediately; "infra" — timeout, network, malformed
    response: retryable by the caller.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class LlmCompletion(Protocol):
    async def complete(self, system: str, user: str) -> CompletionResult: ...


class LlmCompletionClient:
    """Caller-supplied prompts over the same /chat/completions endpoint the
    news writer uses (Yandex AI Studio, OpenAI-compatible)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int,
        max_tokens: int,
        temperature: float,
        disable_reasoning: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._disable_reasoning = disable_reasoning
        # Shared process-wide session wins; otherwise one per call.
        self._session = session

    async def complete(self, system: str, user: str) -> CompletionResult:
        """One completion; raises LlmCompletionError, never other errors."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if self._disable_reasoning:
            # thinking models on OpenRouter otherwise burn the whole
            # max_tokens budget on reasoning and return content=null
            payload["reasoning"] = {"enabled": False}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/chat/completions"
        try:
            if self._session is not None:
                content, tokens = await self._post(
                    self._session, url, payload, headers
                )
            else:
                async with aiohttp.ClientSession() as session:
                    content, tokens = await self._post(
                        session, url, payload, headers
                    )
        except LlmCompletionError:
            raise
        except _HttpError as exc:
            raise self._classify_http(exc) from exc
        except _BadResponseError as exc:
            raise LlmCompletionError(
                "infra", _truncate(f"llm invalid response: {exc}")
            ) from exc
        except Exception as exc:  # timeouts and any network failure
            raise LlmCompletionError(
                "infra",
                _truncate(f"llm request failed: {type(exc).__name__} {exc}"),
            ) from exc
        return CompletionResult(content=content, tokens=tokens, model=self._model)

    async def _post(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[str, int]:
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as response:
            body = await response.text()
            if response.status != 200:
                raise _HttpError(response.status)
            try:
                data = json.loads(body)
                raw_content = data["choices"][0]["message"]["content"]
                tokens = int(data.get("usage", {}).get("total_tokens", 0) or 0)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                raise _BadResponseError(str(exc)) from exc
            content = _normalize_content(raw_content)
            if content is None:
                raise _BadResponseError(
                    f"message.content is not a string: {type(raw_content).__name__}"
                )
            return content, tokens

    @staticmethod
    def _classify_http(exc: _HttpError) -> LlmCompletionError:
        if exc.status in _CONFIG_STATUSES:
            return LlmCompletionError("config", f"llm http {exc.status}")
        return LlmCompletionError("infra", f"llm http {exc.status}")
