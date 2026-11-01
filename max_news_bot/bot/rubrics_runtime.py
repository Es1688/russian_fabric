"""Runtime wiring of the rubrics contour — the only publishing contour.

``main.py`` starts this composition root unconditionally (the legacy
digest/factory loops are gone): everything the process needs at startup is
built here:

- repositories over the shared database file;
- one pipeline per enabled rubric (dispatched by the router below):
  generative / bank / digest / weather;
- the image generator behind ``image: generate`` rubrics (dual-channel
  plan §3.2): fal.ai active iff FAL_KEY is set, bytes to
  ``data/generated`` (dual-channel plan §3.3);
- the slot scheduler with the slot publisher, alerter and the contour's
  dry-run flag (``RUBRICS_DRY_RUN``, previews to admins);
- the admin levers (``/factory`` rubric arguments), the daily summary
  service and the daily backup service.

Construction makes exactly one network call: the readiness probe of the
wired LLM channels (dual-channel plan §2.1) — a config-kind failure
(broken key/balance/model) fails the launch, an infra-kind one (timeout,
network, 5xx) only logs a warning and the startup continues. Everything
else stays offline; config validation has already failed fast on a broken
yaml or a missing LLM key.
"""

from __future__ import annotations

import os
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
from max_news_bot.images import FalImageGenerator, FallbackImageGenerator
from max_news_bot.images.base import ImageGenerator
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
from max_news_bot.writers.fallback import FallbackCompletion
from max_news_bot.writers.readiness import probe_llm_channels

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

# Generated slot images (``image: generate``, dual-channel plan §3.3) land
# next to the database; the daily backup service TTL-prunes the same
# directory (its default matches this name).
GENERATED_DIR_NAME = "generated"


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


async def _build_completion(
    config: AppConfig,
    http_session: aiohttp.ClientSession | None,
    outbound_session: aiohttp.ClientSession | None,
    db: Database,
    alerter: MaxAlerter,
) -> LlmCompletion | None:
    """Primary completion client wrapped into the per-request failover

    when the fallback channel is configured (dual-channel plan §2.3);
    both wired channels are readiness-probed here (§2.1) — the single
    network call of the contour's construction, before the first slot.
    The primary (OpenRouter) egresses over the socks5h proxy session when
    one is configured; the fallback (Yandex) stays direct (ADR-005)."""
    llm = config.rubrics.llm.primary
    if not llm.api_key:
        return None
    primary = LlmCompletionClient(
        base_url=llm.base_url,
        api_key=llm.api_key,
        model=llm.model,
        timeout=llm.timeout,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
        disable_reasoning=llm.disable_reasoning,
        session=outbound_session or http_session,
    )
    fallback_llm = config.rubrics.llm.fallback
    fallback = (
        None
        if fallback_llm is None
        else LlmCompletionClient(
            base_url=fallback_llm.base_url,
            api_key=fallback_llm.api_key,
            model=fallback_llm.model,
            # timeout / max_tokens / temperature are shared with the
            # primary channel (dual-channel plan §2.1)
            timeout=llm.timeout,
            max_tokens=llm.max_tokens,
            temperature=llm.temperature,
            disable_reasoning=fallback_llm.disable_reasoning,
            session=http_session,
        )
    )
    await probe_llm_channels(
        config.rubrics.llm, outbound_session or http_session, http_session
    )
    if fallback is None:
        # a single channel is valid only outside LLM contours
        return primary
    return FallbackCompletion(primary, fallback, db=db, alerter=alerter)


def _build_image_generator(
    rubrics: RubricsConfig,
    http_session: aiohttp.ClientSession | None,
    outbound_session: aiohttp.ClientSession | None,
) -> ImageGenerator | None:
    """fal.ai primary wrapped into the failover shell (dual-channel plan
    §3.2): the provider is active iff its key is configured. Construction
    stays offline, so this keeps the "one network call" invariant; the
    fail-fast for ``image: generate`` rubrics without a key already ran in
    config validation. fal.ai egresses over the socks5h proxy session when
    one is configured (ADR-005)."""
    api_key = os.getenv("FAL_KEY", "").strip()
    if not api_key:
        return None
    return FallbackImageGenerator(
        FalImageGenerator(
            api_key=api_key,
            model=rubrics.image_generation.fal.model,
            timeout=rubrics.image_generation.timeout_seconds,
            session=outbound_session or http_session,
        )
    )


async def build_rubrics_runtime(
    config: AppConfig,
    db: Database,
    client: MaxClient,
    http_session: aiohttp.ClientSession | None,
    fetcher: Fetcher,
    *,
    outbound_session: aiohttp.ClientSession | None = None,
) -> RubricsRuntime:
    """Wire the whole rubrics contour.

    The old "no network calls during construction" invariant is
    deliberately revised (dual-channel plan §2.4): the readiness probe of
    the wired LLM channels is now the construction's only network call —
    a config-kind probe failure (broken key/balance/model) fails the
    launch, an infra-kind one logs a warning and the startup continues.
    """
    rubrics: RubricsConfig = config.rubrics
    runs = SlotRunRepository(db.path)
    states = RubricStateRepository(db.path)
    bank = ContentBankRepository(db.path)
    filter_log = FilterLogRepository(db.path)
    digest_items = DigestItemsRepository(db.path)
    alerter = MaxAlerter(client, config.admin_ids)
    completion = await _build_completion(
        config, http_session, outbound_session, db, alerter
    )
    generated_dir = db.path.parent / GENERATED_DIR_NAME
    images = _build_image_generator(rubrics, http_session, outbound_session)

    enabled = [rubric for rubric in rubrics.rubrics if rubric.enabled]
    needs_llm = any(rubric.kind in LLM_KINDS for rubric in enabled)
    if needs_llm and completion is None:
        # Unreachable behind load_config validation; kept honest anyway.
        raise ValueError(
            "rubrics of LLM kinds require the LLM channel keys at startup"
        )

    router = RubricPipelineRouter()
    weather_provider: FallbackWeatherProvider | None = None
    digest_rubrics: list[RubricConfig] = []
    for rubric in enabled:
        if rubric.kind == "generative":
            router.bind(
                rubric,
                GenerativePipeline(
                    rubrics,
                    completion,
                    bank,
                    runs,
                    filter_log,
                    images=images,
                    generated_dir=generated_dir,
                    db=db,
                    alerter=alerter,
                ),
            )
        elif rubric.kind == "bank":
            router.bind(
                rubric,
                BankPipeline(
                    rubrics,
                    bank,
                    runs,
                    completion,
                    images=images,
                    generated_dir=generated_dir,
                    db=db,
                    alerter=alerter,
                ),
            )
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
        generated_dir=generated_dir,
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
    bank_fallback = BankPipeline(
        rubrics,
        bank,
        runs,
        completion,
        images=images,
        generated_dir=generated_dir,
        db=db,
        alerter=alerter,
    )
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
