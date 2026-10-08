from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from max_news_bot.core.models import Post
from max_news_bot.max_api.client import MaxBadResponseError
from max_news_bot.max_api.errors import MaxApiError, MaxRateLimitError
from max_news_bot.publishers.post import MaxPostPublisher

_CHANNEL = 50000123  # MAX chat_id is int64
_ADMINS = [111, 222, 333]
_NEWS_URL = "https://example.com/news"

_POST_TEXT = (
    "<b>Релиз Python 3.13</b> принесёт экспериментальный JIT-компилятор и "
    "новый интерактивный REPL. Разработчики отмечают ускорение старта "
    "интерпретатора и улучшенные сообщения об ошибках. Совместимость с "
    "популярными фреймворками сохраняется."
)


def make_post(text: str | None = _POST_TEXT) -> Post:
    return Post(
        id=7,
        news_url=_NEWS_URL,
        source="Example",
        title="Python 3.13",
        text=text,
        status="draft",
    )


@pytest.fixture(autouse=True)
def no_send_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MaxPostPublisher, "SEND_PAUSE_SECONDS", 0.0)


def make_client(side_effect=None, mid: str = "mid.42") -> AsyncMock:
    client = AsyncMock()
    if side_effect is not None:
        client.send_message.side_effect = side_effect
    else:
        client.send_message.return_value = mid
    return client


def make_publisher(client: AsyncMock) -> MaxPostPublisher:
    return MaxPostPublisher(
        client=client, chat_id=_CHANNEL, admin_ids=list(_ADMINS)
    )


def call_args(client: AsyncMock, index: int = 0) -> tuple:
    return client.send_message.await_args_list[index].args


def call_kwargs(client: AsyncMock, index: int = 0) -> dict:
    return client.send_message.await_args_list[index].kwargs


def rate_limit() -> MaxRateLimitError:
    return MaxRateLimitError(429, "too.many.requests", "rate exceeded")


async def test_channel_send_appends_source_link_and_uses_html() -> None:
    client = make_client(mid="mid.42")
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is True
    assert result.message_id == "mid.42"  # MAX mid string returned as-is
    assert result.error is None
    client.send_message.assert_awaited_once()
    assert call_args(client)[0] == _CHANNEL
    kwargs = call_kwargs(client)
    assert kwargs["fmt"] == "html"
    assert kwargs["disable_link_preview"] is False
    # sanitized post text + the code-added source link
    text = call_args(client)[1]
    assert text.startswith("<b>Релиз Python 3.13</b>")
    assert text.endswith(f"\nИсточник: {_NEWS_URL}")


async def test_text_is_sanitized_before_sending() -> None:
    client = make_client()
    publisher = make_publisher(client)
    post = make_post("<b>ok</b> & <script>bad()</script> 5 < 6")

    await publisher.publish_post(post)

    text = call_args(client)[1]
    assert "<b>ok</b>" in text
    assert "<script>" not in text
    assert "&amp;" in text and "&lt;" in text


async def test_rate_limit_is_retried_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # real pause value so the backoff schedule is observable; sleep is faked
    monkeypatch.setattr(MaxPostPublisher, "SEND_PAUSE_SECONDS", 2.5)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = make_client(side_effect=[rate_limit(), "mid.7"])
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is True
    assert result.message_id == "mid.7"
    assert client.send_message.await_count == 2
    # MAX has no retry_after: fixed backoff of attempt * SEND_PAUSE_SECONDS
    assert sleeps == [2.5]


async def test_rate_limit_backoff_grows_with_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MaxPostPublisher, "SEND_PAUSE_SECONDS", 2.5)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = make_client(side_effect=[rate_limit(), rate_limit(), rate_limit()])
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is False
    assert sleeps == [2.5, 5.0]


async def test_rate_limit_exhausts_after_three_attempts() -> None:
    client = make_client(side_effect=[rate_limit(), rate_limit(), rate_limit()])
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is False
    assert result.message_id is None
    assert result.error is not None
    assert result.error_kind == "infra"  # rate limiting must not burn attempts
    assert client.send_message.await_count == 3


