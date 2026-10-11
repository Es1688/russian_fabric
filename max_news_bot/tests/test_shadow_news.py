from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from max_news_bot.config.news_sources import NewsSource
from max_news_bot.core.models import NewsItem
from max_news_bot.rubrics.news import (
    ShadowCollectionResult,
    ShadowNewsCollector,
    as_source_config,
    shadow_db_path,
    shadow_news_loop,
)
from max_news_bot.tests.mocks import MockFetcher
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import ShadowNewsRepository


def _feed(name: str, *, enabled: bool = True) -> NewsSource:
    slug = name.lower().replace(" ", "-")
    return NewsSource(
        name=name, url=f"https://{slug}.example/rss", section="science",
        enabled=enabled,
    )


def _item(url: str, source: str = "Feed A", title: str | None = "News") -> NewsItem:
    return NewsItem(
        title=title or "",
        url=url,
        source=source,
        published_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        category="science",
        summary="Одна фраза описания",
    )


@pytest.fixture
async def store(tmp_path: Path) -> ShadowNewsRepository:
    repository = ShadowNewsRepository(tmp_path / "news_shadow.db")
    await repository.initialize()
    return repository


# --- repository -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_seen_round_trip(store: ShadowNewsRepository) -> None:
    item = _item("https://example.com/1")

    assert await store.is_seen(item.url) is False
    await store.mark_seen(item)

    assert await store.is_seen(item.url) is True
    assert await store.count_seen() == 1


@pytest.mark.asyncio
async def test_mark_seen_is_idempotent(store: ShadowNewsRepository) -> None:
    item = _item("https://example.com/1")

    await store.mark_seen(item)
    await store.mark_seen(item)

    assert await store.count_seen() == 1


@pytest.mark.asyncio
async def test_recent_items_return_the_stored_fields(
    store: ShadowNewsRepository,
) -> None:
    first = _item("https://example.com/1")
    second = _item("https://example.com/2")
    await store.mark_seen(first)
    await store.mark_seen(second)

    items = await store.recent_items(limit=10)

    # newest first
    assert [item.url for item in items] == [second.url, first.url]
    assert items[0] == NewsItem(
        title=second.title,
        url=second.url,
        source=second.source,
        published_at=second.published_at,
        category="science",
        summary=second.summary,
    )


@pytest.mark.asyncio
async def test_log_run_round_trip(store: ShadowNewsRepository) -> None:
    await store.log_run(
        feeds=7, collected=105, stored=12, duplicates=90, malformed=3, empty_feeds=1
    )

    runs = await store.recent_runs(limit=5)

    assert len(runs) == 1
    assert runs[0]["feeds"] == 7
    assert runs[0]["collected"] == 105
    assert runs[0]["stored"] == 12
    assert runs[0]["duplicates"] == 90
    assert runs[0]["malformed"] == 3
    assert runs[0]["empty_feeds"] == 1
    assert runs[0]["started_at"] is not None


@pytest.mark.asyncio
async def test_shadow_store_creates_its_own_file(tmp_path: Path) -> None:
    shadow_path = tmp_path / "nested" / "news_shadow.db"

    repository = ShadowNewsRepository(shadow_path)
    await repository.initialize()

    assert shadow_path.exists()
    with sqlite3.connect(shadow_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"seen_news", "shadow_runs"} <= tables


# --- shadow isolation (plan section 14: sent_news/posts stay untouched) ----------


@pytest.mark.asyncio
async def test_collection_never_touches_production_tables(
    tmp_path: Path,
) -> None:
    main_path = tmp_path / "max_news_bot.db"
    await Database(main_path).initialize()  # production schema, v4
    shadow = ShadowNewsRepository(shadow_db_path(main_path))
    await shadow.initialize()
    collector = ShadowNewsCollector(
        [_feed("Feed A")],
        MockFetcher(items=[_item("https://example.com/1")]),
        shadow,
    )

    await collector.collect()

    # the shadow database is a different file and holds the items...
    assert shadow_db_path(main_path) != main_path
    assert await shadow.count_seen() == 1
    # ...while the production dedup tables stay empty and the shadow tables
    # do not even exist in the main database
    with sqlite3.connect(main_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        sent = conn.execute("SELECT COUNT(*) FROM sent_news").fetchone()[0]
        posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    assert "seen_news" not in tables
    assert "shadow_runs" not in tables
    assert sent == 0
    assert posts == 0


# --- collector ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_stores_new_items_and_logs_the_run(
    store: ShadowNewsRepository,
) -> None:
    fetcher = MockFetcher(
        items_by_source={
            "Feed A": [_item("https://example.com/1"), _item("https://example.com/2")],
        }
    )
    collector = ShadowNewsCollector([_feed("Feed A")], fetcher, store)

    result = await collector.collect()

    assert result.feeds == 1
    assert result.collected == 2
    assert result.stored == 2
    assert result.duplicates == 0
    assert result.malformed == 0
    assert result.empty_feeds == ()
    assert [item.url for item in result.stored_items] == [
        "https://example.com/1",
        "https://example.com/2",
    ]
    assert result.per_source[0].source == "Feed A"
    assert result.per_source[0].items == 2
    runs = await store.recent_runs(limit=1)
    assert runs[0]["stored"] == 2


