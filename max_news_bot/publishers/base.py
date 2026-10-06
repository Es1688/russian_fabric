from __future__ import annotations

from typing import Protocol

from max_news_bot.core.models import NewsItem, PublishedResult


class Publisher(Protocol):
    async def publish(self, items: list[NewsItem]) -> PublishedResult:
        ...
