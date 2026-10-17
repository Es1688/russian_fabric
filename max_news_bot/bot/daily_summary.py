"""Daily admin summary of the rubrics contour (plan section 11).

One message per day at the fixed config slot (``summary_time``, 22:40
Europe/Moscow by default — deliberately after the last post of the grid at
21:30): what came out, what was skipped and why, bank reserves, LLM spend
and LLM/image channel counters. LLM cost is reported as a call-count
estimate derived from stored data — attempts of LLM-kind slot runs plus
filter-log rows (classifier and checker calls); token usage is not stored
in slot_runs. Channel counters are COUNT over the durable bot_stats events
of the day (plan section 2.6): channel degradation is visible from the
summary without reading logs.

Delivery is once per local day: the service refuses to fire outside the
slot window (slot time + a restart grace) and skips a day already recorded
in bot_stats, so a restart inside the window never duplicates the message.
"""

from __future__ import annotations

import asyncio
import html
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from max_news_bot.config.rubrics import LLM_KINDS, RubricsConfig, WEEKDAYS
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    FilterLogRepository,
    SlotRunRepository,
)


logger = get_logger(__name__)

SUMMARY_EVENT = "daily_summary"
# Channel events written once per call by the failover wrappers
# (writers/fallback.py) and image pipelines (rubrics/generative.py, bank
# pipeline); the names must stay in sync with the emitters (plan section 2.6).
CHANNEL_EVENTS = (
    "llm_primary_ok",
    "llm_fallback_used",
    "image_fal_ok",
    "image_none",
)
# Plan section 6: alert when a bank type holds less than 30 days of stock;
# with at most one show per type per day the approved count approximates days.
BANK_LOW_WATERMARK = 30
# A restart inside this window still delivers today's summary exactly once.
SUMMARY_GRACE_MINUTES = 15
SUMMARY_TICK_SECONDS = 60.0

_WEEKDAY_RU = {
    "mon": "пн",
    "tue": "вт",
    "wed": "ср",
    "thu": "чт",
    "fri": "пт",
    "sat": "сб",
    "sun": "вс",
}

_IN_FLIGHT_STATUSES = ("pending", "generated", "publishing")

SendFn = Callable[[str], Awaitable[None]]


def parse_summary_time(summary_time: str) -> tuple[int, int]:
    """'22:40' -> (22, 40); raises ValueError on a malformed slot."""
    hours, _, minutes = summary_time.partition(":")
    hh, mm = int(hours), int(minutes or 0)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"invalid summary time {summary_time!r}")
    return hh, mm


