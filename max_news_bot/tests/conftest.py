from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from max_news_bot.config.loader import (
    AppConfig,
    AppSettings,
    FactoryConfig,
    FilterSettings,
    LlmConfig,
    SourceConfig,
)
from max_news_bot.core.factory import ContentFactory
from max_news_bot.core.models import NewsItem
from max_news_bot.utils.db import Database

from max_news_bot.tests.mocks import (
    MockAlerter,
    MockFetcher,
    MockPostPublisher,
    MockPublisher,
    MockWriter,
)


def make_factory_config(**overrides) -> FactoryConfig:
    """Hand-built FactoryConfig (no load_config / yaml involved)."""
    defaults: dict = dict(
        enabled=True,
        dry_run=False,
        interval_hours=4,
        posts_per_cycle=1,
        max_posts_per_day=10,
        max_attempts=3,
        stale_draft_minutes=30,
        max_news_age_hours=24,
        max_post_chars=1800,
        active_hours=None,
        timezone="UTC",
        alert_after_failed_cycles=3,
        circuit_breaker_after=3,
        max_news_per_source=5,
        include_keywords=[],
        exclude_keywords=[],
        llm=LlmConfig(
            base_url="http://localhost:8080/v1",
            model="test-model",
            timeout=30,
            max_tokens=800,
            temperature=0.7,
            api_key="test-api-key",
        ),
        prompt="test prompt",
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


@pytest.fixture
def sample_item() -> NewsItem:
    return NewsItem(
        title="Python 3.13 released",
        url="https://example.com/python-313",
        source="Python Insider",
        published_at=datetime(2026, 5, 25, tzinfo=timezone.utc),
        category="python",
    )


@pytest.fixture
def sample_items() -> list[NewsItem]:
    return [
        NewsItem(
            title="Python news",
            url="https://example.com/1",
            source="Source A",
            category="python",
        ),
        NewsItem(
            title="Rust news",
            url="https://example.com/2",
            source="Source B",
            category="rust",
        ),
    ]


@pytest.fixture
def factory_config() -> FactoryConfig:
    return make_factory_config()


@pytest.fixture
def app_config(tmp_path: Path, factory_config: FactoryConfig) -> AppConfig:
    return AppConfig(
        bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        channel_id="-1001234567890",
        admin_ids=[111, 222],
        log_level="INFO",
        settings=AppSettings(
            post_interval_hours=6,
            max_news_per_source=5,
            request_timeout=30,
        ),
        filters=FilterSettings(
            include_keywords=[],
            exclude_keywords=[],
        ),
        sources=[
            SourceConfig(
                name="Test RSS",
                type="rss",
                url="https://example.com/feed.xml",
                enabled=True,
                category="test",
            ),
            SourceConfig(
                name="Disabled RSS",
                type="rss",
                url="https://example.com/disabled.xml",
                enabled=False,
                category="test",
            ),
        ],
        data_path=tmp_path / "max_news_bot.db",
        factory=factory_config,
    )


@pytest.fixture
async def db(app_config: AppConfig) -> Database:
    database = Database(app_config.data_path)
    await database.initialize()
    return database


@pytest.fixture
def mock_fetcher() -> MockFetcher:
    return MockFetcher()


@pytest.fixture
def mock_publisher() -> MockPublisher:
    return MockPublisher()


@pytest.fixture
def factory(app_config: AppConfig, db: Database) -> ContentFactory:
    """Default factory wiring with mock collaborators.

    Tests needing specific scenarios build their own ContentFactory via
    make_factory_config() and replace(app_config, factory=...).
    """
    return ContentFactory(
        app_config,
        MockFetcher(),
        MockWriter(),
        MockPostPublisher(),
        MockAlerter(),
        db,
    )


@pytest.fixture
def mock_message() -> AsyncMock:
    message = AsyncMock()
    message.answer = AsyncMock()
    return message
