from __future__ import annotations

from pathlib import Path

import pytest

from max_news_bot.config.loader import load_config

_PACKAGE = Path(__file__).resolve().parents[1]


def test_no_heavy_dependencies_in_pyproject() -> None:
    pyproject = (
        Path(__file__).resolve().parents[2] / "pyproject.toml"
    ).read_text(encoding="utf-8").lower()
    banned = ["beautifulsoup", "lxml", "celery", "kafka", "psycopg", "sqlalchemy"]
    for package in banned:
        assert package not in pyproject


def test_package_has_no_telegram_imports() -> None:
    # the port must stay MAX-native: no aiogram/Telegram leftovers anywhere
    # in the production code (this test module itself mentions them)
    for source in _PACKAGE.rglob("*.py"):
        if "tests" in source.relative_to(_PACKAGE).parts:
            continue
        text = source.read_text(encoding="utf-8")
        assert "import aiogram" not in text, source
        assert "from aiogram" not in text, source
        assert "TelegramPublisher" not in text, source
        assert "TelegramAlerter" not in text, source


def test_models_define_news_item() -> None:
    from max_news_bot.core.models import NewsItem

    item = NewsItem(title="t", url="https://example.com", source="s")
    assert item.title == "t"


def test_missing_bot_token_fail_fast_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    # hermetic: a developer's real max_news_bot/.env (e.g. RUBRICS_DRY_RUN)
    # would otherwise leak into os.environ for the whole pytest session
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")

    with pytest.raises(ValueError, match="MAX_BOT_TOKEN and MAX_CHANNEL_ID are required"):
        load_config()
