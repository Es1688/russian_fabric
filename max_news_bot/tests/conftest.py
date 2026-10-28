from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from max_news_bot.config.loader import AppConfig
from max_news_bot.config.rubrics import (
    LlmChannelsConfig,
    LlmConfig,
    RubricsConfig,
    TargetConfig,
)
from max_news_bot.core.models import NewsItem
from max_news_bot.utils.db import Database

from max_news_bot.tests.mocks import MockFetcher


def make_llm_channels(
    *, with_fallback: bool = True, api_key: str = "test-api-key"
) -> LlmChannelsConfig:
    """Hand-built LLM channels (no yaml involved)."""
    return LlmChannelsConfig(
        primary=LlmConfig(
            base_url="http://localhost:8080/v1",
            model="test-model",
            timeout=30,
            max_tokens=800,
            temperature=0.7,
            api_key=api_key,
        ),
        fallback=(
            LlmConfig(
                base_url="http://localhost:8081/v1",
                model="test-fallback-model",
                timeout=30,
                max_tokens=800,
                temperature=0.7,
                api_key=api_key,
            )
            if with_fallback
            else None
        ),
    )


def make_rubrics_config(**overrides) -> RubricsConfig:
    """Minimal hand-built RubricsConfig (no load_config / yaml involved)."""
    defaults: dict = dict(
        timezone="UTC",
        persona="",
        image_style="",
        targets={"main": TargetConfig(name="main", channel_id=-1001234567890)},
        daily_cap=6,
        weather_points=(),
        rubrics=(),
        llm=make_llm_channels(),
        summary_time="22:40",
        dry_run=False,
    )
    defaults.update(overrides)
    return RubricsConfig(**defaults)


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
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        channel_id=-1001234567890,
        admin_ids=[111, 222],
        log_level="INFO",
        data_path=tmp_path / "max_news_bot.db",
        rubrics=make_rubrics_config(),
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
def mock_message() -> AsyncMock:
    message = AsyncMock()
    message.answer = AsyncMock()
    return message
