from __future__ import annotations

from max_news_bot.core.models import NewsItem
from max_news_bot.parsers.base import SourceConfig


class MockFetcher:
    def __init__(
        self,
        items: list[NewsItem] | None = None,
        items_by_source: dict[str, list[NewsItem]] | None = None,
    ) -> None:
        self.items = items or []
        self.items_by_source = items_by_source or {}
        self.fetch_calls: list[tuple] = []

    async def fetch(self, source: SourceConfig, max_news: int, timeout: int) -> list[NewsItem]:
        self.fetch_calls.append((source, max_news, timeout))
        if source.name in self.items_by_source:
            return self.items_by_source[source.name][:max_news]
        return self.items[:max_news]


class MockAlerter:
    """Alerter double recording alerts and recoveries."""

    def __init__(self) -> None:
        self.alerts: list[str] = []
        self.recoveries: list[str] = []

    async def send_alert(self, text: str) -> None:
        self.alerts.append(text)

    async def send_recovery(self, text: str) -> None:
        self.recoveries.append(text)
