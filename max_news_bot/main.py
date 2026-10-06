from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

import aiohttp

from max_news_bot.bot.handlers import BOT_COMMANDS, create_dispatcher
from max_news_bot.bot.polling import polling_loop
from max_news_bot.bot.scheduler import factory_scheduler_loop, scheduler_loop
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
from max_news_bot.writers.llm import LlmWriter


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
        llm = config.factory.llm
        writer = LlmWriter(
            base_url=llm.base_url,
            api_key=llm.api_key,
            model=llm.model,
            timeout=llm.timeout,
            max_tokens=llm.max_tokens,
            temperature=llm.temperature,
            prompt=config.factory.prompt,
            session=http_session,
        )
        post_publisher = MaxPostPublisher(client, config.channel_id, config.admin_ids)
        alerter = MaxAlerter(client, config.admin_ids)
        factory = ContentFactory(config, fetcher, writer, post_publisher, alerter, db)
        factory_lock = asyncio.Lock()

    handle_update = create_dispatcher(
        config,
        pipeline,
        db,
        publish_lock,
        started_at,
        factory=factory,
        factory_lock=factory_lock,
        client=client,
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

    scheduler_task = asyncio.create_task(
        scheduler_loop(pipeline, config.settings.post_interval_hours, publish_lock, db)
    )
    factory_task = None
    if factory is not None and factory_lock is not None:
        factory_task = asyncio.create_task(
            factory_scheduler_loop(
                factory, config.factory.interval_hours, factory_lock, db
            )
        )

    polling_task = asyncio.create_task(polling_loop(client, handle_update, db))

    try:
        # polling replaces the source dispatcher.start_polling: it is the
        # main awaited task and everything else is cancelled when it ends
        await polling_task
    finally:
        for task in (polling_task, scheduler_task, factory_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