class DailySummaryService:
    """Builds and delivers the once-a-day admin summary message."""

    def __init__(
        self,
        config: RubricsConfig,
        runs: SlotRunRepository,
        bank: ContentBankRepository,
        filters: FilterLogRepository,
        db: Database,
        send: SendFn,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._runs = runs
        self._bank = bank
        self._filters = filters
        self._db = db
        self._send = send
        self._tz = ZoneInfo(config.timezone)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- delivery ----------------------------------------------------------------

    async def send_if_due(self, now: datetime | None = None) -> bool:
        """Send the summary when the local time is inside today's slot window.

        Returns True when a message was actually delivered. The once-per-day
        rule is durable: the sent day is recorded as a bot_stats event and
        a restarted process reads it back instead of re-sending.
        """
        now = now if now is not None else self._clock()
        local = now.astimezone(self._tz)
        hh, mm = parse_summary_time(self._config.summary_time)
        slot = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not slot <= local <= slot + timedelta(minutes=SUMMARY_GRACE_MINUTES):
            return False
        slot_date = local.date().isoformat()
        last = await self._db.get_last_event(SUMMARY_EVENT)
        if last is not None and (last.get("details") or "").strip() == slot_date:
            return False  # already delivered today
        text = await self.build_text(slot_date)
        await self._send(text)
        await self._db.log_event(SUMMARY_EVENT, slot_date)
        logger.info("daily_summary_sent date=%s", slot_date)
        return True

    # --- message body -------------------------------------------------------------

    async def build_text(self, slot_date: str) -> str:
        day = date.fromisoformat(slot_date)
        runs = await self._runs.list_for_date(slot_date)
        published = [r for r in runs if r.status == "published"]
        skipped = [r for r in runs if r.status == "skipped"]
        failed = [r for r in runs if r.status == "failed"]
        unknown = [r for r in runs if r.status == "unknown_delivery"]
        in_flight = [r for r in runs if r.status in _IN_FLIGHT_STATUSES]

        planned = sum(
            1
            for rubric in self._config.rubrics
            if rubric.enabled and WEEKDAYS[day.weekday()] in rubric.days
        )

        lines = [
            f"📋 Сводка канала за {slot_date} ({_WEEKDAY_RU[WEEKDAYS[day.weekday()]]})",
            f"Вышло: {len(published)} из {planned} запланированных",
        ]
        for run in published:
            dry = " (dry-run превью)" if run.is_dry_run else ""
            lines.append(f"- {run.rubric_id} — опубликовано{dry}")
        if skipped:
            lines.append(f"Пропущено: {len(skipped)}")
            for run in skipped:
                reason = html.escape(run.skip_reason or "unknown")
                lines.append(f"- {run.rubric_id} — {reason}")
        if failed:
            lines.append(f"Сбои: {len(failed)}")
            for run in failed:
                error = html.escape(_short(run.error or "unknown"))
                lines.append(f"- {run.rubric_id} — {error}")
        if unknown:
            lines.append(f"Доставка неизвестна: {len(unknown)}")
            for run in unknown:
                lines.append(
                    f"- {run.rubric_id} (slot_runs #{run.id}) — проверьте канал; "
                    f"дубль можно убрать: /factory retract slot {run.id}"
                )
        if in_flight:
            lines.append(f"Не завершено к моменту сводки: {len(in_flight)}")
            for run in in_flight:
                lines.append(f"- {run.rubric_id} — {run.status}")

        lines.append(await self._bank_text())
        lines.append(await self._llm_text(day, runs))
        lines.append(await self._channels_text(day))
        return "\n".join(lines)

    async def _bank_text(self) -> str:
        counts = await self._bank.count_available_by_type()
        if not counts:
            return "Банк контента: пусто (одобрённых записей нет)"
        parts = []
        for item_type in sorted(counts):
            count = counts[item_type]
            flag = " ⚠ менее 30" if count < BANK_LOW_WATERMARK else ""
            parts.append(f"{html.escape(item_type)}={count}{flag}")
        return "Банк (одобрено): " + ", ".join(parts)

    def _local_day_window(self, day: date) -> tuple[datetime, datetime]:
        """UTC half-open interval [start, end) of the local calendar day.

        bot_stats.created_at and filter_log.created_at are UTC, the summary
        day is not (same window both aggregations count over).
        """
        start_local = datetime(day.year, day.month, day.day, tzinfo=self._tz)
        start_utc = start_local.astimezone(timezone.utc)
        return start_utc, start_utc + timedelta(days=1)

    async def _llm_text(self, day: date, runs: list) -> str:
        llm_rubrics = {
            item.id for item in self._config.rubrics if item.kind in LLM_KINDS
        }
        slot_calls = sum(
            run.attempts for run in runs if run.rubric_id in llm_rubrics
        )
        start_utc, end_utc = self._local_day_window(day)
        filter_calls = await self._filters.count_between(start_utc, end_utc)
        total = slot_calls + filter_calls
        return (
            f"LLM-вызовы (оценка): {total} — слоты {slot_calls}, "
            f"фильтры {filter_calls}"
        )

    async def _channels_text(self, day: date) -> str:
        """Per-channel LLM/image counters of the day (plan section 2.6).

        Quiet failover must not hide a degrading primary: the line reports
        both channels next to the cost estimate above; zero statistics
        still reads as explicit zeros.
        """
        start_utc, end_utc = self._local_day_window(day)
        counts = await asyncio.to_thread(
            self._count_channel_events_sync, start_utc, end_utc
        )
        return (
            f"LLM: primary {counts['llm_primary_ok']} / "
            f"fallback {counts['llm_fallback_used']}; "
            f"картинки: fal {counts['image_fal_ok']} / "
            f"без картинки {counts['image_none']}"
        )

    def _count_channel_events_sync(
        self, start_utc: datetime, end_utc: datetime
    ) -> dict[str, int]:
        placeholders = ",".join("?" for _ in CHANNEL_EVENTS)
        with sqlite3.connect(self._db.path) as conn:
            rows = conn.execute(
                f"""
                SELECT event, COUNT(*) FROM bot_stats
                WHERE event IN ({placeholders})
                  AND created_at >= ? AND created_at < ?
                GROUP BY event
                """,
                (
                    *CHANNEL_EVENTS,
                    start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    end_utc.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            ).fetchall()
        counts = dict.fromkeys(CHANNEL_EVENTS, 0)
        counts.update({row[0]: int(row[1]) for row in rows})
        return counts


def _short(text: str, limit: int = 120) -> str:
    return " ".join(text.split())[:limit]


async def daily_summary_loop(
    service: DailySummaryService, db: Database, *, tick_seconds: float = SUMMARY_TICK_SECONDS
) -> None:
    """Poll the summary slot once a minute and shield failures forever."""
    while True:
        try:
            await service.send_if_due()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("daily_summary_error error=%s", exc)
            await db.log_event("error", f"daily_summary_error={exc}")
        await asyncio.sleep(tick_seconds)
