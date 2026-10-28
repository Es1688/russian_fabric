from __future__ import annotations

import aiohttp

from max_news_bot.core.models import NewsItem
from max_news_bot.parsers.base import SourceConfig
from max_news_bot.parsers.rss import RssFetcher
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


class CompositeFetcher:
    """RSS-only fetcher of the digest rubric (the RSS-only architecture

    decision dropped the HTML scraping path together with the legacy
    Habr/vc.ru sources)."""

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        # Shared process-wide session; None keeps the per-call behavior.
        self._rss = RssFetcher(session)

    async def fetch(
        self, source: SourceConfig, max_news: int, timeout: int
    ) -> list[NewsItem]:
        if not source.enabled:
            return []

        if source.type == "rss":
            return await self._rss.fetch(source, max_news, timeout)

        logger.warning(
            "source=%s type=%s unsupported",
            source.name,
            source.type,
        )
        return []