@pytest.mark.asyncio
async def test_second_pass_sees_everything_as_duplicates(
    store: ShadowNewsRepository,
) -> None:
    items = [_item("https://example.com/1"), _item("https://example.com/2")]
    collector = ShadowNewsCollector(
        [_feed("Feed A")], MockFetcher(items=items), store
    )

    first = await collector.collect()
    second = await collector.collect()

    assert first.stored == 2
    assert second.stored == 0
    assert second.duplicates == 2
    assert second.stored_items == ()
    assert await store.count_seen() == 2
    # one run row per pass
    assert len(await store.recent_runs(limit=10)) == 2


@pytest.mark.asyncio
async def test_disabled_feed_is_not_fetched(store: ShadowNewsRepository) -> None:
    fetcher = MockFetcher(items=[_item("https://example.com/1")])
    collector = ShadowNewsCollector(
        [_feed("Feed A"), _feed("Parked", enabled=False)], fetcher, store
    )

    result = await collector.collect()

    assert result.feeds == 1
    assert [call[0].name for call in fetcher.fetch_calls] == ["Feed A"]


@pytest.mark.asyncio
async def test_malformed_items_are_skipped_without_poisoning_dedup(
    store: ShadowNewsRepository,
) -> None:
    items = [
        _item("https://example.com/1"),
        _item("", title="No url"),
        _item("https://example.com/2", title=""),
        _item("", title="Also no url"),
    ]
    collector = ShadowNewsCollector(
        [_feed("Feed A")], MockFetcher(items=items), store
    )

    result = await collector.collect()

    # an empty url must not become a seen_news row shadowing other items
    assert result.malformed == 3
    assert result.stored == 1
    assert result.collected == result.stored + result.duplicates + result.malformed
    assert await store.count_seen() == 1


@pytest.mark.asyncio
async def test_empty_feed_is_reported(store: ShadowNewsRepository) -> None:
    collector = ShadowNewsCollector(
        [_feed("Feed A"), _feed("Silent")],
        MockFetcher(items_by_source={"Feed A": [_item("https://example.com/1")]}),
        store,
    )

    result = await collector.collect()

    assert result.empty_feeds == ("Silent",)
    stats = {stat.source: stat.items for stat in result.per_source}
    assert stats == {"Feed A": 1, "Silent": 0}


@pytest.mark.asyncio
async def test_fetch_limits_are_passed_through(store: ShadowNewsRepository) -> None:
    fetcher = MockFetcher(items=[_item("https://example.com/1")])
    collector = ShadowNewsCollector(
        [_feed("Feed A")], fetcher, store, max_news_per_source=7, timeout=11
    )

    await collector.collect()

    assert fetcher.fetch_calls == [(as_source_config(_feed("Feed A")), 7, 11)]


def test_as_source_config_maps_section_to_category() -> None:
    source = NewsSource(
        name="N+1", url="https://nplus1.ru/rss", section="science", enabled=True
    )

    adapted = as_source_config(source)

    assert adapted.name == "N+1"
    assert adapted.type == "rss"
    assert adapted.url == "https://nplus1.ru/rss"
    assert adapted.enabled is True
    assert adapted.category == "science"


# --- loop ------------------------------------------------------------------------


class _FlakyCollector:
    """Collector double: fails the first pass, succeeds afterwards."""

    def __init__(self) -> None:
        self.results: list[ShadowCollectionResult] = []
        self.failures = 0

    async def collect(self) -> ShadowCollectionResult:
        if not self.results and self.failures == 0:
            self.failures += 1
            raise RuntimeError("feed exploded")
        result = ShadowCollectionResult(
            feeds=1, collected=0, stored=0, duplicates=0, malformed=0,
            empty_feeds=(), per_source=(), stored_items=(),
        )
        self.results.append(result)
        return result


@pytest.mark.asyncio
async def test_loop_survives_a_failed_pass_and_paces() -> None:
    collector = _FlakyCollector()

    task = asyncio.create_task(shadow_news_loop(collector, interval_seconds=0.01))
    # 0.2s at a 0.01s interval leaves room for many passes even on a
    # loaded machine; the loop is cancelled, never awaited to an end
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # the first (failed) pass did not stop the loop
    assert collector.failures == 1
    assert len(collector.results) >= 2
