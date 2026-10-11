"""Tests of the weather rubric pipeline adapter (plan section 4).

The adapter is glue: a snapshot from the provider becomes a rendered post
that passes the weather validators; provider failures keep the factory
error taxonomy so the scheduler can retry infra and skip content; there
is never a bank fallback for weather.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from max_news_bot.config.rubrics import (
    RubricConfig,
    RubricsConfig,
    TargetConfig,
    WeatherPoint,
)
from max_news_bot.weather.base import (
    WeatherDataError,
    WeatherInfraError,
    WeatherProviderError,
    WeatherSnapshot,
)
from max_news_bot.weather.pipeline import WeatherPipeline

from max_news_bot.tests.weather_builders import full_snapshot

NOW = datetime(2026, 10, 10, 4, 30, tzinfo=timezone.utc)


def make_config(*rubrics: RubricConfig) -> RubricsConfig:
    return RubricsConfig(
        enabled=True,
        timezone="Europe/Moscow",
        persona="persona",
        image_style="style",
        targets={"main": TargetConfig(name="main", channel_id=-1001234567890)},
        daily_cap=6,
        weather_points=(
            WeatherPoint(name="Санкт-Петербург", latitude=59.9386, longitude=30.3141),
        ),
        rubrics=rubrics,
    )


def make_rubric(**overrides) -> RubricConfig:
    defaults: dict = dict(
        id="weather",
        enabled=True,
        time="07:05",
        days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
        grace_minutes=30,
        jitter_minutes=2,
        kind="weather",
        target="main",
        source=None,
        topics_by_weekday={},
        prompt_overlay=None,
        min_chars=200,
        max_chars=4000,
        image=None,
        history_n=3,
        similarity_threshold=0.7,
        validators=("length", "plausibility", "numbers_match"),
        on_llm_failure="skip",
    )
    defaults.update(overrides)
    return RubricConfig(**defaults)


class FakeProvider:
    def __init__(
        self,
        snapshot: WeatherSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    async def fetch(self) -> WeatherSnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot


class FakeRun:
    id = 1
    slot_date = "2026-10-10"
    attempts = 1


async def test_generated_post_from_snapshot() -> None:
    rubric = make_rubric()
    provider = FakeProvider(snapshot=full_snapshot())
    pipeline = WeatherPipeline(make_config(rubric), provider)

    outcome = await pipeline.generate(rubric, FakeRun(), now=NOW)

    assert outcome.status == "generated"
    assert outcome.text is not None
    assert "Погода" in outcome.text
    assert "Санкт-Петербург" in outcome.text
    assert outcome.image_ref is None  # weather never carries a postcard
    assert provider.calls == 1


async def test_infra_failure_maps_to_retryable_kind() -> None:
    rubric = make_rubric()
    pipeline = WeatherPipeline(
        make_config(rubric),
        FakeProvider(error=WeatherInfraError("open-meteo timeout")),
    )

    outcome = await pipeline.generate(rubric, FakeRun(), now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "infra"


async def test_data_failure_maps_to_content_kind() -> None:
    rubric = make_rubric()
    pipeline = WeatherPipeline(
        make_config(rubric),
        FakeProvider(error=WeatherDataError("no primary point in payload")),
    )

    outcome = await pipeline.generate(rubric, FakeRun(), now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "content"


async def test_both_providers_failed_maps_to_infra() -> None:
    # the two-tier chain raises the base class when both tiers are down
    rubric = make_rubric()
    pipeline = WeatherPipeline(
        make_config(rubric),
        FakeProvider(error=WeatherProviderError("both weather providers failed")),
    )

    outcome = await pipeline.generate(rubric, FakeRun(), now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "infra"


async def test_length_validator_rejects_huge_min_chars() -> None:
    rubric = make_rubric(min_chars=5000)  # the real post is ~500 chars
    pipeline = WeatherPipeline(make_config(rubric), FakeProvider(snapshot=full_snapshot()))

    outcome = await pipeline.generate(rubric, FakeRun(), now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "content"
    assert outcome.error is not None and "too short" in outcome.error


async def test_fallback_post_never_comes_from_the_bank() -> None:
    rubric = make_rubric()
    pipeline = WeatherPipeline(make_config(rubric), FakeProvider(snapshot=full_snapshot()))

    outcome = await pipeline.fallback_post(rubric, FakeRun(), now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "config"
    assert "no bank fallback" in (outcome.error or "")


def test_unknown_validator_name_fails_startup() -> None:
    rubric = make_rubric(validators=("length", "plausibility", "uv_index"))

    with pytest.raises(ValueError, match=r"unknown validators \['uv_index'\]"):
        WeatherPipeline(make_config(rubric), FakeProvider())
