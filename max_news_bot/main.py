from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

import aiohttp

from max_news_bot.bot.daily_summary import daily_summary_loop
from max_news_bot.bot.db_backup import daily_backup_loop
from max_news_bot.bot.handlers import BOT_COMMANDS, create_dispatcher
from max_news_bot.bot.polling import polling_loop
from max_news_bot.bot.rubrics_runtime import RubricsRuntime, build_rubrics_runtime
from max_news_bot.bot.scheduler import factory_scheduler_loop, scheduler_loop
from max_news_bot.bot.slot_scheduler import slot_scheduler_loop
from max_news_bot.config.loader import load_config
from max_news_bot.core.factory import ContentFactory
from max_news_bot.core.pipeline import Pipeline
from max_news_bot.max_api.client import MaxClient
from max_news_bot.parsers.composite import CompositeFetcher
from max_news_bot.publishers.alerter import MaxAlerter
from max_news_bot.publishers.max import MaxDigestPublisher
from max_news_bot.publishers.post import MaxPostPublisher
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger, setup_logging
from max_news_bot.writers.base import Writer
from max_news_bot.writers.fallback import FallbackWriter
from max_news_bot.writers.llm import LlmWriter
from max_news_bot.writers.readiness import probe_llm_channels


logger = get_logger(__name__)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_level)
    db = Database(config.data_path)
    await db.initialize()

    # One shared ClientSession per process: the MAX client, RSS fetching
    # (both cycles) and the LLM writer; closed in the finally block below.
    http_session = aiohttp.ClientSession()

    client = MaxClient(config.bot_token, session=http_session)

    publisher = MaxDigestPublisher(client, config.channel_id)
    fetcher = CompositeFetcher(session=http_session)
    pipeline = Pipeline(config, fetcher, publisher, db)

    publish_lock = asyncio.Lock()
    started_at = datetime.now(timezone.utc)

    factory: ContentFactory | None = None
    factory_lock: asyncio.Lock | None = None
    if config.factory.enabled:
        post_publisher = MaxPostPublisher(client, config.channel_id, config.admin_ids)
        alerter = MaxAlerter(client, config.admin_ids)
        llm = config.factory.llm
        writer: Writer = LlmWriter(
            base_url=llm.base_url,
            api_key=llm.api_key,
            model=llm.model,
            timeout=llm.timeout,
            max_tokens=llm.max_tokens,
            temperature=llm.temperature,
            prompt=config.factory.prompt,
            disable_reasoning=llm.disable_reasoning,
            session=http_session,
        )
        fallback_llm = config.factory.llm_fallback
        if fallback_llm is not None:
            # Per-request failover (dual-channel plan §2.3/§2.4): timeout /
            # max_tokens / temperature stay the primary's, base_url / model
            # / key / reasoning flag come from the fallback section; the
            # alert hook reports a config error of either channel even when
            # a successful post of the other one masks it.
            writer = FallbackWriter(
                writer,
                LlmWriter(
                    base_url=fallback_llm.base_url,
                    api_key=fallback_llm.api_key,
                    model=fallback_llm.model,
                    timeout=llm.timeout,
                    max_tokens=llm.max_tokens,
                    temperature=llm.temperature,
                    prompt=config.factory.prompt,
                    disable_reasoning=fallback_llm.disable_reasoning,
                    session=http_session,
                ),
                db=db,
                alerter=alerter,
            )
        factory = ContentFactory(config, fetcher, writer, post_publisher, alerter, db)
        factory_lock = asyncio.Lock()
        # Readiness probe of both LLM channels before any background task
        # starts (dual-channel plan §2.1): a config-kind failure (broken
        # key/balance/model) aborts the launch, an infra-kind one only
        # warns and the startup continues.
        await probe_llm_channels(config.factory, http_session)

    # RUBRICS_ENABLED contour (plan sections 8 and 14): built only when
    # enabled; config validation guarantees the legacy factory loop is off
    # in that case, so no second publishing contour can appear here.
    runtime: RubricsRuntime | None = None
    if config.rubrics.enabled:
        # the construction's only network call is the readiness probe of
        # the wired LLM channels (see build_rubrics_runtime docstring)
        runtime = await build_rubrics_runtime(
            config, db, client, http_session, fetcher
        )

    handle_update = create_dispatcher(
        config,
        pipeline,
        db,
        publish_lock,
        started_at,
        factory=factory,
        factory_lock=factory_lock,
        client=client,
        rubrics_admin=runtime.admin if runtime is not None else None,
    )

    # Best-effort startup checks: neither a failing /me nor a failing
    # command registration may prevent the bot from running.
    try:
        me = await client.get_me()
        logger.info("authorized as bot name=%s", me.get("name"))
    except Exception as exc:
        logger.warning("startup_get_me_failed error=%s", exc)
    try:
        await client.patch_commands(BOT_COMMANDS)
    except Exception as exc:
        logger.warning("startup_patch_commands_failed error=%s", exc)

    # Contour switch (plan sections 8 and 14): exactly one publishing
    # contour runs. RUBRICS_ENABLED=true starts the slot scheduler with
    # its summary and backup tasks, and both legacy loops stay down (the
    # config validation has already failed the launch on a conflict);
    # with the flag off the process behaves exactly as before.
    background_tasks: list[asyncio.Task] = []
    if runtime is not None:
        background_tasks = [
            asyncio.create_task(slot_scheduler_loop(runtime.scheduler, db)),
            asyncio.create_task(daily_summary_loop(runtime.summary, db)),
            asyncio.create_task(
                daily_backup_loop(runtime.backup, db, alerter=runtime.alerter)
            ),
        ]
        logger.info(
            "rubrics_contour_started dry_run=%s rubrics=%s summary_time=%s",
            config.rubrics.dry_run,
            len([r for r in config.rubrics.rubrics if r.enabled]),
            config.rubrics.summary_time,
        )
    else:
        background_tasks.append(
            asyncio.create_task(
                scheduler_loop(
                    pipeline, config.settings.post_interval_hours, publish_lock, db
                )
            )
        )
        if factory is not None and factory_lock is not None:
            background_tasks.append(
                asyncio.create_task(
                    factory_scheduler_loop(
                        factory, config.factory.interval_hours, factory_lock, db
                    )
                )
            )
        logger.info(
            "legacy_contour_started interval_hours=%s",
            config.settings.post_interval_hours,
        )

    polling_task = asyncio.create_task(polling_loop(client, handle_update, db))

    try:
        # polling replaces the source dispatcher.start_polling: it is the
        # main awaited task and everything else is cancelled when it ends
        await polling_task
    finally:
        for task in [polling_task, *background_tasks]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
