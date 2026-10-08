from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from max_news_bot.max_api.client import MaxClient
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

_MAX_ERROR_LEN = 300
_RETRY_DELAY_SECONDS = 5.0
_UPDATE_TYPES = ["message_created", "bot_started"]


async def polling_loop(
    client: MaxClient,
    handle_update: Callable[[dict], Awaitable[None]],
    db: Database,
) -> None:
    """MAX long-poll loop; replaces aiogram's ``dispatcher.start_polling``.

    The marker advances only when the API returns a new one — None (empty
    poll) means "cursor not moved", so the previous marker is reused. A
    crashing handler never kills the loop: the exception is logged and
    recorded in the db exactly like the source ``bot/error_handler.py``
    did (event "error", details "unhandled_error=..."). Network failures on
    get_updates itself are retried after a short sleep. Cancellation
    (asyncio.CancelledError) is never swallowed.
    """
    marker: int | None = None
    while True:
        try:
            updates, next_marker = await client.get_updates(
                marker, timeout=30, types=_UPDATE_TYPES
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
            logger.error("polling_get_updates_failed error=%s", error)
            await asyncio.sleep(_RETRY_DELAY_SECONDS)
            continue

        if next_marker is not None:
            marker = next_marker

        for update in updates:
            try:
                await handle_update(update)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
                logger.error("unhandled_error error=%s", error, exc_info=exc)
                try:
                    await db.log_event("error", f"unhandled_error={error}")
                except Exception as db_exc:
                    logger.error("error_event_write_failed error=%s", db_exc)
