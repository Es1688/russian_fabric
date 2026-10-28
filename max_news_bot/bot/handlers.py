from __future__ import annotations

import html
from datetime import datetime, timezone

from max_news_bot.bot.rubrics_admin import RubricsAdmin
from max_news_bot.config.loader import AppConfig
from max_news_bot.max_api.client import MaxClient
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

_MAX_ERROR_LEN = 300
_USER_ERROR_TEXT = "Internal error occurred. Check /status later for details."

# /start doubles as the admin opt-in: MAX forbids a bot from DMing a user
# until that user has started it (see publishers/alerter.py).
_START_TEXT = (
    "Привет! Это бот автономного канала: он ведёт суточную сетку рубрик "
    "и публикует посты в канал.\n"
    "Если вы админ — бот может писать вам в личные сообщения "
    "(превью dry-run и алерты приходят сюда).\n"
    "Команды: /status, /stats, /factory."
)

# Registered via PATCH /me/commands at startup (best-effort).
BOT_COMMANDS: list[dict] = [
    {"name": "start", "description": "Приветствие и подписка на личные сообщения"},
    {"name": "chatid", "description": "Показать chat_id чата и user_id отправителя"},
    {"name": "status", "description": "Состояние бота"},
    {"name": "stats", "description": "Статистика по дням"},
    {"name": "factory", "description": "Рубрики: pause, run, resume, status, retract (админ)"},
]


def create_dispatcher(
    config: AppConfig,
    db: Database,
    started_at: datetime,
    *,
    client: MaxClient | None = None,
    rubrics_admin: RubricsAdmin,
):
    """Wire all command handlers into one ``handle_update(update: dict)``.

    Plain MAX update dicts in, replies out via MaxClient.send_message —
    no framework, so tests call the returned coroutine directly.
    ``rubrics_admin`` carries the contour levers (plan section 11): the
    rubrics contour is the only publishing contour of the process.
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
            # setup helper: chat_id goes to MAX_CHANNEL_ID, the sender's
            # user_id goes to ADMIN_IDS (DMs are addressed by user_id)
            user_part = str(user_id) if user_id is not None else "unknown"
            await reply(
                chat_id,
                f"chat_id: {chat_id} (MAX_CHANNEL_ID)\n"
                f"user_id: {user_part} (ADMIN_IDS)",
            )
            return

        if command == "status":
            uptime = datetime.now(timezone.utc) - started_at
            last_heartbeat = await db.get_last_event("slot_heartbeat")
            last_error = await db.get_last_event("error")
            await reply(
                chat_id,
                "\n".join(
                    [
                        f"Uptime: {format_timedelta(uptime)}",
                        f"Last scheduler heartbeat: {format_event(last_heartbeat)}",
                        f"Last error: {format_event(last_error)}",
                    ]
                ),
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

        if command == "factory":
            if not is_admin(user_id):
                logger.info(
                    "access_denied chat_id=%s user_id=%s command=%s",
                    chat_id,
                    user_id,
                    command,
                )
                await reply(chat_id, "Access denied.")
                return
            await dispatch_factory(chat_id, args)
            return

        # unknown command: silent, mirroring the aiogram router fallthrough

    async def dispatch_factory(chat_id: int, args: list[str]) -> None:
        action = args[0].lower() if args else "status"
        scope = args[1].lower() if len(args) > 1 else None

        def admin_reply(text: str):
            return reply(chat_id, text)

        if action == "run":
            if scope is not None:
                # /factory run <rubric>: manual today's slot of the rubric,
                # guarded by the same daily_cap/UNIQUE rules (plan section 2)
                await rubrics_admin.run_rubric(scope, admin_reply)
                return
            # bare run = one immediate scheduler tick
            await rubrics_admin.run_tick(admin_reply)
            return

        if action == "pause":
            # /factory pause [all|<rubric>]: rubric_state pause (plan section 11)
            await reply(chat_id, await rubrics_admin.pause(scope or "all"))
            return

        if action == "resume":
            # /factory resume [all|<rubric>]: clear the rubric_state pause
            await reply(chat_id, await rubrics_admin.resume(scope or "all"))
            return

        if action == "status":
            await reply(chat_id, await rubrics_admin.status_text())
            return

        if action == "retract":
            # /factory retract slot <run_id>: unknown-delivery duplicate
            # lever of the contour (plan section 10)
            if scope != "slot" or len(args) < 3 or not args[2].isdigit():
                await reply(
                    chat_id,
                    "Usage: /factory retract slot &lt;run_id&gt;",
                )
                return
            await reply(
                chat_id,
                await rubrics_admin.retract_slot(client, int(args[2])),
            )
            return

        # replies are html: literal angle brackets must be escaped
        await reply(
            chat_id,
            "Usage: /factory [run|pause|resume|status] [all|&lt;rubric&gt;] | "
            "/factory retract slot &lt;run_id&gt;",
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
    """"/factory retract slot 5" -> ("factory", ["retract", "slot", "5"]); plain
    text or empty input -> ("", [])."""
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
