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
    monkeypatch.delenv("POST_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("REQUEST_TIMEOUT", raising=False)
    # sources.yaml ships with factory.enabled: true -> a (fake) key is
    # required for the default tests; overrides are cleared for hermeticity
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.delenv("FACTORY_ENABLED", raising=False)
    monkeypatch.delenv("FACTORY_DRY_RUN", raising=False)
    monkeypatch.delenv("FACTORY_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    # the rubrics contour is off in every loader test; a developer's real
    # .env must not flip it on and turn loading into a contour conflict
    monkeypatch.delenv("RUBRICS_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)


def test_load_config_reads_yaml(env_vars: None) -> None:
    config = load_config()

    assert config.bot_token == "123456789:test-token"
    # MAX chat_id is int64: parsed as int, not kept as a string
    assert config.channel_id == -1001234567890
    assert isinstance(config.channel_id, int)
    assert config.settings.max_news_per_source == 5
    assert config.filters.include_keywords == []
    assert config.filters.exclude_keywords == []
    assert len(config.sources) == 2
    assert config.sources[0].name == "Habr"
    assert config.sources[0].type == "rss"
    assert config.sources[0].url == "https://habr.com/ru/rss/news/"
    assert config.sources[0].enabled is True
    assert config.sources[1].name == "vc.ru"
    assert config.sources[1].type == "rss"
    assert config.sources[1].url == "https://vc.ru/rss/all"
    assert config.sources[1].enabled is True


def test_mvp_sources_are_rss_only(env_vars: None) -> None:
    config = load_config()

    assert config.sources
    assert all(source.type == "rss" for source in config.sources)


def test_env_overrides_yaml(env_vars: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POST_INTERVAL_HOURS", "12")
    monkeypatch.setenv("REQUEST_TIMEOUT", "45")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    config = load_config()

    assert config.settings.post_interval_hours == 12
    assert config.settings.request_timeout == 45
    assert config.log_level == "WARNING"


def test_missing_bot_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")

    with pytest.raises(ValueError, match="MAX_BOT_TOKEN and MAX_CHANNEL_ID are required"):
        load_config()


def test_missing_channel_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_admin_ids_parsed_from_comma_string(env_vars: None) -> None:
    config = load_config()

    assert config.admin_ids == [111, 222, 333]


def test_admin_ids_ignores_invalid_tokens(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_IDS", "111, abc, 222, ,456")

    config = load_config()

    assert config.admin_ids == [111, 222, 456]


def test_empty_sources_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "123456789:test-token")
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    monkeypatch.setattr(
        "max_news_bot.config.loader.yaml.safe_load",
        lambda _content: {"settings": {}, "filters": {}, "sources": []},
    )
    # the safe_load patch hits the shared yaml module, so the rubrics loader
    # would see this fake dict too: bypass it with a contour-off config
    from max_news_bot.config.rubrics import empty_rubrics_config

    monkeypatch.setattr(
        "max_news_bot.config.loader.load_rubrics_config",
        lambda *args, **kwargs: empty_rubrics_config(),
    )

    config = load_config()

    assert config.sources == []


def test_data_path_points_to_sqlite_file(env_vars: None) -> None:
    config = load_config()

    assert config.data_path.name == "max_news_bot.db"
    assert config.data_path.parent.name == "data"


def test_database_path_env_override(env_vars: None, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    custom_path = tmp_path / "custom.db"
    monkeypatch.setenv("DATABASE_PATH", str(custom_path))

    config = load_config()

    assert config.data_path == custom_path


# --- factory section ---------------------------------------------------------


def test_factory_defaults_from_yaml(env_vars: None) -> None:
    config = load_config()

    factory = config.factory
    # ships disabled: enabling requires FACTORY_ENABLED=true + LLM_API_KEY,
    # so an existing deployment survives the merge without new secrets
    assert factory.enabled is False
    assert factory.dry_run is True
    assert factory.interval_hours == 4
    assert factory.posts_per_cycle == 1
    assert factory.max_posts_per_day == 10
    assert factory.max_attempts == 3
    assert factory.stale_draft_minutes == 30
    assert factory.max_news_age_hours == 24
    assert factory.max_post_chars == 1800
    assert factory.active_hours == "08:00-23:00"
    assert factory.timezone == "Europe/Moscow"
    assert factory.alert_after_failed_cycles == 3
    assert factory.circuit_breaker_after == 3
    assert factory.max_news_per_source == 5
    assert factory.include_keywords == []
    assert factory.exclude_keywords == []
    # llm.base_url/model name the provider and legitimately change per
    # deployment (yaml or env); the shipped defaults target Yandex AI Studio
    # (FOLDER_ID placeholder must be replaced with the real folder id)
    assert factory.llm.base_url == "https://ai.api.cloud.yandex.net/v1"
    assert factory.llm.model == "gpt://FOLDER_ID/yandexgpt-5-lite"
    assert factory.llm.timeout == 60
    assert factory.llm.max_tokens == 800
    assert factory.llm.temperature == 0.7
    assert factory.llm.api_key == "test-llm-key"  # env-only, never from yaml
    assert "SKIP" in factory.prompt
    assert "<article>" in factory.prompt


def test_factory_env_overrides_yaml(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_ENABLED", "false")
    monkeypatch.setenv("FACTORY_INTERVAL_HOURS", "6")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8080/v1")
    monkeypatch.setenv("LLM_MODEL", "custom-model")

    config = load_config()

    assert config.factory.enabled is False
    assert config.factory.interval_hours == 6
    assert config.factory.llm.base_url == "http://localhost:8080/v1"
    assert config.factory.llm.model == "custom-model"


def test_factory_dry_run_env_overrides_yaml(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rollout switch on the VPS: FACTORY_DRY_RUN=false turns on channel
    # publishing without touching sources.yaml in the repo
    monkeypatch.setenv("FACTORY_DRY_RUN", "false")

    config = load_config()

    assert config.factory.dry_run is False


def test_factory_llm_defaults_to_yandex(env_vars: None) -> None:
    # env_vars keeps this hermetic: a real .env loaded by load_dotenv()
    # inside an earlier load_config() call lingers in os.environ for the
    # whole pytest session and would override the defaults under test
    from max_news_bot.config.loader import _parse_factory

    config = _parse_factory({})

    assert config.llm.base_url == "https://ai.api.cloud.yandex.net/v1"
    assert config.llm.model == "gpt://FOLDER_ID/yandexgpt-5-lite"


def test_factory_enabled_without_api_key_raises(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # neutralize a developer's real .env so the test is hermetic
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # yaml ships disabled, so enable explicitly
    monkeypatch.setenv("FACTORY_ENABLED", "true")

    with pytest.raises(ValueError, match="LLM_API_KEY"):
        load_config()


def test_factory_enabled_via_env_flips_the_flag(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_ENABLED", "true")

    config = load_config()

    assert config.factory.enabled is True


def test_factory_disabled_without_api_key_is_fine(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("FACTORY_ENABLED", "false")

    config = load_config()

    assert config.factory.enabled is False
    assert config.factory.llm.api_key == ""


def test_factory_dry_run_without_admin_ids_raises(
    env_vars: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # dry-run previews are delivered to admins: without them every post
    # would fail, so fail fast at startup instead
    monkeypatch.setattr("max_news_bot.config.loader.load_dotenv", lambda: None)
    monkeypatch.setenv("FACTORY_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.delenv("ADMIN_IDS", raising=False)

    with pytest.raises(ValueError, match="ADMIN_IDS"):
        load_config()
