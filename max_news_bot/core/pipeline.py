from __future__ import annotations

from dataclasses import dataclass

from max_news_bot.config.loader import AppConfig
from max_news_bot.core.models import NewsItem
from max_news_bot.parsers.base import Fetcher
from max_news_bot.publishers.base import Publisher
from max_news_bot.utils.db import Database
from max_news_bot.utils.filters import KeywordFilter
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class PipelineResult:
    collected: int
    filtered: int
    published: int
    skipped: int


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        fetcher: Fetcher,
        publisher: Publisher,
        db: Database,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._publisher = publisher
        self._db = db
        self._filter = KeywordFilter(
            include_keywords=config.filters.include_keywords,
            exclude_keywords=config.filters.exclude_keywords,
        )

    async def run(self) -> PipelineResult:
        collected_items = await self._collect_items()
        filtered_items = [item for item in collected_items if self._filter.passes(item)]
        deduped_items = []
        for item in filtered_items:
            if not await self._db.is_sent(item.url):
                deduped_items.append(item)

        result = await self._publish_items(deduped_items)
        skipped = len(collected_items) - result.published
        await self._db.log_event(
            "run",
            f"collected={len(collected_items)} published={result.published} skipped={skipped}",
        )
        logger.info(
            "collected=%s published=%s skipped=%s",
            len(collected_items),
            result.published,
            skipped,
        )
        return PipelineResult(
            collected=len(collected_items),
            filtered=len(filtered_items),
            published=result.published,
            skipped=skipped,
        )

    async def _collect_items(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        for source in self._config.sources:
            if not source.enabled:
                continue
            source_items = await self._fetcher.fetch(
                source,
                max_news=self._config.settings.max_news_per_source,
                timeout=self._config.settings.request_timeout,
            )
            items.extend(source_items)
        return items

    async def _publish_items(self, items: list[NewsItem]):
        if not items:
            return await self._publisher.publish([])

        result = await self._publisher.publish(items)
        if result.success:
            for item in items:
                await self._db.mark_sent(item)
            await self._db.log_event(
                "publish",
                f"published={result.published}",
            )
        else:
            await self._db.log_event("error", "publisher_failed")
        return result
