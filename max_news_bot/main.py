from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

import aiohttp

from max_news_bot.bot.daily_summary import daily_summary_loop
from max_news_bot.bot.db_backup import daily_backup_loop
from max_news_bot.bot.handlers import BOT_COMMANDS, create_dispatcher
from max_news_bot.bot.polling import polling_loop
from max_news_bot.bot.rubrics_runtime import build_rubrics_runtime
from max_news_bot.bot.slot_scheduler import slot_scheduler_loop
from max_news_bot.config.loader import load_config
from max_news_bot.max_api.client import MaxClient
from max_news_bot.parsers.composite import CompositeFetcher
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger, setup_logging
from max_news_bot.utils.proxy import egress_connector_from_env


logger = get_logger(__name__)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_level)
    db = Database(config.data_path)
    await db.initialize()

    # One shared direct ClientSession per process: the MAX client, RSS
    # fetching, weather and the Yandex fallback channel; both sessions are
    # closed by the outermost finally, on startup and runtime exits alike.
    http_session = aiohttp.ClientSession()

    # Foreign endpoints (OpenRouter, fal.ai) egress through the host-local
    # socks5h proxy when EGRESS_PROXY_URL is set; without it everything
    # stays direct (ADR-005).
    egress_connector = egress_connector_from_env()
    outbound_session: aiohttp.ClientSession | None = (
        aiohttp.ClientSession(connector=egress_connector)
        if egress_connector is not None
        else None
    )

    try:
        client = MaxClient(config.bot_token, session=http_session)
        fetcher = CompositeFetcher(session=http_session)

        started_at = datetime.now(timezone.utc)

        # The rubrics contour is the only publishing contour: its
        # construction probes the wired LLM channels (the single network
        # call) and wires the pipelines, the slot scheduler and the admin
        # levers.
        runtime = await build_rubrics_runtime(
            config,
            db,
            client,
            http_session,
            fetcher,
            outbound_session=outbound_session,
        )

        handle_update = create_dispatcher(
            config,
            db,
            started_at,
            client=client,
            rubrics_admin=runtime.admin,
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

        polling_task = asyncio.create_task(polling_loop(client, handle_update, db))

        try:
            # polling replaces the source dispatcher.start_polling: it is
            # the main awaited task and everything else is cancelled when
            # it ends
            await polling_task
        finally:
            for task in [polling_task, *background_tasks]:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    finally:
        # closed last, after the tasks are cancelled: a startup failure
        # must not leak "Unclosed client session" noise into crash-loop
        # logs
        if outbound_session is not None:
            await outbound_session.close()
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
