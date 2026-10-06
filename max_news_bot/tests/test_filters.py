from __future__ import annotations

from max_news_bot.core.models import NewsItem
from max_news_bot.utils.filters import KeywordFilter


def _item(title: str) -> NewsItem:
    return NewsItem(title=title, url=f"https://example.com/{title}", source="Test")


def test_include_keywords_pass_matching() -> None:
    filt = KeywordFilter(include_keywords=["python"], exclude_keywords=[])

    assert filt.passes(_item("New Python release")) is True
    assert filt.passes(_item("Rust update")) is False


def test_include_keywords_case_insensitive() -> None:
    filt = KeywordFilter(include_keywords=["PYTHON"], exclude_keywords=[])

    assert filt.passes(_item("python news")) is True


def test_exclude_keywords_block_matching() -> None:
    filt = KeywordFilter(include_keywords=[], exclude_keywords=["spam"])

    assert filt.passes(_item("Important news")) is True
    assert filt.passes(_item("This is spam content")) is False


def test_exclude_takes_precedence_over_include() -> None:
    filt = KeywordFilter(include_keywords=["python"], exclude_keywords=["spam"])

    assert filt.passes(_item("Python spam roundup")) is False


def test_empty_include_passes_all_except_excluded() -> None:
    filt = KeywordFilter(include_keywords=[], exclude_keywords=["blocked"])

    assert filt.passes(_item("Anything goes")) is True
    assert filt.passes(_item("blocked topic")) is False


def test_empty_keywords_pass_all() -> None:
    filt = KeywordFilter(include_keywords=[], exclude_keywords=[])

    assert filt.passes(_item("Random headline")) is True


def test_whitespace_keywords_ignored() -> None:
    filt = KeywordFilter(include_keywords=["  "], exclude_keywords=["  "])

    assert filt.passes(_item("Some news")) is True
