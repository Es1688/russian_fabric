from __future__ import annotations

from urllib.parse import urlparse

import aiohttp

from max_news_bot.config.loader import SourceConfig
from max_news_bot.core.models import NewsItem
from max_news_bot.parsers.habr import HabrFetcher
from max_news_bot.parsers.rss import RssFetcher
from max_news_bot.parsers.vc_ru import VcRuFetcher
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


class CompositeFetcher:
    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        # Shared process-wide session (factory + digest in one process);
        # None keeps the old per-call behavior.
        self._rss = RssFetcher(session)
        self._habr = HabrFetcher()
        self._vc_ru = VcRuFetcher()

    async def fetch(
        self, source: SourceConfig, max_news: int, timeout: int
    ) -> list[NewsItem]:
        if not source.enabled:
            return []

        if source.type == "rss":
            return await self._rss.fetch(source, max_news, timeout)

        if source.type == "html":
            host = urlparse(source.url).netloc.lower()
            if "habr.com" in host:
                return await self._habr.fetch(source, max_news, timeout)
            if "vc.ru" in host:
                return await self._vc_ru.fetch(source, max_news, timeout)

        logger.warning(
            "source=%s type=%s unsupported",
            source.name,
            source.type,
        )
        return []
