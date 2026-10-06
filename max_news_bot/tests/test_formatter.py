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


def test_format_items_respects_max_limit() -> None:
    long_title = "X" * 5000
    messages = format_items([_item(long_title)])

    assert len(messages) >= 1
    for message in messages:
        assert len(message) <= MAX_MESSAGE_LEN


def test_format_items_truncates_block_to_exactly_max_limit() -> None:
    # the MAX hard limit is 4000 (not Telegram's 4096): an oversized block
    # is truncated to exactly MAX_MESSAGE_LEN, "..." included
    assert MAX_MESSAGE_LEN == 4000

    messages = format_items([_item("X" * 5000)])

    assert len(messages) == 1
    assert len(messages[0]) == MAX_MESSAGE_LEN
    assert messages[0].endswith("...")


def test_format_items_batch_fitting_4096_splits_at_max_limit() -> None:
    # two 2020-char blocks total 4042 chars: fits the old Telegram limit
    # (4096) but exceeds the MAX limit (4000) — must split into two messages
    # (block = title + 52 fixed chars, so title length is 2020 - 52)
    items = [_item("X" * 1968), _item("Y" * 1968)]
    messages = format_items(items)

    assert len(messages) == 2
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
