"""Tests of the rubrics contour wiring (plan sections 8, 14).

``build_rubrics_runtime`` is the composition root behind RUBRICS_ENABLED:
every enabled rubric must resolve to a pipeline of its kind, the dry-run
flag must reach the slot scheduler, and the digest pipeline must consult
the scheduler's empty-source breaker. The router also degrades honestly
for a rubric with no pipeline bound.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from max_news_bot.bank.pipeline import BankPipeline
from max_news_bot.bot.rubrics_runtime import RubricPipelineRouter, build_rubrics_runtime
from max_news_bot.bot.slot_scheduler import GenerationOutcome
from max_news_bot.config.loader import AppConfig, AppSettings, FilterSettings
from max_news_bot.config.rubrics import RubricConfig, load_rubrics_config
from max_news_bot.rubrics.digest import DigestPipeline
from max_news_bot.rubrics.generative import GenerativePipeline
from max_news_bot.tests.conftest import make_factory_config
from max_news_bot.tests.mocks import MockFetcher
from max_news_bot.utils.db import Database
from max_news_bot.weather.pipeline import WeatherPipeline

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


class DummyMaxClient:
    """Stands in for MaxClient: constructed, never called in these tests."""

    async def send_message(self, chat_id: int, text: str, **kwargs) -> str:
        return "mid.1"

    async def get_me(self) -> dict:
        return {"name": "dummy"}

    async def patch_commands(self, commands) -> None:
        return None


class StubPipeline:
    def __init__(self) -> None:
        self.generate_calls: list[str] = []

    async def generate(self, rubric, run, *, now) -> GenerationOutcome:
        self.generate_calls.append(rubric.id)
        return GenerationOutcome(status="generated", text=f"text of {rubric.id}")

    async def fallback_post(self, rubric, run, *, now) -> GenerationOutcome:
        return GenerationOutcome(status="failed", error="no reserve")


def _rubric(**overrides) -> RubricConfig:
    defaults: dict = dict(
        id="bound_rubric",
        enabled=True,
        time="07:00",
        days=("mon",),
        grace_minutes=30,
        jitter_minutes=0,
        kind="bank",
        target="main",
        source=None,
        topics_by_weekday={},
        prompt_overlay=None,
        min_chars=10,
        max_chars=100,
        image=None,
        history_n=3,
        similarity_threshold=0.7,
        validators=(),
        on_llm_failure="bank",
    )
    defaults.update(overrides)
    return RubricConfig(**defaults)


@pytest.fixture
def app_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """AppConfig over the shipped rubrics.yaml, contour enabled."""
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    rubrics = load_rubrics_config(enabled=True)
    return AppConfig(
        bot_token="123456789:test-token",
        channel_id=-1001234567890,
        admin_ids=[111, 222],
        log_level="INFO",
        settings=AppSettings(
            post_interval_hours=6, max_news_per_source=5, request_timeout=30
        ),
        filters=FilterSettings(include_keywords=[], exclude_keywords=[]),
        sources=[],
        data_path=tmp_path / "max_news_bot.db",
        factory=make_factory_config(enabled=False, dry_run=False),
        rubrics=rubrics,
    )


@pytest.fixture
async def db(app_config: AppConfig) -> Database:
    database = Database(app_config.data_path)
    await database.initialize()
    return database


async def test_runtime_binds_every_enabled_rubric(
    app_config: AppConfig, db: Database
) -> None:
    runtime = build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    router = runtime.scheduler._pipeline
    by_kind = {
        rubric.kind: router.pipeline_for(rubric)
        for rubric in app_config.rubrics.rubrics
        if rubric.enabled
    }
    assert isinstance(by_kind["generative"], GenerativePipeline)
    assert isinstance(by_kind["bank"], BankPipeline)
    assert isinstance(by_kind["digest"], DigestPipeline)
    assert isinstance(by_kind["weather"], WeatherPipeline)
    # per-rubric binding: every enabled rubric id resolves on its own
    for rubric in app_config.rubrics.rubrics:
        if rubric.enabled:
            assert router.pipeline_for(rubric) is not None


async def test_runtime_passes_dry_run_to_scheduler(
    app_config: AppConfig, db: Database
) -> None:
    runtime = build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    assert app_config.rubrics.dry_run is True  # shipped default
    assert runtime.scheduler._dry_run is True

    flipped = build_rubrics_runtime(
        replace(app_config, rubrics=replace(app_config.rubrics, dry_run=False)),
        db,
        DummyMaxClient(),
        None,
        MockFetcher(),
    )
    assert flipped.scheduler._dry_run is False


async def test_digest_pipeline_uses_scheduler_source_breaker(
    app_config: AppConfig, db: Database
) -> None:
    runtime = build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    digest = runtime.scheduler._pipeline.pipeline_for(
        next(r for r in app_config.rubrics.rubrics if r.kind == "digest")
    )
    assert isinstance(digest, DigestPipeline)
    # the hook is the scheduler's own breaker: three empty responses in a
    # row exclude the feed from the next digest collection (plan section 11)
    for _ in range(3):
        await runtime.scheduler.sources.record("some-feed", 0)
    assert digest._is_excluded_source("some-feed") is True
    assert digest._is_excluded_source("other-feed") is False


async def test_smoke_stub_rubric_is_wired_for_dry_run_shakedown(
    app_config: AppConfig, db: Database
) -> None:
    runtime = build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    smoke = next(
        rubric for rubric in app_config.rubrics.rubrics if rubric.id == "smoke_test"
    )
    # ships disabled: an enabled 7th slot would exceed daily_cap (plan
    # section 2); enabling it for a shakedown binds the bank pipeline
    assert smoke.enabled is False
    assert isinstance(
        runtime.scheduler._pipeline.pipeline_for(replace(smoke, enabled=True)),
        BankPipeline,
    )
    # the shakedown stub runs the full cycle with admin previews, not
    # channel posts (plan section 14, stage 1)
    assert runtime.scheduler._dry_run is True


async def test_runtime_fails_honestly_without_llm_key_for_llm_kinds(
    app_config: AppConfig, db: Database
) -> None:
    # loader validation normally prevents this state; the wiring stays
    # honest if it ever sees it anyway
    from max_news_bot.config.loader import LlmConfig

    no_key = replace(
        app_config,
        factory=make_factory_config(
            enabled=False,
            dry_run=False,
            llm=LlmConfig(
                base_url="http://localhost:8080/v1",
                model="test-model",
                timeout=30,
                max_tokens=800,
                temperature=0.7,
                api_key="",
            ),
        ),
    )

    with pytest.raises(ValueError, match="LLM_API_KEY"):
        build_rubrics_runtime(no_key, db, DummyMaxClient(), None, MockFetcher())


async def test_runtime_without_llm_kinds_needs_no_key(
    app_config: AppConfig, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bank/weather only contour: no completion client is built and no key
    # is required at wiring time
    from max_news_bot.config.loader import LlmConfig

    bank_weather_only = replace(
        app_config,
        rubrics=replace(
            app_config.rubrics,
            rubrics=tuple(
                rubric
                for rubric in app_config.rubrics.rubrics
                if rubric.kind in ("bank", "weather")
            ),
        ),
        factory=make_factory_config(
            enabled=False,
            dry_run=False,
            llm=LlmConfig(
                base_url="http://localhost:8080/v1",
                model="test-model",
                timeout=30,
                max_tokens=800,
                temperature=0.7,
                api_key="",
            ),
        ),
    )

    runtime = build_rubrics_runtime(
        bank_weather_only, db, DummyMaxClient(), None, MockFetcher()
    )

    router = runtime.scheduler._pipeline
    for rubric in bank_weather_only.rubrics.rubrics:
        assert router.pipeline_for(rubric) is not None


# --- router ---------------------------------------------------------------------


async def test_router_dispatches_by_id_then_kind() -> None:
    router = RubricPipelineRouter()
    stub = StubPipeline()
    bound = _rubric()
    # same kind, different id: resolves through the kind fallback (a
    # rubric disabled in the config after a restart, recovery path)
    unbound = _rubric(id="other_rubric_of_same_kind")

    router.bind(bound, stub)
    outcome = await router.generate(bound, None, now=NOW)
    assert outcome.status == "generated" and outcome.text == "text of bound_rubric"

    kind_outcome = await router.generate(unbound, None, now=NOW)
    assert kind_outcome.status == "generated"
    assert stub.generate_calls == ["bound_rubric", "other_rubric_of_same_kind"]


async def test_router_fails_config_on_unbound_rubric() -> None:
    router = RubricPipelineRouter()
    orphan = _rubric(id="orphan", kind="weather")

    missing = await router.fallback_post(orphan, None, now=NOW)

    assert missing.status == "failed"
    assert missing.error_kind == "config"
    assert "no pipeline bound" in (missing.error or "")
