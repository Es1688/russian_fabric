"""Shadow news collection for the good-news rubric (plan sections 5, 14).

Stage 0 of the plan runs this collector for 2-3 days before the test week:
it fetches the operator-confirmed feeds (``config/news_sources.yaml``),
records every unseen item in a *shadow* sqlite database and logs one row per
pass. Sampling and URL deduplication follow the legacy digest pipeline
pattern (``core/pipeline.py``: fetch every enabled source, then
is_seen/mark_seen per item) — but against ``seen_news`` in the shadow file,
never against the production dedup tables (``sent_news``, ``posts``).

The stored items are the review material for the digest stage: the filter
chain (stop-list, topical exclusions, LLM classifier, freshness) is built
on top of this store later and must not lose fresh news to premature
production dedup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from max_news_bot.bot.slot_scheduler import SourceStat
from max_news_bot.config.news_sources import NewsSource
from max_news_bot.core.models import NewsItem
from max_news_bot.parsers.base import Fetcher, SourceConfig
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.repositories import ShadowNewsRepository


logger = get_logger(__name__)

# Hourly passes: the digest freshness window is 24-48 hours (plan section 5),
# so an hour misses nothing that matters while keeping the feeds' load small.
SHADOW_COLLECT_INTERVAL_SECONDS: float = 3600.0
# Verified feeds carry 10-100 entries; 20 per feed leaves enough candidates
# for a 3-4 item digest after the filter chain drops most of them.
DEFAULT_MAX_NEWS_PER_SOURCE = 20
DEFAULT_REQUEST_TIMEOUT = 30

SHADOW_DB_SUFFIX = "_news_shadow.db"


def shadow_db_path(main_db_path: Path) -> Path:
    """Default shadow database file, next to the main one (never the same)."""
    return main_db_path.with_name(f"{main_db_path.stem}{SHADOW_DB_SUFFIX}")


def as_source_config(source: NewsSource) -> SourceConfig:
    """Adapt a confirmed feed for the RSS fetcher (section -> category)."""
    return SourceConfig(
        name=source.name,
        type="rss",
        url=source.url,
        enabled=source.enabled,
        category=source.section,
    )


@dataclass(frozen=True)
class ShadowCollectionResult:
    """One shadow collection pass (counting invariant: collected equals
    stored + duplicates + malformed)."""

    feeds: int  # enabled feeds tried
    collected: int  # items fetched in total
    stored: int  # unseen items recorded in the shadow database
    duplicates: int  # items already seen in an earlier pass
    malformed: int  # items without a url or title, neither stored nor deduped
    empty_feeds: tuple[str, ...]  # feeds that returned nothing this pass
    per_source: tuple[SourceStat, ...]  # feeds the empty-source breaker reads
    stored_items: tuple[NewsItem, ...]


class ShadowNewsCollector:
    """Fetch the confirmed feeds and dedup them into the shadow store."""

    def __init__(
        self,
        sources: Sequence[NewsSource],
        fetcher: Fetcher,
        store: ShadowNewsRepository,
        *,
        max_news_per_source: int = DEFAULT_MAX_NEWS_PER_SOURCE,
        timeout: int = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._sources = tuple(sources)
        self._fetcher = fetcher
        self._store = store
        self._max_news_per_source = max_news_per_source
        self._timeout = timeout

    async def collect(self) -> ShadowCollectionResult:
        """One pass over every enabled feed; the run itself is logged.

        Feeds are fetched sequentially, like the legacy pipeline: seven RSS
        feeds per hour put no parallelism pressure worth the complexity.
        Items with an empty url or title are counted as malformed and
        skipped — an empty-string url would poison URL dedup (one row would
        shadow every other malformed item).
        """
        stats: list[SourceStat] = []
        empty_feeds: list[str] = []
        stored_items: list[NewsItem] = []
        collected = 0
        duplicates = 0
        malformed = 0
        for source in self._sources:
            if not source.enabled:
                continue
            items = await self._fetcher.fetch(
                as_source_config(source),
                max_news=self._max_news_per_source,
                timeout=self._timeout,
            )
            stats.append(SourceStat(source=source.name, items=len(items)))
            if not items:
                empty_feeds.append(source.name)
            collected += len(items)
            for item in items:
                if not item.url or not item.title:
                    malformed += 1
                    continue
                if await self._store.is_seen(item.url):
                    duplicates += 1
                    continue
                await self._store.mark_seen(item)
                stored_items.append(item)
        result = ShadowCollectionResult(
            feeds=len(stats),
            collected=collected,
            stored=len(stored_items),
            duplicates=duplicates,
            malformed=malformed,
            empty_feeds=tuple(empty_feeds),
            per_source=tuple(stats),
            stored_items=tuple(stored_items),
        )
        await self._store.log_run(
            feeds=result.feeds,
            collected=collected,
            stored=result.stored,
            duplicates=duplicates,
            malformed=malformed,
            empty_feeds=len(empty_feeds),
        )
        logger.info(
            "shadow_news feeds=%s collected=%s stored=%s duplicates=%s "
            "malformed=%s empty_feeds=%s",
            result.feeds,
            collected,
            result.stored,
            duplicates,
            malformed,
            len(empty_feeds),
        )
        return result


async def shadow_news_loop(
    collector: ShadowNewsCollector,
    *,
    interval_seconds: float = SHADOW_COLLECT_INTERVAL_SECONDS,
) -> None:
    """Pace and shield the shadow collection (Stage 0, plan section 14).

    A failed pass logs and waits for the next one — the shadow period must
    survive a flaky feed or a locked file for its whole 2-3 days.
    """
    while True:
        try:
            await collector.collect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("shadow_news_error error=%s", exc)
        await asyncio.sleep(interval_seconds)
