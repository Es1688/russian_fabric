"""Admin levers and status for the rubrics contour behind /factory (plan §11).

The /factory command family predates the rubrics contour: without a
scheduler attached it keeps its legacy semantics untouched (backward
compatibility). This class is everything the rubric-argument extensions
need — pause/resume of one rubric or all of them (persisted in
rubric_state, so the state survives a restart), a manual out-of-schedule
slot run guarded by the same daily_cap/UNIQUE rules as scheduled slots,
a per-rubric status overview and slot-run retract for unknown-delivery
duplicates (plan section 10).
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from max_news_bot.bot.slot_scheduler import SlotScheduler, TickResult
from max_news_bot.config.rubrics import RubricConfig, RubricsConfig
from max_news_bot.core.models import SlotRun
from max_news_bot.max_api.client import MaxClient
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.repositories import (
    RubricStateRepository,
    SlotRunRepository,
)


logger = get_logger(__name__)

SCOPE_ALL = "all"
_ERROR_MAX_CHARS = 300


def format_tick_result(result: TickResult) -> str:
    """One-line admin view of a manual tick or slot run."""
    if result.globally_paused:
        return "Scheduler skipped: MAX API global pause is active."
    parts = [
        f"executed={len(result.executed)}",
        f"published={len(result.published)}",
        f"skipped={len(result.skipped)}",
        f"failed={len(result.failed)}",
    ]
    if result.recovered:
        parts.append(f"recovered={len(result.recovered)}")
    if result.unknown_delivery:
        parts.append(f"unknown_delivery={len(result.unknown_delivery)}")
    line = "Done: " + " ".join(parts)
    if result.skipped:
        reasons = ", ".join(
            f"{html.escape(rubric)} ({html.escape(reason)})"
            for rubric, reason in result.skipped
        )
        line += f". Skipped: {reasons}"
    return line


class RubricsAdmin:
    """Rubric-scoped levers of /factory (plan section 11)."""

    def __init__(
        self,
        config: RubricsConfig,
        scheduler: SlotScheduler,
        runs: SlotRunRepository,
        states: RubricStateRepository,
        db: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._scheduler = scheduler
        self._runs = runs
        self._states = states
        self._db = db
        self._tz = ZoneInfo(config.timezone)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def rubric_ids(self) -> tuple[str, ...]:
        """Enabled rubric ids in config order (command help and validation)."""
        return tuple(item.id for item in self._config.rubrics if item.enabled)

    def _enabled_rubric(self, rubric_id: str) -> RubricConfig | None:
        return next(
            (
                item
                for item in self._config.rubrics
                if item.enabled and item.id == rubric_id
            ),
            None,
        )

    def unknown_rubric_text(self, rubric_id: str) -> str:
        known = ", ".join(self.rubric_ids) or "none configured"
        return f"Unknown rubric: {html.escape(rubric_id)}. Configured: {known}."

    # --- levers -------------------------------------------------------------------

    async def pause(self, scope: str) -> str:
        """Pause one rubric or all of them; the state survives a restart."""
        if scope == SCOPE_ALL:
            for rubric_id in self.rubric_ids:
                await self._states.set_paused(rubric_id, True)
            await self._db.log_event("rubric_pause", "scope=all manual")
            logger.info("rubrics_paused scope=all count=%s", len(self.rubric_ids))
            return (
                f"All rubrics paused ({len(self.rubric_ids)}). Scheduled slots "
                "will be skipped with reason rubric_paused. Use "
                "/factory resume all to continue."
            )
        rubric = self._enabled_rubric(scope)
        if rubric is None:
            return self.unknown_rubric_text(scope)
        await self._states.set_paused(rubric.id, True)
        await self._db.log_event("rubric_pause", f"rubric={rubric.id} manual")
        logger.info("rubric_paused rubric=%s scope=manual", rubric.id)
        return (
            f"Rubric {rubric.id} paused. Scheduled slots will be skipped "
            f"with reason rubric_paused. Use /factory resume {rubric.id} "
            "to continue."
        )

    async def resume(self, scope: str) -> str:
        """Clear the pause of one rubric or all of them."""
        if scope == SCOPE_ALL:
            for rubric_id in self.rubric_ids:
                await self._states.set_paused(rubric_id, False)
            await self._db.log_event("rubric_resume", "scope=all manual")
            logger.info("rubrics_resumed scope=all count=%s", len(self.rubric_ids))
            return (
                f"All rubrics resumed ({len(self.rubric_ids)}). Slots fire "
                "again at their scheduled times."
            )
        rubric = self._enabled_rubric(scope)
        if rubric is None:
            return self.unknown_rubric_text(scope)
        await self._states.set_paused(rubric.id, False)
        await self._db.log_event("rubric_resume", f"rubric={rubric.id} manual")
        logger.info("rubric_resumed rubric=%s scope=manual", rubric.id)
        return f"Rubric {rubric.id} resumed."

    async def run_tick(self, reply: Callable[[str], Awaitable[None]]) -> None:
        """Bare ``/factory run`` in the rubrics contour: one immediate tick."""
        await reply("Running scheduler tick...")
        result = await self._scheduler.tick()
        await reply(format_tick_result(result))

    async def run_rubric(
        self, rubric_id: str, reply: Callable[[str], Awaitable[None]]
    ) -> None:
        """``/factory run <rubric>``: today's slot right now (plan section 11)."""
        if self._enabled_rubric(rubric_id) is None:
            await reply(self.unknown_rubric_text(rubric_id))
            return
        await reply(f"Running rubric {rubric_id} slot now...")
        result = await self._scheduler.run_slot_now(rubric_id)
        if result is None:
            await reply(self.unknown_rubric_text(rubric_id))
            return
        await reply(format_tick_result(result))

    # --- observability -------------------------------------------------------------

    async def status_text(self) -> str:
        now = self._clock()
        slot_date = now.astimezone(self._tz).date().isoformat()
        runs = {
            run.rubric_id: run
            for run in await self._runs.list_for_date(slot_date)
        }

        pause_until = self._scheduler.global_pause_until
        lines = [
            f"Rubrics: enabled, timezone {self._config.timezone}, "
            f"daily_cap {self._config.daily_cap}, date {slot_date}",
            "Global MAX pause: "
            + (
                f"until {pause_until.isoformat()}"
                if pause_until is not None and pause_until > now
                else "no"
            ),
        ]
        for rubric in self._config.rubrics:
            if not rubric.enabled:
                continue
            state = await self._states.get(rubric.id)
            target = (
                f", target override {html.escape(state.target)}"
                if state.target
                else ""
            )
            lines.append(
                f"- {rubric.id} ({rubric.kind}, {rubric.time}): "
                f"{'paused' if state.paused else 'active'}; "
                f"today: {self._describe_run(runs.get(rubric.id))}{target}"
            )
        return "\n".join(lines)

    @staticmethod
    def _describe_run(run: SlotRun | None) -> str:
        if run is None:
            return "not run yet"
        if run.status == "skipped" and run.skip_reason:
            return f"skipped ({html.escape(run.skip_reason)})"
        if run.status in ("failed", "unknown_delivery") and run.error:
            return f"{run.status} ({html.escape(run.error[:120])})"
        return run.status

    async def retract_slot(self, client: MaxClient | None, run_id: int) -> str:
        """Retract the channel message of a slot run (duplicate lever, §10)."""
        run = await self._runs.get_by_id(run_id)
        if run is None:
            return f"Slot run {run_id} not found."
        if run.max_message_id is None:
            return (
                f"Cannot retract slot run {run_id}: no channel message "
                "recorded (dry-run previews go to the admins, not the channel)."
            )
        if client is None:
            return "Retract is unavailable (bot instance missing)."
        try:
            await client.delete_message(run.max_message_id)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS]
            logger.error("slot_retract_failed run=%s error=%s", run_id, error)
            return f"Retract failed: {html.escape(error)}"
        await self._db.log_event(
            "slot_retract",
            f"run={run_id} rubric={run.rubric_id} date={run.slot_date}",
        )
        return (
            f"Slot run {run_id} ({run.rubric_id}, {run.slot_date}): "
            "message retracted from the channel."
        )


def build_rubrics_admin(
    config: RubricsConfig, scheduler: SlotScheduler, db: Database
) -> RubricsAdmin:
    """Wire a RubricsAdmin over the shared database file (used by main.py)."""
    return RubricsAdmin(
        config,
        scheduler,
        SlotRunRepository(db.path),
        RubricStateRepository(db.path),
        db,
    )
