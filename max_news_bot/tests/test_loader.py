from __future__ import annotations

import pytest

from max_news_bot.config.loader import load_config


@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # hermetic: a developer's real max_news_bot/.env must not leak into defaults
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "123456789:test-token")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_IDS", "111,222,333")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    # the shipped rubrics.yaml enables LLM rubrics: both channel keys are
    # required for the default tests; overrides are cleared for hermeticity
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("RUBRICS_DRY_RUN", raising=False)
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)


def test_load_config_reads_env_and_rubrics_yaml(env_vars: None) -> None:
    config = load_config()

    assert config.bot_token == "123456789:test-token"
    # MAX chat_id is int64: parsed as int, not kept as a string
    assert config.channel_id == -1001234567890
    assert isinstance(config.channel_id, int)
    assert config.admin_ids == [111, 222, 333]
    assert config.log_level == "DEBUG"
    # the contour config comes from rubrics.yaml (the single config file)
    assert config.rubrics.timezone == "Europe/Moscow"
    assert config.rubrics.llm.primary.base_url == "https://openrouter.ai/api/v1"
    assert config.rubrics.llm.primary.model == "qwen/qwen3.8-flash"
    assert config.rubrics.llm.primary.api_key == "test-openrouter-key"
    assert config.rubrics.llm.fallback is not None
    assert config.rubrics.llm.fallback.api_key == "test-llm-key"
    # dry-run is the deploy default: previews to admins
    assert config.rubrics.dry_run is True


def test_llm_env_overrides(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8080/v1")
    monkeypatch.setenv("LLM_MODEL", "custom-model")
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://localhost:9090/v1")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "fallback-model")

    config = load_config()

    assert config.rubrics.llm.primary.base_url == "http://localhost:8080/v1"
    assert config.rubrics.llm.primary.model == "custom-model"
    assert config.rubrics.llm.fallback is not None
    assert config.rubrics.llm.fallback.base_url == "http://localhost:9090/v1"
    assert config.rubrics.llm.fallback.model == "fallback-model"


def test_missing_bot_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")

    with pytest.raises(ValueError, match="MAX_BOT_TOKEN and MAX_CHANNEL_ID are required"):
        load_config()


def test_missing_channel_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "123456789:test-token")
    monkeypatch.setenv("MAX_CHANNEL_ID", "")

    with pytest.raises(ValueError, match="MAX_BOT_TOKEN and MAX_CHANNEL_ID are required"):
        load_config()


def test_invalid_channel_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("MAX_BOT_TOKEN", "123456789:test-token")
    monkeypatch.setenv("MAX_CHANNEL_ID", "not-a-number")

    with pytest.raises(ValueError, match="MAX_CHANNEL_ID"):
        load_config()


def test_admin_ids_ignores_invalid_tokens(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_IDS", "111, abc, 222, ,456")

    config = load_config()

    assert config.admin_ids == [111, 222, 456]


def test_data_path_points_to_sqlite_file(env_vars: None) -> None:
    config = load_config()

    assert config.data_path.name == "max_news_bot.db"
    assert config.data_path.parent.name == "data"


def test_database_path_env_override(env_vars: None, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    custom_path = tmp_path / "custom.db"
    monkeypatch.setenv("DATABASE_PATH", str(custom_path))

    config = load_config()

    assert config.data_path == custom_path


def test_dry_run_without_admin_ids_raises(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # dry-run previews are delivered to admins: without them every post
    # would fail, so fail fast at startup instead
    monkeypatch.delenv("ADMIN_IDS", raising=False)

    with pytest.raises(ValueError, match="ADMIN_IDS"):
        load_config()


def test_dry_run_off_with_admin_ids_is_fine(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUBRICS_DRY_RUN", "false")
    monkeypatch.delenv("ADMIN_IDS", raising=False)

    config = load_config()

    assert config.rubrics.dry_run is False
