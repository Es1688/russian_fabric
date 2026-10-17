"""Tests of the rubrics contour wiring (plan sections 8, 14).

``build_rubrics_runtime`` is the composition root behind RUBRICS_ENABLED:
every enabled rubric must resolve to a pipeline of its kind, the dry-run
flag must reach the slot scheduler, and the digest pipeline must consult
the scheduler's empty-source breaker. The router also degrades honestly
for a rubric with no pipeline bound.

Dual-channel wiring (plan §2.4/§2.5): the LLM pipelines receive a
FallbackCompletion over two local fake channels, and the readiness probe
is the construction's only network call - one minimal request per wired
channel; a config-kind failure (401) fails the build, an infra-kind one
(500) only warns.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from max_news_bot.bank.pipeline import BankPipeline
from max_news_bot.bot.rubrics_runtime import (
    GENERATED_DIR_NAME,
    RubricPipelineRouter,
    build_rubrics_runtime,
)
from max_news_bot.bot.slot_scheduler import GenerationOutcome
from max_news_bot.config.loader import AppConfig, AppSettings, FilterSettings, LlmConfig
from max_news_bot.config.rubrics import RubricConfig, load_rubrics_config
from max_news_bot.images import FalImageGenerator, FallbackImageGenerator
from max_news_bot.rubrics.digest import DigestPipeline
from max_news_bot.rubrics.generative import GenerativePipeline
from max_news_bot.tests.conftest import make_factory_config
from max_news_bot.tests.mocks import MockFetcher
from max_news_bot.utils.db import Database
from max_news_bot.weather.pipeline import WeatherPipeline
from max_news_bot.writers.completion import LlmCompletionClient
from max_news_bot.writers.fallback import FallbackCompletion

NOW = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


class DummyMaxClient:
    """Stands in for MaxClient: constructed, never called in these tests."""

    async def send_message(self, chat_id: int, text: str, **kwargs) -> str:
        return "mid.1"

    async def get_me(self) -> dict:
        return {"name": "dummy"}

    async def patch_commands(self, commands) -> None:
        return None


class FakeLlmServer:
    """Local aiohttp server impersonating one OpenAI-compatible channel.

    Records request payloads; ``status`` switches the reply between a
    minimal valid completion and an error code for the probe scenarios.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def completions(request: web.Request) -> web.Response:
            self.requests.append(await request.json())
            payload = {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 1},
            }
            return web.json_response(payload, status=self.status)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", completions)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        self._runner = runner
        return f"http://127.0.0.1:{runner.addresses[0][1]}/v1"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


def server_base(server: FakeLlmServer) -> str:
    """The base URL the server was started on."""
    assert server._runner is not None
    assert server._runner.addresses, "test server did not bind"
    return f"http://127.0.0.1:{server._runner.addresses[0][1]}/v1"


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
async def llm_channels() -> tuple[FakeLlmServer, FakeLlmServer]:
    """Two local channels (primary and fallback); no external network."""
    primary = FakeLlmServer()
    fallback = FakeLlmServer()
    await primary.start()
    await fallback.start()
    yield primary, fallback
    await primary.stop()
    await fallback.stop()


