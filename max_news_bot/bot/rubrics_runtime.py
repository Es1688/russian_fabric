"""Runtime wiring of the rubrics contour (plan sections 8, 14).

``main.py`` switches the whole process between two publishing contours:
the legacy interval loops and the rubrics slot scheduler. This module is
the composition root of the second one - everything ``RUBRICS_ENABLED``
needs at startup is built here, so ``main.py`` only decides which set of
background tasks to start:

- repositories over the shared database file;
- one pipeline per enabled rubric (dispatched by the router below):
  generative / bank / digest / weather;
- the slot scheduler with the slot publisher, alerter and the contour's
  dry-run flag (``RUBRICS_DRY_RUN``, previews to admins);
- the admin levers (``/factory`` rubric arguments), the daily summary
  service and the daily backup service.

Construction performs no network calls; config validation has already
failed fast on a broken yaml, a contour conflict or a missing LLM key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from max_news_bot.bank.pipeline import BankPipeline
from max_news_bot.bot.daily_summary import DailySummaryService
from max_news_bot.bot.db_backup import DailyBackupService
from max_news_bot.bot.rubrics_admin import RubricsAdmin
from max_news_bot.bot.slot_scheduler import (
    GenerationOutcome,
    RubricPipeline,
    SlotScheduler,
)
from max_news_bot.config.news_sources import NewsSource, load_news_sources
from max_news_bot.config.rubrics import LLM_KINDS, RubricConfig, RubricsConfig
from max_news_bot.publishers.alerter import MaxAlerter
from max_news_bot.publishers.slot import MaxSlotPublisher
from max_news_bot.rubrics.digest import DigestPipeline
from max_news_bot.rubrics.generative import GenerativePipeline
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    DigestItemsRepository,
    FilterLogRepository,
    RubricStateRepository,
    SlotRunRepository,
)
from max_news_bot.weather import (
    FallbackWeatherProvider,
    MetNoProvider,
    OpenMeteoProvider,
    WeatherPipeline,
)
from max_news_bot.writers.completion import LlmCompletion, LlmCompletionClient

if TYPE_CHECKING:
    from max_news_bot.config.loader import AppConfig
    from max_news_bot.max_api.client import MaxClient
    from max_news_bot.parsers.base import Fetcher
    from max_news_bot.utils.db import Database


logger = get_logger(__name__)

# Bank postcards live next to the database (metadata carries the file path
# relative to this directory); a missing card degrades to a text-only post,
# so the directory itself is created lazily by the operator's import.
POSTCARDS_DIR_NAME = "postcards"


class RubricPipelineRouter:
    """Dispatches each rubric to its pipeline (by id, then by kind).

    The id binding is exact; the kind fallback lets a run of a rubric that
    was disabled in the config after a restart (recovery of an old slot)
    still find a pipeline of its kind. An unbound rubric is a wiring bug:
    it fails the slot as a config error instead of crashing the loop.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, RubricPipeline] = {}
        self._by_kind: dict[str, RubricPipeline] = {}

    def bind(self, rubric: RubricConfig, pipeline: RubricPipeline) -> None:
        self._by_id[rubric.id] = pipeline
        self._by_kind.setdefault(rubric.kind, pipeline)

    def pipeline_for(self, rubric: RubricConfig) -> RubricPipeline | None:
        return self._by_id.get(rubric.id) or self._by_kind.get(rubric.kind)

    # --- RubricPipeline protocol ---------------------------------------------

    async def generate(
        self, rubric: RubricConfig, run, *, now
    ) -> GenerationOutcome:
        pipeline = self.pipeline_for(rubric)
        if pipeline is None:
            return self._unbound(rubric)
        return await pipeline.generate(rubric, run, now=now)

    async def fallback_post(
        self, rubric: RubricConfig, run, *, now
    ) -> GenerationOutcome:
        pipeline = self.pipeline_for(rubric)
        if pipeline is None:
            return self._unbound(rubric)
        return await pipeline.fallback_post(rubric, run, now=now)

    @staticmethod
    def _unbound(rubric: RubricConfig) -> GenerationOutcome:
        return GenerationOutcome(
            status="failed",
            error=f"no pipeline bound for rubric {rubric.id} (kind {rubric.kind})",
            error_kind="config",
        )


@dataclass(frozen=True)
class RubricsRuntime:
    """Everything the rubrics contour runs with, built at startup."""

    scheduler: SlotScheduler
    admin: RubricsAdmin
    summary: DailySummaryService
    backup: DailyBackupService
    alerter: MaxAlerter


