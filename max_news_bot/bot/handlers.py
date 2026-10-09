from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone

from max_news_bot.config.loader import AppConfig
from max_news_bot.core.factory import ContentFactory, FactoryResult
from max_news_bot.core.pipeline import Pipeline
from max_news_bot.max_api.client import MaxClient
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

_RETRACT_LOOKUP_LIMIT = 200
_RECENT_POSTS_LIMIT = 10
_LINE_TAIL_LIMIT = 80
_MAX_ERROR_LEN = 300
_USER_ERROR_TEXT = "Internal error occurred. Check /status later for details."

# /start doubles as the admin opt-in: MAX forbids a bot from DMing a user
# until that user has started it (see publishers/alerter.py).
_START_TEXT = (
    "Привет! Это бот, который собирает новости и публикует их в канал.\n"
    "Если вы админ — теперь бот может писать вам в личные сообщения "
    "(превью dry-run и алерты приходят сюда).\n"
    "Команды: /status, /sources, /stats, /news, /factory, /posts."
)

# Registered via PATCH /me/commands at startup (best-effort).
BOT_COMMANDS: list[dict] = [
    {"name": "start", "description": "Приветствие и подписка на личные сообщения"},
    {"name": "chatid", "description": "Показать chat_id этого чата"},
    {"name": "status", "description": "Состояние бота"},
    {"name": "sources", "description": "Список RSS-источников"},
    {"name": "stats", "description": "Статистика по дням"},
    {"name": "news", "description": "Запустить сбор новостей (админ)"},
    {"name": "factory", "description": "Управление фабрикой контента (админ)"},
    {"name": "posts", "description": "Последние посты (админ)"},
]


