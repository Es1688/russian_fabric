from __future__ import annotations

import asyncio

from max_news_bot.core.factory import ContentFactory
from max_news_bot.core.pipeline import Pipeline
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)


async def scheduler_loop(
    pipeline: Pipeline,
    interval_hours: int,
    publish_lock: asyncio.Lock,
    db: Database,
) -> None:
    while True:
        async with publish_lock:
            try:
                await pipeline.run()
            except Exception as exc:
                logger.error("scheduler_error error=%s", exc)
                await db.log_event("error", f"scheduler_error={exc}")
        await asyncio.sleep(interval_hours * 3600)


async def factory_scheduler_loop(
    factory: ContentFactory,
    interval_hours: int,
    factory_lock: asyncio.Lock,
    db: Database,
) -> None:
    """Second independent cycle with its own lock and interval.

    Quiet hours, pause/autopause checks and idle heartbeats live inside
    ContentFactory.run(), so this loop only paces and shields it.
    """
    while True:
        async with factory_lock:
            try:
                await factory.run()
            except Exception as exc:
                logger.error("factory_scheduler_error error=%s", exc)
                await db.log_event("error", f"factory_scheduler_error={exc}")
        await asyncio.sleep(interval_hours * 3600)
