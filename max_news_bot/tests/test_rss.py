from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from max_news_bot.parsers.base import SourceConfig
from max_news_bot.parsers.rss import RssFetcher, _entry_to_item


@pytest.fixture
def rss_source() -> SourceConfig:
    return SourceConfig(
        name="Broken Feed",
        type="rss",
        url="https://example.com/feed.xml",
        enabled=True,
        category="test",
    )


@pytest.mark.asyncio
async def test_unreachable_rss_returns_empty_list(rss_source: SourceConfig) -> None:
    fetcher = RssFetcher()

    mock_response = AsyncMock()
    mock_response.__aenter__.side_effect = TimeoutError("connection timed out")
    mock_response.__aexit__.return_value = None

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_response
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None

    with patch("max_news_bot.parsers.rss.aiohttp.ClientSession", return_value=mock_session):
        items = await fetcher.fetch(rss_source, max_news=5, timeout=5)

    assert items == []


@pytest.mark.asyncio
async def test_broken_xml_returns_empty_or_partial(rss_source: SourceConfig) -> None:
    fetcher = RssFetcher()
    broken_xml = b"<rss><channel><item><title>Bad"

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.read = AsyncMock(return_value=broken_xml)

    mock_get = AsyncMock()
    mock_get.__aenter__.return_value = mock_response
    mock_get.__aexit__.return_value = None

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_get
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None

    with patch("max_news_bot.parsers.rss.aiohttp.ClientSession", return_value=mock_session):
        items = await fetcher.fetch(rss_source, max_news=5, timeout=5)

    assert isinstance(items, list)


@pytest.mark.asyncio
async def test_non_rss_source_type_returns_empty() -> None:
    fetcher = RssFetcher()
    source = SourceConfig(
        name="HTML source",
        type="html",
        url="https://example.com",
        enabled=True,
    )

    items = await fetcher.fetch(source, max_news=5, timeout=5)

    assert items == []


@pytest.mark.asyncio
async def test_disabled_source_returns_empty() -> None:
    fetcher = RssFetcher()
    source = SourceConfig(
        name="Disabled",
        type="rss",
        url="https://example.com/feed.xml",
        enabled=False,
    )

    items = await fetcher.fetch(source, max_news=5, timeout=5)

    assert items == []


@pytest.mark.network
@pytest.mark.asyncio
async def test_habr_rss_feed_returns_items() -> None:
    fetcher = RssFetcher()
    source = SourceConfig(
        name="Habr",
        type="rss",
        url="https://habr.com/ru/rss/news/",
        enabled=True,
        category="tech",
    )

    items = await fetcher.fetch(source, max_news=3, timeout=30)

    assert len(items) >= 1
    assert all(item.source == "Habr" for item in items)
    assert all(item.title for item in items)
    assert all(item.url.startswith("https://") for item in items)


def _summary_source() -> SourceConfig:
    return SourceConfig(
        name="Summary Feed",
        type="rss",
        url="https://example.com/feed.xml",
        enabled=True,
        category="test",
    )


def test_summary_plain_text() -> None:
    entry = {"title": "News", "link": "https://example.com/1", "summary": "Просто текст"}
    item = _entry_to_item(entry, _summary_source())
    assert item.summary == "Просто текст"


def test_summary_strips_html_and_unescapes_entities() -> None:
    entry = {
        "title": "News",
        "link": "https://example.com/2",
        "summary": "<p>Текст с <b>тегами</b> &amp; сущностями</p>",
    }
    item = _entry_to_item(entry, _summary_source())
    assert item.summary == "Текст с тегами & сущностями"


def test_summary_truncated_to_limit() -> None:
    entry = {
        "title": "News",
        "link": "https://example.com/3",
        "summary": "x" * 2500,
    }
    item = _entry_to_item(entry, _summary_source())
    assert item.summary is not None
    assert len(item.summary) == 1000


def test_summary_falls_back_to_description() -> None:
    entry = {
        "title": "News",
        "link": "https://example.com/4",
        "description": "Из поля description",
    }
    item = _entry_to_item(entry, _summary_source())
    assert item.summary == "Из поля description"


def test_summary_missing_returns_none() -> None:
    entry = {"title": "News", "link": "https://example.com/5"}
    item = _entry_to_item(entry, _summary_source())
    assert item.summary is None


@pytest.mark.asyncio
async def test_shared_session_is_used_not_created(rss_source: SourceConfig) -> None:
    payload = b"<rss><channel></channel></rss>"

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.read = AsyncMock(return_value=payload)

    mock_get = AsyncMock()
    mock_get.__aenter__.return_value = mock_response
    mock_get.__aexit__.return_value = None

    shared_session = MagicMock()
    shared_session.get = MagicMock(return_value=mock_get)

    fetcher = RssFetcher(session=shared_session)
    with patch("max_news_bot.parsers.rss.aiohttp.ClientSession") as factory:
        items = await fetcher.fetch(rss_source, max_news=5, timeout=5)

    assert items == []
    factory.assert_not_called()
    shared_session.get.assert_called_once()