def create_dispatcher(
    config: AppConfig,
    pipeline: Pipeline,
    db: Database,
    publish_lock: asyncio.Lock,
    started_at: datetime,
    *,
    factory: ContentFactory | None = None,
    factory_lock: asyncio.Lock | None = None,
    client: MaxClient | None = None,
):
    """Wire all command handlers into one ``handle_update(update: dict)``.

    Plain MAX update dicts in, replies out via MaxClient.send_message —
    no framework, so tests call the returned coroutine directly.
    """
    # MAX sends every message with a format; replies go as html, so any
    # dynamic value (names, titles, errors) is escaped at embed time.
    async def reply(chat_id: int, text: str) -> None:
        if client is None:
            logger.warning("reply_skipped chat_id=%s (no client)", chat_id)
            return
        await client.send_message(chat_id, text, fmt="html")

    def is_admin(user_id: int | None) -> bool:
        return user_id is not None and user_id in config.admin_ids

    async def dispatch(
        chat_id: int, user_id: int | None, command: str, args: list[str]
    ) -> None:
        if command == "start":
            await reply(chat_id, _START_TEXT)
            return

        if command == "chatid":
            # setup helper: tells the admin the chat_id to put in MAX_CHANNEL_ID
            await reply(chat_id, f"chat_id: {chat_id}")
            return

        if command == "status":
            uptime = datetime.now(timezone.utc) - started_at
            last_run = await db.get_last_event("run")
            last_error = await db.get_last_event("error")
            await reply(
                chat_id,
                "\n".join(
                    [
                        f"Uptime: {format_timedelta(uptime)}",
                        f"Last run: {format_event(last_run)}",
                        f"Last error: {format_event(last_error)}",
                    ]
                ),
            )
            return

        if command == "sources":
            lines = []
            for source in config.sources:
                status = "enabled" if source.enabled else "disabled"
                category = (
                    f" ({html.escape(source.category)})" if source.category else ""
                )
                lines.append(f"- {html.escape(source.name)}{category}: {status}")
            await reply(
                chat_id, "\n".join(lines) if lines else "No sources configured."
            )
            return

        if command == "stats":
            stats = await db.get_daily_stats()
            if not stats:
                await reply(chat_id, "No stats yet.")
                return
            lines = ["Daily stats:"]
            for item in stats:
                lines.append(
                    f"{html.escape(str(item['day']))}: "
                    f"publish={item['publishes']} errors={item['errors']}"
                )
            await reply(chat_id, "\n".join(lines))
            return

        if command == "news":
            if not is_admin(user_id):
                await reply(chat_id, "Access denied.")
                return
            async with publish_lock:
                await reply(chat_id, "Running pipeline...")
                result = await pipeline.run()
            await reply(
                chat_id,
                f"Pipeline done: collected={result.collected} "
                f"published={result.published}",
            )
            return

        if command == "factory":
            if not is_admin(user_id):
                await reply(chat_id, "Access denied.")
                return
            await dispatch_factory(chat_id, args)
            return

        if command == "posts":
            if not is_admin(user_id):
                await reply(chat_id, "Access denied.")
                return
            posts = await db.get_recent_posts(_RECENT_POSTS_LIMIT)
            if not posts:
                await reply(chat_id, "No posts yet.")
                return
            lines = ["Recent posts:"]
            for post in posts:
                tail = post.error or post.title or ""
                tail = " ".join(tail.split())
                lines.append(
                    f"#{post.id} [{html.escape(post.status)}] "
                    f"attempts={post.attempts} "
                    f"{html.escape(post.source)}: "
                    f"{html.escape(tail[:_LINE_TAIL_LIMIT])}"
                )
            await reply(chat_id, "\n".join(lines))
            return

        # unknown command: silent, mirroring the aiogram router fallthrough

    async def dispatch_factory(chat_id: int, args: list[str]) -> None:
        action = args[0].lower() if args else "run"

        if action == "run":
            if factory is None or factory_lock is None:
                await reply(
                    chat_id,
                    "Factory is disabled "
                    "(check FACTORY_ENABLED / factory.enabled).",
                )
                return
            async with factory_lock:
                await reply(chat_id, "Running factory cycle...")
                result = await factory.run()
            await reply(chat_id, format_factory_result(result))
            return

        if action == "pause":
            await db.set_state_flag("factory", False)
            await db.log_event("factory_pause", "manual")
            await reply(
                chat_id, "Factory paused. Use /factory resume to continue."
            )
            return

        if action == "resume":
            await db.set_state_flag("factory", True)
            await db.set_state_flag("factory_autopause", True)
            await db.log_event("factory_resume", "manual")
            await reply(
                chat_id,
                "Factory resumed (manual pause and auto-pause cleared).",
            )
            return

        if action == "status":
            await reply(chat_id, await factory_status(config, db))
            return

        if action == "retract":
            await retract_post(client, chat_id, args, config, db)
            return

        await reply(
            chat_id, "Usage: /factory [run|pause|resume|status|retract <post_id>]"
        )

    async def handle_update(update: dict) -> None:
        chat_id, user_id, update_type, text = _parse_update(update)
        if chat_id is None:
            # malformed/non-message update: nothing to answer
            logger.debug(
                "update_ignored update_type=%s payload=%s",
                update_type or "unknown",
                update,
            )
            return
        try:
            if update_type == "bot_started":
                logger.info(
                    "bot_started chat_id=%s user_id=%s", chat_id, user_id
                )
                await reply(chat_id, _START_TEXT)
                return
            command, args = _split_command(text)
            if not command:
                return
            await dispatch(chat_id, user_id, command, args)
        except Exception as exc:
            # port of the source bot/error_handler.py UX: log, record the
            # db error event, try to tell the user; never kill the poller
            await _report_update_error(client, db, chat_id, exc)

    return handle_update


async def _report_update_error(
    client: MaxClient | None, db: Database, chat_id: int, exc: Exception
) -> None:
    error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
    logger.error("unhandled_error error=%s", error, exc_info=exc)
    try:
        await db.log_event("error", f"unhandled_error={error}")
    except Exception as db_exc:
        logger.error("error_event_write_failed error=%s", db_exc)
    try:
        if client is None:
            logger.warning("error_reply_skipped chat_id=%s (no client)", chat_id)
            return
        await client.send_message(chat_id, _USER_ERROR_TEXT, fmt="html")
    except Exception as answer_exc:
        logger.error("error_reply_failed error=%s", answer_exc)


