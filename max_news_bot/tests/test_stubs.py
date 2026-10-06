from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from max_news_bot.config.loader import SourceConfig
from max_news_bot.core.models import NewsItem
from max_news_bot.parsers.composite import CompositeFetcher
from max_news_bot.parsers.habr import HabrFetcher
from max_news_bot.parsers.vc_ru import VcRuFetcher


@pytest.mark.asyncio
async def test_habr_stub_returns_empty_list() -> None:
    fetcher = HabrFetcher()
    source = SourceConfig(
        name="Habr",
        type="html",
        url="https://habr.com/ru/news/",
        enabled=True,
        category="tech",
    )

    items = await fetcher.fetch(source, max_news=5, timeout=5)

    assert items == []


@pytest.mark.asyncio
async def test_vc_ru_stub_returns_empty_list() -> None:
    fetcher = VcRuFetcher()
    source = SourceConfig(
        name="vc.ru",
        type="html",
        url="https://vc.ru/",
        enabled=True,
        category="business",
    )

    items = await fetcher.fetch(source, max_news=5, timeout=5)

    assert items == []


@pytest.mark.asyncio
async def test_composite_fetcher_routes_html_sources() -> None:
    fetcher = CompositeFetcher()
    habr = SourceConfig(
        name="Habr",
        type="html",
        url="https://habr.com/ru/news/",
        enabled=True,
    )
    vc_ru = SourceConfig(
        name="vc.ru",
        type="html",
        url="https://vc.ru/",
        enabled=True,
    )

    assert await fetcher.fetch(habr, max_news=5, timeout=5) == []
    assert await fetcher.fetch(vc_ru, max_news=5, timeout=5) == []


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