async def test_parse_error_falls_back_to_plain_text() -> None:
    parse_error = MaxApiError(
        400, "parse.failed", "can't parse entities: unsupported tag"
    )
    client = make_client(side_effect=[parse_error, "mid.9"])
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is True
    assert result.message_id == "mid.9"
    assert client.send_message.await_count == 2
    # MAX has no "no format" mode: the fallback is still fmt=html, but with
    # tags stripped and everything escaped — guaranteed-valid html
    assert call_kwargs(client, index=1)["fmt"] == "html"
    plain_text = call_args(client, index=1)[1]
    assert "<b>" not in plain_text
    assert "&lt;b&gt;" not in plain_text
    assert plain_text.endswith(f"\nИсточник: {_NEWS_URL}")
    assert "Релиз Python 3.13" in plain_text


async def test_other_bad_request_errors_are_reported_without_fallback() -> None:
    error = MaxApiError(400, "chat.not.found", "chat not found")
    client = make_client(side_effect=error)
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is False
    assert result.message_id is None
    assert result.error is not None
    assert "chat not found" in result.error
    # unlike Telegram, MAX classify_error maps every 400 to "content":
    # a 400 means the request body itself is malformed
    assert result.error_kind == "content"
    assert client.send_message.await_count == 1


async def test_too_long_message_is_content_error_without_fallback() -> None:
    error = MaxApiError(400, "validation.failed", "text is too long")
    client = make_client(side_effect=error)
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is False
    assert result.error_kind == "content"
    # the plain variant would be just as long — no fallback attempt
    client.send_message.assert_awaited_once()


async def test_bad_response_error_is_infra() -> None:
    error = MaxBadResponseError("/messages response has no message id")
    client = make_client(side_effect=error)
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is False
    assert result.error_kind == "infra"
    client.send_message.assert_awaited_once()


async def test_admins_mode_sends_preview_to_every_admin() -> None:
    client = make_client(mid="mid.55")
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post(), to_admins=True)

    assert result.success is True
    # dry-run previews carry no channel message id
    assert result.message_id is None
    assert client.send_message.await_count == len(_ADMINS)
    sent_to = [call_args(client, i)[0] for i in range(len(_ADMINS))]
    assert sent_to == _ADMINS
    for i in range(len(_ADMINS)):
        text = call_args(client, i)[1]
        assert text.startswith("[DRY-RUN] ")
        assert text.endswith(f"\nИсточник: {_NEWS_URL}")


async def test_one_admin_failure_does_not_block_others() -> None:
    client = make_client(side_effect=[RuntimeError("dm closed"), "mid.1", "mid.2"])
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post(), to_admins=True)

    assert result.success is True
    assert result.message_id is None
    assert client.send_message.await_count == 3


async def test_admins_mode_all_failures_report_error() -> None:
    client = make_client(side_effect=RuntimeError("no dm"))
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post(), to_admins=True)

    assert result.success is False
    assert result.message_id is None
    assert "no admin preview delivered" in (result.error or "")
    assert result.error_kind == "infra"


async def test_generic_exception_is_reported_not_raised() -> None:
    client = make_client(side_effect=RuntimeError("network down"))
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.success is False
    assert result.message_id is None
    assert "network down" in (result.error or "")
    assert result.error_kind == "infra"


async def test_empty_post_text_is_rejected() -> None:
    client = make_client()
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post(text="   "))

    assert result.success is False
    assert "empty" in (result.error or "")
    assert result.error_kind == "content"
    client.send_message.assert_not_awaited()


async def test_source_link_ampersand_is_escaped() -> None:
    client = make_client(mid="mid.5")
    publisher = make_publisher(client)
    post = replace(make_post(), news_url="https://example.com/news?a=1&b=2")

    result = await publisher.publish_post(post)

    assert result.success is True
    text = call_args(client)[1]
    # every MAX send is fmt="html", so the URL is always escaped
    assert text.endswith("\nИсточник: https://example.com/news?a=1&amp;b=2")


async def test_error_text_is_truncated() -> None:
    client = make_client(side_effect=RuntimeError("x" * 1000))
    publisher = make_publisher(client)

    result = await publisher.publish_post(make_post())

    assert result.error is not None
    assert len(result.error) <= 300


async def test_pause_applies_between_consecutive_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MaxPostPublisher, "SEND_PAUSE_SECONDS", 0.1)
    client = make_client(mid="mid.1")
    publisher = make_publisher(client)

    started = time.monotonic()
    await publisher.publish_post(make_post())
    first_send_elapsed = time.monotonic() - started

    started = time.monotonic()
    await publisher.publish_post(make_post())
    second_send_elapsed = time.monotonic() - started

    # first send is immediate, the following one waits out the pause
    assert first_send_elapsed < 0.09
    assert second_send_elapsed >= 0.09
