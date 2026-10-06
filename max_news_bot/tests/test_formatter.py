from __future__ import annotations

from max_news_bot.core.models import NewsItem
from max_news_bot.utils.db import Database
from max_news_bot.utils.formatter import MAX_MESSAGE_LEN, format_items


def _item(title: str, url: str = "https://example.com/news", source: str = "Test") -> NewsItem:
    return NewsItem(title=title, url=url, source=source, category="tech")


def test_format_items_includes_url_and_source() -> None:
    messages = format_items([_item("Hello World", url="https://example.com/hello")])

    assert len(messages) == 1
    assert "Hello World" in messages[0]
    assert "https://example.com/hello" in messages[0]
    assert "Test" in messages[0]
    assert "tech" in messages[0]


def test_format_items_html_escapes_special_chars() -> None:
    messages = format_items([_item("A & B <script>", url="https://example.com?q=1&b=2")])

    assert "&amp;" in messages[0]
    assert "&lt;script&gt;" in messages[0]
    assert "https://example.com?q=1&amp;b=2" in messages[0]


def test_format_items_respects_telegram_limit() -> None:
    long_title = "X" * 5000
    messages = format_items([_item(long_title)])

    assert len(messages) >= 1
    for message in messages:
        assert len(message) <= MAX_MESSAGE_LEN


def test_format_items_splits_long_batch() -> None:
    items = [
        _item(f"News item number {index} with extra context text")
        for index in range(120)
    ]
    messages = format_items(items)

    assert len(messages) > 1
    for message in messages:
        assert len(message) <= MAX_MESSAGE_LEN


def test_format_empty_list_returns_empty() -> None:
    assert format_items([]) == []


def test_format_multiple_items_single_message_when_short() -> None:
    items = [_item("One"), _item("Two")]
    messages = format_items(items)

    assert len(messages) == 1
    assert "One" in messages[0]
    assert "Two" in messages[0]
