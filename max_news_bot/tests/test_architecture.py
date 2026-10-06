from __future__ import annotations

from pathlib import Path

import pytest

from max_news_bot.config.loader import load_config


def test_no_heavy_dependencies_in_pyproject() -> None:
    pyproject = (
        Path(__file__).resolve().parents[2] / "pyproject.toml"
    ).read_text(encoding="utf-8").lower()
    banned = ["beautifulsoup", "lxml", "celery", "kafka", "psycopg", "sqlalchemy"]
    for package in banned:
        assert package not in pyproject


def test_pipeline_has_no_telegram_imports() -> None:
    pipeline_source = (
        Path(__file__).resolve().parents[1] / "core" / "pipeline.py"
    ).read_text(encoding="utf-8")
    assert "aiogram" not in pipeline_source
    assert "TelegramPublisher" not in pipeline_source


def test_factory_has_no_telegram_imports() -> None:
    factory_source = (
        Path(__file__).resolve().parents[1] / "core" / "factory.py"
    ).read_text(encoding="utf-8")
    assert "aiogram" not in factory_source
    assert "TelegramPublisher" not in factory_source
    assert "TelegramAlerter" not in factory_source


def test_models_define_news_item_and_publisher_result() -> None:
    from max_news_bot.core.models import NewsItem, PublishedResult
    from max_news_bot.publishers.base import Publisher

    item = NewsItem(title="t", url="https://example.com", source="s")
    assert item.title == "t"
    assert hasattr(Publisher, "publish") or "publish" in Publisher.__dict__
    result = PublishedResult(success=True, published=1)
    assert result.success is True


def test_missing_bot_token_fail_fast_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")

    with pytest.raises(ValueError, match="MAX_BOT_TOKEN and MAX_CHANNEL_ID are required"):
        load_config()