def _build_completion(
    config: AppConfig, http_session: aiohttp.ClientSession | None
) -> LlmCompletion | None:
    llm = config.factory.llm
    if not llm.api_key:
        return None
    return LlmCompletionClient(
        base_url=llm.base_url,
        api_key=llm.api_key,
        model=llm.model,
        timeout=llm.timeout,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
        session=http_session,
    )


def build_rubrics_runtime(
    config: AppConfig,
    db: Database,
    client: MaxClient,
    http_session: aiohttp.ClientSession | None,
    fetcher: Fetcher,
) -> RubricsRuntime:
    """Wire the whole rubrics contour; no network calls happen here."""
    rubrics: RubricsConfig = config.rubrics
    runs = SlotRunRepository(db.path)
    states = RubricStateRepository(db.path)
    bank = ContentBankRepository(db.path)
    filter_log = FilterLogRepository(db.path)
    digest_items = DigestItemsRepository(db.path)
    alerter = MaxAlerter(client, config.admin_ids)
    completion = _build_completion(config, http_session)

    enabled = [rubric for rubric in rubrics.rubrics if rubric.enabled]
    needs_llm = any(rubric.kind in LLM_KINDS for rubric in enabled)
    if needs_llm and completion is None:
        # Unreachable behind load_config validation; kept honest anyway.
        raise ValueError(
            "rubrics of LLM kinds require LLM_API_KEY at startup"
        )

    router = RubricPipelineRouter()
    weather_provider: FallbackWeatherProvider | None = None
    digest_rubrics: list[RubricConfig] = []
    for rubric in enabled:
        if rubric.kind == "generative":
            router.bind(
                rubric,
                GenerativePipeline(rubrics, completion, bank, runs, filter_log),
            )
        elif rubric.kind == "bank":
            router.bind(rubric, BankPipeline(rubrics, bank, runs, completion))
        elif rubric.kind == "weather":
            if weather_provider is None:
                points = rubrics.weather_points
                weather_provider = FallbackWeatherProvider(
                    OpenMeteoProvider(
                        points, session=http_session, timezone_name=rubrics.timezone
                    ),
                    MetNoProvider(
                        points, session=http_session, timezone_name=rubrics.timezone
                    ),
                )
            router.bind(rubric, WeatherPipeline(rubrics, weather_provider))
        elif rubric.kind == "digest":
            digest_rubrics.append(rubric)

    publisher = MaxSlotPublisher(
        client,
        config.admin_ids,
        bank=bank,
        postcards_dir=db.path.parent / POSTCARDS_DIR_NAME,
    )
    scheduler = SlotScheduler(
        rubrics,
        runs,
        states,
        router,
        publisher,
        alerter,
        dry_run=rubrics.dry_run,
    )

    # Digest pipelines close over the scheduler's empty-source breaker
    # (plan section 11), so they are bound after the scheduler exists.
    bank_fallback = BankPipeline(rubrics, bank, runs, completion)
    for rubric in digest_rubrics:
        router.bind(
            rubric,
            DigestPipeline(
                rubrics,
                _load_digest_sources(rubric),
                fetcher,
                completion,
                filter_log,
                digest_items,
                bank_fallback,
                is_excluded_source=scheduler.sources.is_excluded,
            ),
        )

    admin = RubricsAdmin(rubrics, scheduler, runs, states, db)
    summary = DailySummaryService(
        rubrics, runs, bank, filter_log, db, send=alerter.send_alert
    )
    backup = DailyBackupService(db, tz_name=rubrics.timezone)
    logger.info(
        "rubrics_runtime_built rubrics=%s dry_run=%s kinds=%s",
        len(enabled),
        rubrics.dry_run,
        sorted({rubric.kind for rubric in enabled}),
    )
    return RubricsRuntime(
        scheduler=scheduler,
        admin=admin,
        summary=summary,
        backup=backup,
        alerter=alerter,
    )


def _load_digest_sources(rubric: RubricConfig) -> tuple[NewsSource, ...]:
    """Feeds of a digest rubric: ``source.feeds_file`` resolved against the
    config package directory (the yaml names the file, the deploy may swap
    it with NEWS_SOURCES_PATH when no name is given)."""
    feeds_file = (rubric.source or {}).get("feeds_file")
    if not feeds_file:
        return load_news_sources()
    path = Path(str(feeds_file))
    if not path.is_absolute():
        config_dir = Path(__file__).resolve().parents[1] / "config"
        path = config_dir / path
    return load_news_sources(path)
