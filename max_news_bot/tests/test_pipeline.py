from __future__ import annotations

import pytest

from max_news_bot.config.loader import AppConfig, FilterSettings
from max_news_bot.core.models import NewsItem
from max_news_bot.core.pipeline import Pipeline
from max_news_bot.tests.mocks import MockFetcher, MockPublisher
from max_news_bot.utils.db import Database


@pytest.mark.asyncio
async def test_successful_publish_marks_items_sent_and_logs(
    app_config: AppConfig,
    db: Database,
    sample_items: list[NewsItem],
) -> None:
    fetcher = MockFetcher(items=sample_items)
    publisher = MockPublisher(success=True)
    pipeline = Pipeline(app_config, fetcher, publisher, db)

    result = await pipeline.run()

    assert result.published == 2
    assert len(publisher.publish_calls) == 1
    assert len(publisher.publish_calls[0]) == 2
    for item in sample_items:
        assert await db.is_sent(item.url) is True

    publish_event = await db.get_last_event("publish")
    assert publish_event is not None
    assert "published=2" in publish_event["details"]


@pytest.mark.asyncio
async def test_publisher_failure_does_not_mark_sent(
    app_config: AppConfig,
    db: Database,
    sample_items: list[NewsItem],
) -> None:
    fetcher = MockFetcher(items=sample_items)
    publisher = MockPublisher(success=False)
    pipeline = Pipeline(app_config, fetcher, publisher, db)

    result = await pipeline.run()

    assert result.published == 0
    for item in sample_items:
        assert await db.is_sent(item.url) is False

    error_event = await db.get_last_event("error")
    assert error_event is not None
    assert error_event["details"] == "publisher_failed"


@pytest.mark.asyncio
async def test_empty_news_list_publishes_empty(
    app_config: AppConfig,
    db: Database,
) -> None:
    fetcher = MockFetcher(items=[])
    publisher = MockPublisher(success=True)
    pipeline = Pipeline(app_config, fetcher, publisher, db)

    result = await pipeline.run()

    assert result.published == 0
    assert publisher.publish_calls == [[]]


@pytest.mark.asyncio
async def test_filter_excludes_items_before_publish(
    app_config: AppConfig,
    db: Database,
) -> None:
    items = [
        NewsItem(title="Python release", url="https://example.com/py", source="A"),
        NewsItem(title="Random news", url="https://example.com/rnd", source="B"),
    ]
    config = AppConfig(
        bot_token=app_config.bot_token,
        channel_id=app_config.channel_id,
        admin_ids=app_config.admin_ids,
        log_level=app_config.log_level,
        settings=app_config.settings,
        filters=FilterSettings(include_keywords=["python"], exclude_keywords=[]),
        sources=app_config.sources,
        data_path=app_config.data_path,
        factory=app_config.factory,
        rubrics=app_config.rubrics,
    )
    fetcher = MockFetcher(items=items)
    publisher = MockPublisher(success=True)
    pipeline = Pipeline(config, fetcher, publisher, db)

    result = await pipeline.run()

    assert result.published == 1
    assert len(publisher.publish_calls[0]) == 1
    assert publisher.publish_calls[0][0].title == "Python release"
    assert await db.is_sent("https://example.com/py") is True
    assert await db.is_sent("https://example.com/rnd") is False


@pytest.mark.asyncio
async def test_duplicate_items_skipped(
    app_config: AppConfig,
    db: Database,
    sample_item: NewsItem,
) -> None:
    await db.mark_sent(sample_item)
    fetcher = MockFetcher(items=[sample_item])
    publisher = MockPublisher(success=True)
    pipeline = Pipeline(app_config, fetcher, publisher, db)

    result = await pipeline.run()

    assert result.published == 0
    assert publisher.publish_calls == [[]]


@pytest.mark.asyncio
async def test_disabled_sources_not_fetched(
    app_config: AppConfig,
    db: Database,
) -> None:
    fetcher = MockFetcher(items=[])
    publisher = MockPublisher(success=True)
    pipeline = Pipeline(app_config, fetcher, publisher, db)

    await pipeline.run()

    fetched_names = [call[0].name for call in fetcher.fetch_calls]
    assert "Test RSS" in fetched_names
    assert "Disabled RSS" not in fetched_names


@pytest.mark.asyncio
async def test_pipeline_does_not_import_telegram_api() -> None:
    import max_news_bot.core.pipeline as pipeline_module

    source = pipeline_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        content = handle.read()
    assert "aiogram" not in content
    assert "TelegramPublisher" not in content
