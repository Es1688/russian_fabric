from __future__ import annotations

from typing import Protocol

from max_news_bot.config.loader import SourceConfig
from max_news_bot.core.models import NewsItem


class Fetcher(Protocol):
    async def fetch(
        self, source: SourceConfig, max_news: int, timeout: int
    ) -> list[NewsItem]:
        ...