def _parse_update(
    update: dict,
) -> tuple[int | None, int | None, str, str | None]:
    """Defensively dig (chat_id, sender_user_id, update_type, text) out of a
    MAX update dict; missing/broken levels degrade to None instead of raising.

    Real MAX shapes (no TamTam-era "update" wrapper — fields sit directly
    on the update object):
    message_created: {update_type, timestamp, message: {sender, recipient,
        body: {text}}, user_locale}
    bot_started: {update_type, timestamp, chat_id, user: {user_id}}
    """
    if not isinstance(update, dict):
        return None, None, "", None
    update_type = update.get("update_type")
    if not isinstance(update_type, str):
        update_type = ""
    message = update.get("message")
    if not isinstance(message, dict):
        message = {}

    recipient = message.get("recipient")
    chat_id = (
        _int_or_none(recipient.get("chat_id"))
        if isinstance(recipient, dict)
        else None
    )
    if chat_id is None:
        # bot_started and other chat-level events carry the chat id directly
        chat_id = _int_or_none(update.get("chat_id"))

    sender = message.get("sender")
    user_id = (
        _int_or_none(sender.get("user_id")) if isinstance(sender, dict) else None
    )
    if user_id is None:
        # bot_started/bot_added carry the acting user directly
        user = update.get("user")
        user_id = _int_or_none(user.get("user_id")) if isinstance(user, dict) else None

    body = message.get("body")
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        text = None

    return chat_id, user_id, update_type, text


def _split_command(text: str | None) -> tuple[str, list[str]]:
    """"/factory retract 5" -> ("factory", ["retract", "5"]); plain text or
    empty input -> ("", [])."""
    if not isinstance(text, str):
        return "", []
    parts = text.split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    return parts[0].lstrip("/").lower(), parts[1:]


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def factory_status(config: AppConfig, db: Database) -> str:
    paused = not await db.get_state_flag("factory")
    autopaused = not await db.get_state_flag("factory_autopause")
    last_run = await db.get_last_event("factory_run")
    lines = [
        f"Factory: {'enabled' if config.factory.enabled else 'disabled'} (config)",
        f"Dry-run: {'on' if config.factory.dry_run else 'off'}",
        f"Interval: {config.factory.interval_hours}h",
        f"Paused (manual): {'yes' if paused else 'no'}",
        f"Auto-paused: {'yes' if autopaused else 'no'}",
        f"Last cycle: {format_event(last_run)}",
    ]
    if autopaused:
        last_alert = await db.get_last_event("factory_alert")
        reason = (last_alert or {}).get("details") or "unknown"
        lines.append(f"Autopause reason: {html.escape(_short(reason))}")
    return "\n".join(lines)


async def retract_post(
    client: MaxClient | None,
    chat_id: int,
    args: list[str],
    config: AppConfig,
    db: Database,
) -> None:
    if len(args) < 2 or not args[1].isdigit():
        await _reply(client, chat_id, "Usage: /factory retract <post_id>")
        return
    post_id = int(args[1])

    posts = await db.get_recent_posts(_RETRACT_LOOKUP_LIMIT)
    post = next((p for p in posts if p.id == post_id), None)
    if post is None:
        await _reply(client, chat_id, f"Post {post_id} not found.")
        return
    if post.message_id is None or post.status == "previewed":
        await _reply(
            client,
            chat_id,
            "Cannot retract: the post has no channel message "
            "(dry-run previews go to the admins, not to the channel).",
        )
        return
    if client is None:
        await _reply(
            client, chat_id, "Retract is unavailable (bot instance missing)."
        )
        return

    try:
        # MaxClient.delete_message takes only the message id — the token
        # already scopes it to the bot's own messages
        await client.delete_message(post.message_id)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
        logger.error("factory_retract_failed id=%s error=%s", post_id, error)
        await _reply(client, chat_id, f"Retract failed: {error}")
        return
    await db.log_event("factory_retract", f"id={post_id}")
    await _reply(
        client, chat_id, f"Post {post_id} retracted from the channel."
    )


async def _reply(client: MaxClient | None, chat_id: int, text: str) -> None:
    if client is None:
        logger.warning("reply_skipped chat_id=%s (no client)", chat_id)
        return
    await client.send_message(chat_id, text, fmt="html")


def format_factory_result(result: FactoryResult) -> str:
    return (
        "Factory cycle done: "
        f"collected={result.collected} filtered={result.filtered} "
        f"deduped={result.deduped} written={result.written} "
        f"skipped={result.skipped} published={result.published} "
        f"previewed={result.previewed} failed={result.failed} "
        f"recovered={result.recovered}"
    )


def format_event(event: dict | None) -> str:
    if not event:
        return "n/a"
    details = html.escape(_short(event.get("details") or "no details"))
    return f"{html.escape(str(event['created_at']))} ({details})"


def format_timedelta(delta) -> str:
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text[:limit]
