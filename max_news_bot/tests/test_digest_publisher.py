from __future__ import annotations

from unittest.mock import AsyncMock

from max_news_bot.core.models import NewsItem
from max_news_bot.max_api.errors import MaxApiError, MaxRateLimitError
from max_news_bot.publishers.max import MaxDigestPublisher
from max_news_bot.utils.formatter import format_items

_CHAT_ID = 50000123


def _item(index: int) -> NewsItem:
    return NewsItem(
        title=f"News item number {index} with extra context text",
        url=f"https://example.com/news/{index}",
        source="Test",
        category="tech",
    )


def _items(count: int) -> list[NewsItem]:
    return [_item(index) for index in range(count)]


def make_client(side_effect=None) -> AsyncMock:
    client = AsyncMock()
    if side_effect is not None:
        client.send_message.side_effect = side_effect
    else:
        client.send_message.return_value = 12345
    return client


def make_publisher(client: AsyncMock) -> MaxDigestPublisher:
    return MaxDigestPublisher(client=client, chat_id=_CHAT_ID)


async def test_empty_items_short_circuits_without_sending() -> None:
    client = make_client()
    publisher = make_publisher(client)

    result = await publisher.publish([])

    assert result.success is True
    assert result.published == 0
    assert result.failed == 0
    client.send_message.assert_not_awaited()


async def test_single_batch_is_sent_as_html_without_preview() -> None:
    items = _items(2)
    client = make_client()
    publisher = make_publisher(client)

    result = await publisher.publish(items)

    assert result.success is True
    assert result.published == len(items)
    assert result.failed == 0
    client.send_message.assert_awaited_once()
    args, kwargs = client.send_message.await_args
    assert args[0] == _CHAT_ID
    assert args[1] == format_items(items)[0]
    assert kwargs["fmt"] == "html"
    assert kwargs["disable_link_preview"] is True


async def test_batches_are_sent_sequentially_in_order() -> None:
    items = _items(120)  # format_items splits this into several batches
    client = make_client()
    publisher = make_publisher(client)

    result = await publisher.publish(items)

    expected = format_items(items)
    assert len(expected) > 1
    assert result.success is True
    assert result.published == len(items)
    assert client.send_message.await_count == len(expected)
    sent = [
        (call.args[0], call.args[1])
        for call in client.send_message.await_args_list
    ]
    assert sent == [(_CHAT_ID, message) for message in expected]


async def test_send_failure_fails_all_items() -> None:
    items = _items(3)
    client = make_client(side_effect=RuntimeError("network down"))
    publisher = make_publisher(client)

    result = await publisher.publish(items)

    assert result.success is False
    assert result.published == 0
    assert result.failed == len(items)


async def test_rate_limit_error_fails_whole_digest() -> None:
    # the digest publisher has no per-message retry (unlike MaxPostPublisher):
    # a raised MaxRateLimitError fails the run the same way as any error
    items = _items(2)
    client = make_client(
        side_effect=MaxRateLimitError(429, "too.many.requests", "rate exceeded")
    )
    publisher = make_publisher(client)

    result = await publisher.publish(items)

    assert result.success is False
    assert result.published == 0
    assert result.failed == len(items)


async def test_failure_on_later_batch_fails_all_items() -> None:
    items = _items(120)
    client = make_client(side_effect=[12345, MaxApiError(500, "boom", "oops")])
    publisher = make_publisher(client)

    result = await publisher.publish(items)

    # all-or-nothing counting: no partial success is reported
    assert result.success is False
    assert result.published == 0
    assert result.failed == len(items)
    assert client.send_message.await_count == 2
