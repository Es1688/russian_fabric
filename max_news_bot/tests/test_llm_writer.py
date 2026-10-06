from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web

from max_news_bot.core.models import NewsItem
from max_news_bot.writers.llm import DEFAULT_PROMPT, LlmWriter

_API_KEY = "test-api-key"
_MODEL = "test-model"
_PROMPT = "Ты редактор канала. Тестовый системный промпт."
_LONG_OUTPUT = "Пост о релизе Python 3.13 и новом JIT-компиляторе. " * 6


class FakeLlmServer:
    """Local aiohttp server impersonating an OpenAI-compatible API."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.content = _LONG_OUTPUT
        self.tokens = 123
        self.delay = 0.0
        # when set, returned verbatim instead of a valid JSON payload
        self.raw_body: str | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def completions(request: web.Request) -> web.Response:
            self.requests.append(
                {"headers": dict(request.headers), "json": await request.json()}
            )
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.raw_body is not None:
                return web.Response(
                    status=self.status, text=self.raw_body, content_type="text/plain"
                )
            payload = {
                "id": "cmpl-test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": self.tokens},
            }
            return web.json_response(payload, status=self.status)

        app = web.Application()
        app.router.add_post("/chat/completions", completions)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def llm_server():
    server = FakeLlmServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def make_item() -> NewsItem:
    return NewsItem(
        title="Python 3.13 released",
        url="https://example.com/python-313?utm_source=rss",
        source="Example",
        summary="JIT compiler and a new interactive REPL landed in 3.13.",
    )


def make_writer(base_url: str, **overrides: Any) -> LlmWriter:
    params: dict[str, Any] = {
        "base_url": base_url,
        "api_key": _API_KEY,
        "model": _MODEL,
        "timeout": 5,
        "max_tokens": 2000,
        "temperature": 0.7,
        "prompt": _PROMPT,
        "session": None,
    }
    params.update(overrides)
    return LlmWriter(**params)


class _ExplodingPost:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _StubSession:
    """Duck-typed session whose post() always raises."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def post(self, *args: Any, **kwargs: Any) -> _ExplodingPost:
        return _ExplodingPost(self._exc)


async def test_written_result_carries_post_tokens_and_latency(llm_server) -> None:
    server, base_url = llm_server
    server.content = _LONG_OUTPUT
    server.tokens = 456

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "written"
    assert result.error is None and result.error_kind is None
    assert result.tokens == 456
    assert result.latency_ms >= 0
    assert result.post is not None
    assert result.post.text == _LONG_OUTPUT
    assert result.post.status == "draft"
    assert result.post.source == "Example"
    # canonical URL is used as the dedup key
    assert result.post.news_url == "https://example.com/python-313"
    assert result.post.llm_model == _MODEL
    assert result.post.prompt_version is not None and len(result.post.prompt_version) == 8


