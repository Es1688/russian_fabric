from __future__ import annotations

from max_news_bot.config.loader import SourceConfig
from max_news_bot.core.models import NewsItem
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


class VcRuFetcher:
    async def fetch(
        self, source: SourceConfig, max_news: int, timeout: int
    ) -> list[NewsItem]:
        if not source.enabled:
            return []

        logger.info(
            "source=%s html parser stub — not implemented yet",
            source.name,
        )
        return []
