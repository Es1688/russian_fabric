"""Slot scheduler for the autonomous channel (plan sections 10 and 11).

The only publishing contour of the process: the day is a grid of rubric
slots, every slot
is claimed exactly once through UNIQUE(rubric_id, slot_date), slots that
became due in the same tick execute strictly in time order, and each run is
tracked through the ``slot_runs`` lifecycle:

    pending -> generated -> publishing -> published
            -> skipped / failed / unknown_delivery

Crash windows are closed constructively (plan section 10): the slot moves to
``publishing`` *before* the network send, so after a restart a run left in
``publishing`` is never re-sent blindly - it becomes ``unknown_delivery``
with an alert; a run left ``generated`` is re-sent from the saved text
without another LLM call; a leftover ``pending`` is re-executed while it is
still within grace, otherwise it is recorded as skipped(missed_grace).

Automatic breakers (plan section 11): N consecutive failed slots pause the
rubric (persisted in rubric_state) with an alert; an infra failure of the
MAX publish call trips a global pause with a retry deadline - slots stay
unclaimed until the pause lifts and then run if still within grace; a feed
with three empty responses in a row is excluded and reported.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from max_news_bot.config.rubrics import RubricConfig, RubricsConfig, WEEKDAYS
from max_news_bot.core.models import SlotRun
from max_news_bot.publishers.alerter import Alerter
from max_news_bot.utils.db import Database
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.repositories import RubricStateRepository, SlotRunRepository


logger = get_logger(__name__)

TICK_SECONDS: float = 45.0  # plan section 10: one tick every 30-60 seconds
MAX_GENERATION_ATTEMPTS = 3  # 1 LLM call + up to 2 regenerations (plan section 7)
# Liveness heartbeat written by the loop for the Docker healthcheck (plan
# section 11); scripts/healthcheck.py allows 2x this window by default.
HEARTBEAT_SECONDS: float = 900.0
HEARTBEAT_EVENT = "slot_heartbeat"
_ERROR_MAX_CHARS = 300

# Runs counted by daily_cap: everything that did produce or may still
# produce a post today. skipped/failed produced nothing and stay free.
# Dry-run previews are counted out at the call site: they go to the
# admins, not the channel, and the cap limits channel posts (plan §2).
CAP_STATUSES = (
    "pending",
    "generated",
    "publishing",
    "published",
    "unknown_delivery",
)

SKIP_MISSED_GRACE = "missed_grace"
SKIP_RUBRIC_PAUSED = "rubric_paused"
SKIP_NO_TOPIC = "no_topic"
SKIP_DAILY_CAP = "daily_cap"

DEFAULT_STALE_MINUTES = 10
DEFAULT_PAUSE_AFTER_FAILURES = 3
DEFAULT_GLOBAL_PAUSE_RETRY_SECONDS = 300.0
DEFAULT_EMPTY_SOURCE_LIMIT = 3


@dataclass(frozen=True)
class SourceStat:
    """Per-feed fetch outcome of one generation attempt."""

    source: str
    items: int


@dataclass(frozen=True)
class GenerationOutcome:
    """One generation attempt of a rubric pipeline.

    ``status`` is "generated" (text ready), "failed" (retryable unless
    ``error_kind`` is "config") or "skipped" (deliberate skip, e.g. weather
    without the primary point). ``source_stats`` feeds the empty-source
    breaker; ``alert`` is an optional one-shot anomaly text.
    """

    status: str
    text: str | None = None
    image_ref: str | None = None
    source_refs: tuple[str, ...] = ()
    error: str | None = None
    error_kind: str | None = None  # content|infra|config (factory taxonomy)
    skip_reason: str | None = None
    alert: str | None = None
    source_stats: tuple[SourceStat, ...] = ()


@dataclass(frozen=True)
class SlotPublishResult:
    """Result of one slot send."""

    success: bool
    message_id: str | None = None
    error: str | None = None
    # "content" (text unusable at the MAX side) or "infra" (MAX API down,
    # rate limited, network) - infra trips the global pause
    error_kind: str | None = None


class RubricPipeline(Protocol):
    """One generation attempt for a rubric slot (LLM + validators inside).

    The scheduler owns retry counts and the fallback policy (plan section
    7); the pipeline performs a single attempt and reports per-source fetch
    stats so the scheduler can trip the empty-source breaker.
    """

    async def generate(
        self, rubric: RubricConfig, run: SlotRun, *, now: datetime
    ) -> GenerationOutcome: ...

    async def fallback_post(
        self, rubric: RubricConfig, run: SlotRun, *, now: datetime
    ) -> GenerationOutcome: ...


class SlotPublisher(Protocol):
    async def publish_slot(
        self,
        text: str,
        *,
        chat_id: int,
        image_ref: str | None = None,
        to_admins: bool = False,
    ) -> SlotPublishResult: ...


@dataclass(frozen=True)
class TickResult:
    """What one scheduler tick did (for logs and tests)."""

    globally_paused: bool = False
    recovered: tuple[str, ...] = ()
    unknown_delivery: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    published: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def is_quiet(self) -> bool:
        return not (
            self.recovered
            or self.unknown_delivery
            or self.executed
            or self.skipped
            or self.published
            or self.failed
        )


class _TickAccumulator:
    __slots__ = (
        "recovered",
        "unknown_delivery",
        "executed",
        "skipped",
        "published",
        "failed",
    )

    def __init__(self) -> None:
        self.recovered: list[str] = []
        self.unknown_delivery: list[str] = []
        self.executed: list[str] = []
        self.skipped: list[tuple[str, str]] = []
        self.published: list[str] = []
        self.failed: list[str] = []

    def freeze(self) -> TickResult:
        return TickResult(
            recovered=tuple(self.recovered),
            unknown_delivery=tuple(self.unknown_delivery),
            executed=tuple(self.executed),
            skipped=tuple(self.skipped),
            published=tuple(self.published),
            failed=tuple(self.failed),
        )


class SourceHealthBreaker:
    """Excludes a feed after N empty fetches in a row (plan section 11).

    In-memory on purpose: a restart gives the feed a fresh chance, and three
    more empty answers re-exclude it. Pipelines consult ``is_excluded``
    before fetching.
    """

    def __init__(
        self, alerter: Alerter, *, limit: int = DEFAULT_EMPTY_SOURCE_LIMIT
    ) -> None:
        self._alerter = alerter
        self.limit = limit
        self._empty_streak: dict[str, int] = {}
        self._excluded: set[str] = set()

    async def record(self, source: str, items: int) -> None:
        if source in self._excluded:
            return
        if items > 0:
            self._empty_streak[source] = 0
            return
        streak = self._empty_streak.get(source, 0) + 1
        self._empty_streak[source] = streak
        if streak < self.limit:
            return
        self._excluded.add(source)
        logger.warning(
            "slot_scheduler_source_excluded source=%s streak=%s", source, streak
        )
        try:
            await self._alerter.send_alert(
                f"News source excluded after {streak} empty responses in a "
                f"row: {source}. Check the feed; a restart re-enables it."
            )
        except Exception as exc:
            logger.error("slot_scheduler_source_alert_failed error=%s", exc)

    def is_excluded(self, source: str) -> bool:
        return source in self._excluded

    def empty_streak(self, source: str) -> int:
        return self._empty_streak.get(source, 0)

    @property
    def excluded(self) -> frozenset[str]:
        return frozenset(self._excluded)


def slot_jitter_minutes(rubric: RubricConfig, slot_date: str) -> int:
    """Deterministic jitter in [0, rubric.jitter_minutes].

    Derived from (rubric id, slot date) so every tick and a restart compute
    the same slot time - otherwise a slot could fire at a different moment
    after each tick. Weather's 2-minute cap keeps it behind the 07:00
    morning post (plan section 2).
    """
    if rubric.jitter_minutes <= 0:
        return 0
    digest = hashlib.sha1(f"{rubric.id}:{slot_date}".encode()).digest()
    return digest[0] % (rubric.jitter_minutes + 1)


def _load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("unknown timezone=%s, falling back to UTC", name)
        return ZoneInfo("UTC")


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS]


def _parse_chat_id(target: str | None) -> int | None:
    if target is None:
        return None
    try:
        return int(target)
    except ValueError:
        return None


def _slot_weekday(slot_date: str) -> str:
    return WEEKDAYS[date.fromisoformat(slot_date).weekday()]


class SlotScheduler:
    """Drives the rubric grid day by day (plan sections 2, 10, 11)."""

    def __init__(
        self,
        config: RubricsConfig,
        runs: SlotRunRepository,
        states: RubricStateRepository,
        pipeline: RubricPipeline,
        publisher: SlotPublisher,
        alerter: Alerter,
        *,
        dry_run: bool = False,
        tick_seconds: float = TICK_SECONDS,
        stale_minutes: int = DEFAULT_STALE_MINUTES,
        pause_after_failures: int = DEFAULT_PAUSE_AFTER_FAILURES,
        global_pause_retry_seconds: float = DEFAULT_GLOBAL_PAUSE_RETRY_SECONDS,
        empty_source_limit: int = DEFAULT_EMPTY_SOURCE_LIMIT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._runs = runs
        self._states = states
        self._pipeline = pipeline
        self._publisher = publisher
        self._alerter = alerter
        self._dry_run = dry_run
        self.tick_seconds = tick_seconds
        self._stale_minutes = stale_minutes
        self._pause_after_failures = pause_after_failures
        self._global_pause_retry_seconds = global_pause_retry_seconds
        self._tz = _load_timezone(config.timezone)
        self._rubrics = tuple(item for item in config.rubrics if item.enabled)
        self._rubric_by_id = {item.id: item for item in self._rubrics}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # consecutive failed slots per rubric (in memory; the pause itself
        # is durable via rubric_state, like the factory's failed streak)
        self._failure_streak: dict[str, int] = {}
        self._global_pause_until: datetime | None = None
        self._pause_incident_open = False
        # shared with pipelines: they report stats, the digest consults it
        self.sources = SourceHealthBreaker(alerter, limit=empty_source_limit)

    # --- entry points ----------------------------------------------------------

    async def startup_recover(self, now: datetime | None = None) -> TickResult:
        """Re-adopt rows interrupted by the previous process (plan section 10).

        Single writer per database (plan section 15): at startup nothing is
        in flight, so every pending/generated/publishing row is a crash
        leftover regardless of its age.
        """
        now = now if now is not None else self._clock()
        accumulator = _TickAccumulator()
        await self._recover_stale(now, accumulator, all_rows=True)
        return accumulator.freeze()

    async def tick(self, now: datetime | None = None) -> TickResult:
        now = now if now is not None else self._clock()
        if self._global_pause_until is not None:
            if now >= self._global_pause_until:
                self._global_pause_until = None
                logger.info("slot_scheduler_global_pause_expired")
            else:
                return TickResult(globally_paused=True)
        accumulator = _TickAccumulator()
        await self._recover_stale(now, accumulator)
        for _scheduled, kind, rubric, slot_date in self._actionable(now):
            if self._global_pause_until is not None:
                # tripped mid-tick by an infra publish failure: remaining
                # slots stay unclaimed and run after the retry window while
                # still within grace ("global pause with a retry")
                break
            if kind == "expired":
                await self._claim_and_skip(
                    rubric, slot_date, SKIP_MISSED_GRACE, accumulator
                )
            else:
                await self._run_due_slot(rubric, slot_date, now, accumulator)
        return accumulator.freeze()

    async def run_slot_now(
        self, rubric_id: str, now: datetime | None = None
    ) -> TickResult | None:
        """Manual lever ``/factory run <rubric>`` (plan section 11).

        Claims and executes today's slot of the rubric immediately,
        bypassing the scheduled time; every other guard (rubric pause,
        daily_cap, the UNIQUE once-a-day claim) is exactly the scheduled
        one, which is what makes daily_cap a manual-run safety net (plan
        section 2). Returns None when the rubric is unknown or disabled.
        """
        rubric = self._rubric_by_id.get(rubric_id)
        if rubric is None:
            return None
        now = now if now is not None else self._clock()
        if self._global_pause_until is not None and now < self._global_pause_until:
            return TickResult(globally_paused=True)
        slot_date = now.astimezone(self._tz).date().isoformat()
        accumulator = _TickAccumulator()
        await self._run_due_slot(rubric, slot_date, now, accumulator)
        return accumulator.freeze()

    @property
    def global_pause_until(self) -> datetime | None:
        """Active MAX API global pause deadline, if any (admin status)."""
        return self._global_pause_until

    # --- slot selection ---------------------------------------------------------

    def _actionable(
        self, now: datetime
    ) -> list[tuple[datetime, str, RubricConfig, str]]:
        """Today's slots that are due or expired, in scheduled-time order.

        A stable sort keeps the config order for equal times, so the 07:00
        morning post always precedes 07:05 weather even at full jitter
        (plan section 2).
        """
        now_local = now.astimezone(self._tz)
        slot_date = now_local.date().isoformat()
        weekday = WEEKDAYS[now_local.weekday()]
        actionable: list[tuple[datetime, str, RubricConfig, str]] = []
        for rubric in self._rubrics:
            if weekday not in rubric.days:
                continue
            scheduled = self._scheduled_utc(rubric, slot_date)
            grace = timedelta(minutes=rubric.grace_minutes)
            if now < scheduled:
                continue
            kind = "due" if now <= scheduled + grace else "expired"
            actionable.append((scheduled, kind, rubric, slot_date))
        actionable.sort(key=lambda item: item[0])
        return actionable

    def _scheduled_utc(self, rubric: RubricConfig, slot_date: str) -> datetime:
        day = date.fromisoformat(slot_date)
        hour, minute = (int(part) for part in rubric.time.split(":"))
        local = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=self._tz
        ) + timedelta(minutes=slot_jitter_minutes(rubric, slot_date))
        return local.astimezone(timezone.utc)

    def _needs_topic(self, rubric: RubricConfig, weekday: str) -> bool:
        return (
            rubric.kind == "generative"
            and bool(rubric.topics_by_weekday)
            and weekday not in rubric.topics_by_weekday
        )

    async def _run_due_slot(
        self,
        rubric: RubricConfig,
        slot_date: str,
        now: datetime,
        accumulator: _TickAccumulator,
    ) -> None:
        state = await self._states.get(rubric.id)
        if state.paused:
            await self._claim_and_skip(rubric, slot_date, SKIP_RUBRIC_PAUSED, accumulator)
            return
        if self._needs_topic(rubric, _slot_weekday(slot_date)):
            await self._claim_and_skip(rubric, slot_date, SKIP_NO_TOPIC, accumulator)
            return
        # daily_cap limits channel posts (plan section 2); dry-run previews
        # go to the admins and must not consume it, otherwise a dry-run
        # shakedown over the 6-slot grid would starve the night slot
        used = await self._runs.count_for_date(
            slot_date, statuses=CAP_STATUSES, dry_run=False
        )
        if used >= self._config.daily_cap:
            await self._claim_and_skip(rubric, slot_date, SKIP_DAILY_CAP, accumulator)
            return
        if state.target is not None and _parse_chat_id(state.target) is None:
            run = await self._runs.try_claim(rubric.id, slot_date)
            if run is not None:
                await self._fail_slot(
                    rubric,
                    run,
                    f"invalid target override {state.target!r}",
                    accumulator,
                    alert=(
                        f"Rubric {rubric.id} target override "
                        f"{state.target!r} is not an integer chat id."
                    ),
                )
            return
        if state.target is not None:
            target = state.target
        else:
            target = str(self._config.targets[rubric.target].channel_id)
        run = await self._runs.try_claim(
            rubric.id, slot_date, target=target, is_dry_run=self._dry_run
        )
        if run is None:
            return  # UNIQUE: another tick or process already owns the slot
        accumulator.executed.append(rubric.id)
        await self._execute(rubric, run, now, accumulator)

    async def _claim_and_skip(
        self,
        rubric: RubricConfig,
        slot_date: str,
        reason: str,
        accumulator: _TickAccumulator,
    ) -> None:
        run = await self._runs.try_claim(rubric.id, slot_date)
        if run is None:
            return
        await self._runs.save(replace(run, status="skipped", skip_reason=reason))
        accumulator.skipped.append((rubric.id, reason))
        logger.info(
            "slot_skipped rubric=%s date=%s reason=%s", rubric.id, slot_date, reason
        )

    # --- recovery ---------------------------------------------------------------

    async def _recover_stale(
        self,
        now: datetime,
        accumulator: _TickAccumulator,
        *,
        all_rows: bool = False,
    ) -> None:
        """Re-adopt interrupted runs (startup) or hung ones (in-process).

        Fresh rows (updated within ``stale_minutes``) are left alone: at
        ``all_rows`` (startup) the single-writer rule makes every row a
        leftover. A stale ``pending`` is re-executed only while within
        grace; a stale ``generated`` is re-sent from its saved text without
        a new LLM call; a stale ``publishing`` is never re-sent blindly - it
        becomes ``unknown_delivery`` with an alert (plan section 10).
        """
        cutoff = None if all_rows else now - timedelta(minutes=self._stale_minutes)
        for status in ("pending", "generated"):
            for run in await self._runs.find_by_status(status):
                if self._is_fresh(run, cutoff):
                    continue
                rubric = self._rubric_by_id.get(run.rubric_id)
                if rubric is None:
                    logger.warning(
                        "slot_scheduler_orphan_run rubric=%s status=%s date=%s",
                        run.rubric_id,
                        status,
                        run.slot_date,
                    )
                    continue
                if status == "pending":
                    scheduled = self._scheduled_utc(rubric, run.slot_date)
                    if now > scheduled + timedelta(minutes=rubric.grace_minutes):
                        await self._runs.save(
                            replace(run, status="skipped", skip_reason=SKIP_MISSED_GRACE)
                        )
                        accumulator.skipped.append((run.rubric_id, SKIP_MISSED_GRACE))
                        continue
                    await self._execute(rubric, run, now, accumulator)
                else:
                    await self._publish(rubric, run, now, accumulator)
                accumulator.recovered.append(run.rubric_id)
        for run in await self._runs.find_by_status("publishing"):
            if self._is_fresh(run, cutoff):
                continue
            await self._runs.save(
                replace(
                    run,
                    status="unknown_delivery",
                    error="interrupted while publishing",
                )
            )
            accumulator.unknown_delivery.append(run.rubric_id)
            await self._alert(
                f"Rubric slot {run.rubric_id} ({run.slot_date}) was "
                "interrupted during publishing and delivery is unknown - "
                "check the channel, then retract the duplicate or complete "
                "the slot manually."
            )

    @staticmethod
    def _is_fresh(run: SlotRun, cutoff: datetime | None) -> bool:
        if cutoff is None:
            return False
        return run.updated_at is not None and run.updated_at > cutoff

    # --- slot lifecycle ----------------------------------------------------------

    async def _execute(
        self,
        rubric: RubricConfig,
        run: SlotRun,
        now: datetime,
        accumulator: _TickAccumulator,
    ) -> None:
        """pending -> (generated | skipped | failed): data, generation, fallback."""
        outcome: GenerationOutcome | None = None
        last_error: str | None = None
        config_error = False
        attempt = run.attempts  # a recovered pending keeps its attempt count
        while attempt < MAX_GENERATION_ATTEMPTS:
            attempt += 1
            run = replace(run, attempts=attempt)
            await self._runs.save(run)  # a crash mid-LLM keeps the count
            outcome = await self._generate(rubric, run, now)
            await self._record_source_stats(outcome)
            if outcome.alert:
                await self._alert(outcome.alert)
            if outcome.status != "failed":
                break
            last_error = outcome.error or "generation failed"
            if outcome.error_kind == "config":
                # billing/access: retrying the same key is pointless
                config_error = True
                break
        if outcome is not None and outcome.status == "skipped":
            reason = outcome.skip_reason or "pipeline_skip"
            await self._runs.save(replace(run, status="skipped", skip_reason=reason))
            accumulator.skipped.append((rubric.id, reason))
            logger.info(
                "slot_skipped rubric=%s date=%s reason=%s",
                rubric.id,
                run.slot_date,
                reason,
            )
            return
        if outcome is not None and outcome.status == "generated" and outcome.text:
            run = replace(
                run,
                status="generated",
                text=outcome.text,
                image_ref=outcome.image_ref,
                source_refs=outcome.source_refs,
            )
            await self._runs.save(run)
            await self._publish(rubric, run, now, accumulator)
            return
        if config_error:
            await self._alert(
                f"LLM config/access error in rubric {rubric.id} "
                f"({last_error}); check the key and billing."
            )
        # plan section 7 chain: reserve post from the bank, else failed+alert
        if rubric.on_llm_failure == "bank":
            fallback = await self._fallback(rubric, run, now)
            if fallback.status == "generated" and fallback.text:
                run = replace(
                    run,
                    status="generated",
                    text=fallback.text,
                    image_ref=fallback.image_ref,
                    source_refs=fallback.source_refs,
                )
                await self._runs.save(run)
                await self._publish(rubric, run, now, accumulator)
                return
            if fallback.error:
                last_error = f"{last_error}; fallback: {fallback.error}"[
                    :_ERROR_MAX_CHARS
                ]
        await self._fail_slot(
            rubric,
            run,
            last_error,
            accumulator,
            alert=(
                f"Rubric slot failed: {rubric.id} ({run.slot_date}) after "
                f"{run.attempts} attempt(s): {last_error}"
            ),
        )

    async def _publish(
        self,
        rubric: RubricConfig,
        run: SlotRun,
        now: datetime,
        accumulator: _TickAccumulator,
    ) -> None:
        """generated -> publishing -> published (or back to generated/failed)."""
        chat_id = _parse_chat_id(run.target)
        if chat_id is None:
            await self._fail_slot(
                rubric,
                run,
                f"invalid target {run.target!r}",
                accumulator,
                alert=(
                    f"Rubric slot {rubric.id} ({run.slot_date}) has an "
                    f"invalid target {run.target!r}."
                ),
            )
            return
        # plan section 10: publishing BEFORE the network call, so a crash
        # here is never re-sent blindly after a restart
        run = replace(run, status="publishing")
        await self._runs.save(run)
        try:
            result = await self._publisher.publish_slot(
                run.text or "",
                chat_id=chat_id,
                image_ref=run.image_ref,
                to_admins=run.is_dry_run,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = SlotPublishResult(
                success=False, error=_safe_error(exc), error_kind="infra"
            )
        if result.success:
            run = replace(
                run,
                status="published",
                max_message_id=result.message_id,
                published_at=now,
            )
            await self._runs.save(run)
            accumulator.published.append(rubric.id)
            self._failure_streak[rubric.id] = 0
            if self._pause_incident_open:
                self._pause_incident_open = False
                await self._recovery(
                    "Slot publishing recovered after a MAX API pause."
                )
            return
        error = result.error or "publish failed"
        if (result.error_kind or "infra") == "infra":
            # MAX API down or rate limited: global pause with a retry (plan
            # section 11). The generated text is kept - the recovery path
            # re-sends it after the pause without another LLM call.
            await self._runs.save(replace(run, status="generated", error=error))
            await self._trip_global_pause(now, error)
            return
        await self._fail_slot(
            rubric,
            run,
            error,
            accumulator,
            alert=(
                f"Rubric slot publish rejected: {rubric.id} "
                f"({run.slot_date}): {error}"
            ),
        )

    async def _fail_slot(
        self,
        rubric: RubricConfig,
        run: SlotRun,
        error: str | None,
        accumulator: _TickAccumulator,
        *,
        alert: str | None,
    ) -> None:
        await self._runs.save(replace(run, status="failed", error=error))
        accumulator.failed.append(rubric.id)
        if alert:
            await self._alert(alert)
        streak = self._failure_streak.get(rubric.id, 0) + 1
        self._failure_streak[rubric.id] = streak
        if streak >= self._pause_after_failures:
            await self._states.set_paused(rubric.id, True)
            logger.warning(
                "slot_scheduler_rubric_paused rubric=%s streak=%s", rubric.id, streak
            )
            await self._alert(
                f"Rubric {rubric.id} paused automatically after {streak} "
                "consecutive failed slots; resume it with /factory resume."
            )

    # --- breakers and alerting ----------------------------------------------------

    async def _trip_global_pause(self, now: datetime, cause: str) -> None:
        self._global_pause_until = now + timedelta(
            seconds=self._global_pause_retry_seconds
        )
        logger.warning(
            "slot_scheduler_global_pause until=%s cause=%s",
            self._global_pause_until.isoformat(),
            cause,
        )
        if self._pause_incident_open:
            return  # one alert per incident, re-trips only extend the window
        self._pause_incident_open = True
        await self._alert(
            f"MAX API publish failed ({cause}); the rubrics scheduler is "
            f"globally paused for {int(self._global_pause_retry_seconds)}s "
            "and will retry then. Generated posts are kept and re-sent "
            "without a new LLM call."
        )

    async def _record_source_stats(self, outcome: GenerationOutcome) -> None:
        for stat in outcome.source_stats:
            await self.sources.record(stat.source, stat.items)

    async def _generate(
        self, rubric: RubricConfig, run: SlotRun, now: datetime
    ) -> GenerationOutcome:
        try:
            return await self._pipeline.generate(rubric, run, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "slot_pipeline_crashed rubric=%s error=%s", rubric.id, exc
            )
            return GenerationOutcome(
                status="failed", error=_safe_error(exc), error_kind="infra"
            )

    async def _fallback(
        self, rubric: RubricConfig, run: SlotRun, now: datetime
    ) -> GenerationOutcome:
        try:
            return await self._pipeline.fallback_post(rubric, run, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "slot_fallback_crashed rubric=%s error=%s", rubric.id, exc
            )
            return GenerationOutcome(
                status="failed", error=_safe_error(exc), error_kind="infra"
            )

    async def _alert(self, text: str) -> None:
        try:
            await self._alerter.send_alert(text)
        except Exception as exc:
            logger.error("slot_scheduler_alert_failed error=%s", exc)

    async def _recovery(self, text: str) -> None:
        try:
            await self._alerter.send_recovery(text)
        except Exception as exc:
            logger.error("slot_scheduler_recovery_failed error=%s", exc)


async def slot_scheduler_loop(
    scheduler: SlotScheduler,
    db: Database,
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> None:
    """Pace and shield the slot scheduler, mirroring bot/scheduler.py loops.

    Every ``heartbeat_seconds`` a "slot_heartbeat" bot_stats event is
    written even when the day is quiet: the external watchdog
    (scripts/healthcheck.py, plan section 11) distinguishes "no slots due"
    from "the process hung" by that event alone.
    """
    try:
        result = await scheduler.startup_recover()
        if not result.is_quiet():
            logger.info("slot_scheduler_startup_recovery %s", result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("slot_scheduler_startup_error error=%s", exc)
        await db.log_event("error", f"slot_scheduler_startup_error={exc}")
    loop = asyncio.get_running_loop()
    next_heartbeat = loop.time()  # first beat lands with the first tick
    while True:
        try:
            result = await scheduler.tick()
            if not result.is_quiet():
                logger.info("slot_scheduler_tick %s", result)
            if loop.time() >= next_heartbeat:
                next_heartbeat = loop.time() + heartbeat_seconds
                details = (
                    "quiet"
                    if result.is_quiet() and not result.globally_paused
                    else f"paused={int(result.globally_paused)} "
                    f"published={len(result.published)} "
                    f"skipped={len(result.skipped)} "
                    f"failed={len(result.failed)}"
                )
                try:
                    await db.log_event(HEARTBEAT_EVENT, details)
                except Exception as exc:
                    logger.error("slot_scheduler_heartbeat_failed error=%s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("slot_scheduler_error error=%s", exc)
            await db.log_event("error", f"slot_scheduler_error={exc}")
        await asyncio.sleep(scheduler.tick_seconds)