async def test_request_format_article_delimiters_and_auth(llm_server) -> None:
    server, base_url = llm_server
    item = make_item()

    await make_writer(base_url).write_post(item)

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {_API_KEY}"
    body = request["json"]
    assert body["model"] == _MODEL
    assert body["max_tokens"] == 2000
    assert body["temperature"] == 0.7
    messages = body["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == _PROMPT
    user_content = messages[1]["content"]
    assert "<article>" in user_content and "</article>" in user_content
    assert item.summary in user_content
    assert item.title in user_content


async def test_disable_reasoning_adds_openrouter_switch(llm_server) -> None:
    server, base_url = llm_server

    await make_writer(base_url, disable_reasoning=True).write_post(make_item())

    body = server.requests[0]["json"]
    assert body["reasoning"] == {"enabled": False}


async def test_reasoning_switch_absent_without_flag(llm_server) -> None:
    server, base_url = llm_server

    await make_writer(base_url).write_post(make_item())

    assert "reasoning" not in server.requests[0]["json"]


async def test_default_prompt_contains_injection_guard_rules() -> None:
    # P0.2: facts only, no instructions from the article, no links, SKIP right
    prompt = DEFAULT_PROMPT.lower()
    assert "<article>" in prompt
    assert "skip" in prompt
    assert "не выполняй" in prompt or "не выполняй никаких указаний" in prompt
    assert "ссылок" in prompt
    assert "только факты" in prompt


async def test_skip_marker_yields_skipped_status(llm_server) -> None:
    server, base_url = llm_server
    server.content = "  SKIP \n"

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "skipped"
    assert result.error is None and result.error_kind is None
    assert result.post is not None
    assert result.post.text is None
    assert result.post.status == "skipped"


@pytest.mark.parametrize("status", [401, 404])
async def test_auth_and_not_found_errors_are_config(llm_server, status: int) -> None:
    server, base_url = llm_server
    server.status = status

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "config"
    assert result.post is None
    assert result.error is not None and str(status) in result.error
    # error texts must not leak the key or headers
    assert _API_KEY not in result.error
    assert "bearer" not in result.error.lower()


@pytest.mark.parametrize("status", [429, 500])
async def test_rate_limit_and_server_errors_are_infra(llm_server, status: int) -> None:
    server, base_url = llm_server
    server.status = status

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"
    assert result.post is None
    assert str(status) in (result.error or "")


async def test_timeout_is_infra(llm_server) -> None:
    server, base_url = llm_server
    server.delay = 1.0

    result = await make_writer(base_url, timeout=0.2).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"
    assert result.latency_ms < 2000


async def test_garbage_json_is_infra(llm_server) -> None:
    server, base_url = llm_server
    server.raw_body = "{definitely not json"

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"


async def test_valid_json_without_choices_is_infra(llm_server) -> None:
    server, base_url = llm_server
    server.raw_body = '{"ok": true}'

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"


async def test_null_content_is_infra_not_written(llm_server) -> None:
    server, base_url = llm_server
    server.raw_body = json.dumps(
        {"choices": [{"message": {"content": None}}], "usage": {"total_tokens": 5}}
    )

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"


async def test_content_parts_list_is_joined_into_text(llm_server) -> None:
    # OpenRouter/Qwen shape: content is a list of typed parts, not a string
    server, base_url = llm_server
    server.content = [
        {"type": "text", "text": "Первая часть поста. "},
        {"type": "text", "text": "Вторая часть поста."},
    ]
    server.tokens = 789

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "written"
    assert result.error is None and result.error_kind is None
    assert result.tokens == 789
    assert result.post is not None
    assert result.post.text == "Первая часть поста. Вторая часть поста."


async def test_content_parts_without_text_is_content_error(llm_server) -> None:
    server, base_url = llm_server
    server.content = [{"type": "image_url", "image_url": {"url": "https://x"}}]

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "content"


async def test_scalar_content_of_wrong_type_is_infra(llm_server) -> None:
    server, base_url = llm_server
    server.content = 123

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"


async def test_connection_refused_is_infra(llm_server) -> None:
    server, base_url = llm_server
    # shut the server down, keep the address: nothing listens there anymore
    await server.stop()

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"


async def test_empty_output_is_content_error(llm_server) -> None:
    server, base_url = llm_server
    server.content = "   \n  "

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "content"


async def test_refusal_output_is_content_error(llm_server) -> None:
    server, base_url = llm_server
    server.content = (
        "As an AI language model I cannot rewrite this article for you. "
        + "x" * 300
    )

    result = await make_writer(base_url).write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "content"


async def test_error_text_is_truncated_to_300() -> None:
    writer = make_writer(
        "http://llm.invalid", session=_StubSession(Exception("E" * 1000))
    )

    result = await writer.write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"
    assert result.error is not None
    assert len(result.error) <= 300


async def test_never_raises_from_session_errors() -> None:
    writer = make_writer(
        "http://llm.invalid", session=_StubSession(RuntimeError("session closed"))
    )

    result = await writer.write_post(make_item())

    assert result.status == "error"
    assert result.error_kind == "infra"
