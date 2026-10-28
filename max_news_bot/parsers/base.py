from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from max_news_bot.core.models import NewsItem


@dataclass(frozen=True)
class SourceConfig:
    """One feed as the fetcher sees it (moved here from config/loader.py:

    the legacy sources.yaml is gone, and the digest rubric adapts its
    confirmed feeds through this type via ``rubrics/news.as_source_config``)."""

    name: str
    type: str
    url: str
    enabled: bool
    category: str | None = None


class Fetcher(Protocol):
    async def fetch(
        self, source: SourceConfig, max_news: int, timeout: int
    ) -> list[NewsItem]:
        ...
