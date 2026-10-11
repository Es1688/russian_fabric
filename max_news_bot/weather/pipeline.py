"""Weather rubric pipeline adapter (plan section 4).

Glues the weather domain together into the ``RubricPipeline`` protocol of
``bot/slot_scheduler.py``: provider (Open-Meteo batch primary, met.no
fallback) -> template -> validator. No LLM is involved anywhere in the
chain - the post is rendered from provider data and re-checked number by
number, so a weather slot never spends the LLM budget.

Failure semantics of plan section 4: a failed primary point fails the
whole attempt (providers raise instead of returning a snapshot without
Saint Petersburg), a failed regional point just drops out of the post,
and weather never publishes a reserve post - a hopeless slot is skipped
after the scheduler's retries, with an alert.
"""

from __future__ import annotations

import asyncio

from max_news_bot.bot.slot_scheduler import GenerationOutcome
from max_news_bot.config.rubrics import RubricConfig, RubricsConfig
from max_news_bot.weather.base import (
    WeatherDataError,
    WeatherInfraError,
    WeatherProvider,
    WeatherProviderError,
)
from max_news_bot.weather.template import render_weather_post
from max_news_bot.weather.validator import validate_weather_post
from max_news_bot.writers.validator import validate_post_text


# Validator names understood by this pipeline (rubric ``validators`` list).
KNOWN_VALIDATORS = ("length", "plausibility", "numbers_match")

_ERROR_MAX_CHARS = 300


class WeatherPipeline:
    """``kind: weather`` rubric: fetch, render, validate - no LLM calls."""

    def __init__(self, config: RubricsConfig, provider: WeatherProvider) -> None:
        self._provider = provider
        self._tz_name = config.timezone
        # A config typo in a validator name must abort the launch (plan
        # section 8), not surface as a broken 07:05 post.
        unknown = sorted(
            {
                name
                for rubric in config.rubrics
                if rubric.enabled and rubric.kind == "weather"
                for name in rubric.validators
                if name not in KNOWN_VALIDATORS
            }
        )
        if unknown:
            raise ValueError(
                f"weather rubrics: unknown validators {unknown}; "
                f"allowed: {list(KNOWN_VALIDATORS)}"
            )

    # --- RubricPipeline protocol -------------------------------------------

    async def generate(
        self, rubric: RubricConfig, run, *, now
    ) -> GenerationOutcome:
        try:
            snapshot = await self._provider.fetch()
        except asyncio.CancelledError:
            raise
        except WeatherInfraError as exc:
            return self._failed(str(exc), kind="infra")
        except WeatherDataError as exc:
            return self._failed(str(exc), kind="content")
        except WeatherProviderError as exc:
            # both tiers of the fallback chain failed with mixed kinds;
            # a retry re-runs the whole chain, so treat it as transport
            return self._failed(str(exc), kind="infra")

        text = render_weather_post(snapshot, tz_name=self._tz_name)
        if "length" in rubric.validators:
            ok, reason = validate_post_text(
                text, max_chars=rubric.max_chars, min_chars=rubric.min_chars
            )
            if not ok:
                return self._failed(f"validator: {reason or 'length'}", kind="content")
        if "plausibility" in rubric.validators or "numbers_match" in rubric.validators:
            ok, reason = validate_weather_post(
                text, snapshot, max_chars=rubric.max_chars, tz_name=self._tz_name
            )
            if not ok:
                return self._failed(f"validator: {reason or 'weather'}", kind="content")
        return GenerationOutcome(status="generated", text=text)

    async def fallback_post(
        self, rubric: RubricConfig, run, *, now
    ) -> GenerationOutcome:
        # Plan section 4: no reserve post for weather; the scheduler only
        # reaches here for a misconfigured on_llm_failure=bank rubric.
        return self._failed(
            "weather rubrics have no bank fallback (plan section 4)",
            kind="config",
        )

    @staticmethod
    def _failed(error: str, *, kind: str) -> GenerationOutcome:
        return GenerationOutcome(
            status="failed", error=error[:_ERROR_MAX_CHARS], error_kind=kind
        )