@pytest.fixture
async def app_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llm_channels
) -> AppConfig:
    """AppConfig over the shipped rubrics.yaml, contour enabled; both LLM
    channels point at the local fakes, so every build probes them instead
    of the real providers."""
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    primary, fallback = llm_channels
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
        factory=make_factory_config(
            enabled=False,
            dry_run=False,
            llm=LlmConfig(
                base_url=server_base(primary),
                model="primary-model",
                timeout=5,
                max_tokens=800,
                temperature=0.7,
                api_key="test-primary-key",
            ),
            llm_fallback=LlmConfig(
                base_url=server_base(fallback),
                model="fallback-model",
                timeout=5,
                max_tokens=800,
                temperature=0.7,
                api_key="test-fallback-key",
            ),
        ),
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
    runtime = await build_rubrics_runtime(
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
    runtime = await build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    assert app_config.rubrics.dry_run is True  # shipped default
    assert runtime.scheduler._dry_run is True

    flipped = await build_rubrics_runtime(
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
    runtime = await build_rubrics_runtime(
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
    runtime = await build_rubrics_runtime(
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
        await build_rubrics_runtime(no_key, db, DummyMaxClient(), None, MockFetcher())


async def test_runtime_without_llm_kinds_needs_no_key(
    app_config: AppConfig, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bank/weather only contour: no completion client is built and no key
    # is required at wiring time
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

    runtime = await build_rubrics_runtime(
        bank_weather_only, db, DummyMaxClient(), None, MockFetcher()
    )

    router = runtime.scheduler._pipeline
    for rubric in bank_weather_only.rubrics.rubrics:
        assert router.pipeline_for(rubric) is not None


# --- dual-channel wiring (plan §2.4/§2.5) ---------------------------------------


async def test_llm_pipelines_get_fallback_completion_wired_with_alerts(
    app_config: AppConfig, db: Database, llm_channels
) -> None:
    primary, fallback = llm_channels
    runtime = await build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    router = runtime.scheduler._pipeline
    generative = router.pipeline_for(
        next(r for r in app_config.rubrics.rubrics if r.kind == "generative")
    )
    digest = router.pipeline_for(
        next(r for r in app_config.rubrics.rubrics if r.kind == "digest")
    )
    completion = generative._llm
    assert isinstance(completion, FallbackCompletion)
    assert isinstance(completion._primary, LlmCompletionClient)
    assert isinstance(completion._fallback, LlmCompletionClient)
    # digest and the bank reserve share the same wrapped completion
    assert digest._llm is completion
    assert completion._primary._base_url == server_base(primary)
    assert completion._fallback._base_url == server_base(fallback)
    # timeout / max_tokens / temperature stay the primary's (plan §2.1)
    assert completion._fallback._timeout == completion._primary._timeout
    assert completion._fallback._max_tokens == completion._primary._max_tokens
    assert completion._fallback._temperature == completion._primary._temperature
    # the alert hook is the contour's own alerter, events go to the db
    assert completion._observer._alerter is runtime.alerter
    assert completion._observer._db is db


async def test_build_probes_each_wired_channel_once(
    app_config: AppConfig, db: Database, llm_channels
) -> None:
    primary, fallback = llm_channels

    await build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    # the construction's only network calls: one minimal probe per channel
    assert len(primary.requests) == 1
    assert primary.requests[0]["max_tokens"] == 1
    assert len(fallback.requests) == 1
    assert fallback.requests[0]["max_tokens"] == 1


@pytest.mark.parametrize("channel", ["primary", "fallback"])
async def test_readiness_config_kind_fails_build(
    app_config: AppConfig, db: Database, llm_channels, channel: str
) -> None:
    primary, fallback = llm_channels
    (primary if channel == "primary" else fallback).status = 401

    with pytest.raises(ValueError, match="readiness probe"):
        await build_rubrics_runtime(
            app_config, db, DummyMaxClient(), None, MockFetcher()
        )


async def test_readiness_infra_kind_warns_and_build_continues(
    app_config: AppConfig,
    db: Database,
    llm_channels,
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary, fallback = llm_channels
    primary.status = 500

    with caplog.at_level(logging.WARNING):
        runtime = await build_rubrics_runtime(
            app_config, db, DummyMaxClient(), None, MockFetcher()
        )

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "llm_probe_degraded" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "channel=primary" in warnings[0].getMessage()
    # the still-alive fallback channel was probed too, and the contour
    # keeps its pipelines: a temporarily down provider does not gate it
    assert len(fallback.requests) == 1
    assert fallback.requests[0]["max_tokens"] == 1
    assert runtime.scheduler._pipeline.pipeline_for(
        next(r for r in app_config.rubrics.rubrics if r.kind == "generative")
    ) is not None


# --- image generator wiring (dual-channel plan §3.2/§3.3) ------------------------


async def test_image_generator_wired_into_pipelines_and_publisher(
    app_config: AppConfig, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    runtime = await build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    router = runtime.scheduler._pipeline
    generative = router.pipeline_for(
        next(r for r in app_config.rubrics.rubrics if r.kind == "generative")
    )
    bank = router.pipeline_for(
        next(r for r in app_config.rubrics.rubrics if r.kind == "bank")
    )
    # fal is active with its key and wrapped into the failover shell
    images = generative._images._generator
    assert isinstance(images, FallbackImageGenerator)
    assert isinstance(images._primary, FalImageGenerator)
    assert images._fallback is None  # yandex_art is a stub (plan §3.1)
    assert images._primary._api_key == "test-fal-key"
    assert (
        images._primary._model == app_config.rubrics.image_generation.fal.model
    )
    assert (
        images._primary._timeout
        == app_config.rubrics.image_generation.timeout_seconds
    )
    # bytes land next to the database, the same dir the publisher reads
    # and the daily backup TTL-prunes (plan §3.3)
    generated_dir = db.path.parent / GENERATED_DIR_NAME
    assert generative._images._directory == generated_dir
    # observability of the shared GeneratedImages helper
    assert generative._images._db is db
    assert generative._images._alerter is runtime.alerter
    # the bank pipeline (and its digest reserve copy) share one generator
    assert bank._images._generator is images
    assert bank._images._directory == generated_dir
    assert runtime.scheduler._publisher._generated_dir == generated_dir


async def test_image_generator_inactive_without_fal_key(
    app_config: AppConfig, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no FAL_KEY: the provider stays unwired; only rubrics with
    # image: generate would need it and those abort earlier in config
    # validation (fail-fast), so wiring None here is the honest default
    monkeypatch.delenv("FAL_KEY", raising=False)
    runtime = await build_rubrics_runtime(
        app_config, db, DummyMaxClient(), None, MockFetcher()
    )

    generative = runtime.scheduler._pipeline.pipeline_for(
        next(r for r in app_config.rubrics.rubrics if r.kind == "generative")
    )
    assert generative._images._generator is None
    assert generative._images._directory == db.path.parent / GENERATED_DIR_NAME


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
