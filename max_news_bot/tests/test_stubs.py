from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from max_news_bot.core.models import NewsItem
from max_news_bot.parsers.base import SourceConfig
from max_news_bot.parsers.composite import CompositeFetcher


@pytest.mark.asyncio
async def test_composite_fetcher_routes_rss_sources() -> None:
    fetcher = CompositeFetcher()
    source = SourceConfig(
        name="Habr",
        type="rss",
        url="https://habr.com/ru/rss/news/",
        enabled=True,
        category="tech",
    )
    expected = [
        NewsItem(
            title="Test headline",
            url="https://habr.com/ru/news/123/",
            source="Habr",
            published_at=datetime(2026, 5, 25, tzinfo=timezone.utc),
            category="tech",
        )
    ]

    with patch.object(
        fetcher._rss, "fetch", new=AsyncMock(return_value=expected)
    ) as mock_rss_fetch:
        items = await fetcher.fetch(source, max_news=5, timeout=30)

    assert items == expected
    mock_rss_fetch.assert_awaited_once_with(source, 5, 30)


@pytest.mark.asyncio
async def test_composite_fetcher_unsupported_type_returns_empty() -> None:
    # the RSS-only architecture: anything but rss degrades to an empty
    # result with a warning, never to an exception
    fetcher = CompositeFetcher()
    source = SourceConfig(
        name="Legacy html",
        type="html",
        url="https://example.com/news/",
        enabled=True,
    )

    assert await fetcher.fetch(source, max_news=5, timeout=5) == []


@pytest.mark.asyncio
async def test_composite_fetcher_disabled_source_returns_empty() -> None:
    fetcher = CompositeFetcher()
    source = SourceConfig(
        name="Disabled",
        type="rss",
        url="https://example.com/rss/",
        enabled=False,
    )

    with patch.object(fetcher._rss, "fetch", new=AsyncMock()) as mock_rss:
        assert await fetcher.fetch(source, max_news=5, timeout=5) == []

    mock_rss.assert_not_awaited()
