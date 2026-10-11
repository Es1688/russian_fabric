"""Slot scheduler tests (plan sections 2, 10, 11).

Time is fully mocked: ticks receive an explicit ``now``. Tests that mix
fake ticks with the database's own CURRENT_TIMESTAMP (the stale-row sweep)
anchor their fake clock to the real one via ``_anchored_now``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from max_news_bot.bot.slot_scheduler import (
    SlotScheduler,
    SourceHealthBreaker,
    SourceStat,
    GenerationOutcome,
    SlotPublishResult,
    slot_jitter_minutes,
    slot_scheduler_loop,
)
from max_news_bot.config.rubrics import (
    RubricConfig,
    RubricsConfig,
    TargetConfig,
    WEEKDAYS,
)
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    RubricStateRepository,
    SlotRunRepository,
)
from max_news_bot.tests.mocks import MockAlerter


MAIN_CHAT = -1001234567890
ALL_DAYS = tuple(WEEKDAYS)
UTC = timezone.utc

# Fixed fake "today": a Friday. days=ALL_DAYS keeps weekday matching out of
# the picture except for the explicit no_topic test, which picks its own day.
DAY = date(2026, 10, 9)
DAY2 = date(2026, 10, 10)
DAY3 = date(2026, 10, 11)

GENERATED_TEXT = "Доброе утро! Пусть день будет тёплым и спокойным." * 3
BANK_TEXT = "Резервный пост из банка контента: тёплая мысль на день."


def _at(day: date, hour: int, minute: int) -> datetime:
    """UTC datetime on a fixed calendar day."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _anchored_now() -> datetime:
    """Real-clock-anchored 'now' for tests mixing fake ticks with the DB's
    CURRENT_TIMESTAMP (stale-row sweep), pushed away from the midnight minute
    so that HH:MM strings derived from it stay on the same calendar day."""
    now = datetime.now(UTC)
    if now.time() >= time(23, 55) or now.time() < time(0, 5):
        now = now + timedelta(minutes=10)
    return now


def make_rubric(
    rubric_id: str = "good_morning",
    *,
    time_str: str = "07:00",
    kind: str = "bank",
    days: tuple[str, ...] = ALL_DAYS,
    grace_minutes: int = 60,
    jitter_minutes: int = 0,
    on_llm_failure: str = "bank",
    topics_by_weekday: dict[str, tuple[str, ...]] | None = None,
) -> RubricConfig:
    return RubricConfig(
        id=rubric_id,
        enabled=True,
        time=time_str,
        days=days,
        grace_minutes=grace_minutes,
        jitter_minutes=jitter_minutes,
        kind=kind,
        target="main",
        source=None,
        topics_by_weekday=topics_by_weekday or {},
        prompt_overlay=None,
        min_chars=1,
        max_chars=4000,
        image=None,
        history_n=5,
        similarity_threshold=0.7,
        validators=(),
        on_llm_failure=on_llm_failure,
    )


def make_config(
    rubrics: list[RubricConfig],
    *,
    timezone: str = "UTC",
    daily_cap: int = 6,
) -> RubricsConfig:
    return RubricsConfig(
        enabled=True,
        timezone=timezone,
        persona="",
        image_style="",
        targets={"main": TargetConfig(name="main", channel_id=MAIN_CHAT)},
        daily_cap=daily_cap,
        weather_points=(),
        rubrics=tuple(rubrics),
    )


def failed_outcome(
    error: str = "generation failed", error_kind: str = "content"
) -> GenerationOutcome:
    return GenerationOutcome(
        status="failed", error=error, error_kind=error_kind
    )


class FakePipeline:
    """RubricPipeline double: scripted outcomes, every call recorded.

    ``outcomes`` plays back in order and the last one repeats, like
    MockWriter in tests/mocks.py. Without outcomes every attempt succeeds
    with GENERATED_TEXT.
    """

    def __init__(
        self,
        outcomes: list[GenerationOutcome] | None = None,
        fallback: GenerationOutcome | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self._fallback = fallback
        self.generate_calls: list[str] = []
        self.fallback_calls: list[str] = []

    async def generate(
        self, rubric: RubricConfig, run, *, now: datetime
    ) -> GenerationOutcome:
        self.generate_calls.append(rubric.id)
        if not self._outcomes:
            return GenerationOutcome(status="generated", text=GENERATED_TEXT)
        if len(self._outcomes) > 1:
            return self._outcomes.pop(0)
        return self._outcomes[0]

    async def fallback_post(
        self, rubric: RubricConfig, run, *, now: datetime
    ) -> GenerationOutcome:
        self.fallback_calls.append(rubric.id)
        return self._fallback or GenerationOutcome(
            status="failed", error="bank empty"
        )


class FakeSlotPublisher:
    """SlotPublisher double; records every call."""

    def __init__(
        self,
        *,
        result: SlotPublishResult | None = None,
        raise_on_publish: Exception | None = None,
    ) -> None:
        self.result = result or SlotPublishResult(
            success=True, message_id="mid.5000"
        )
        self.raise_on_publish = raise_on_publish
        self.calls: list[dict] = []

    async def publish_slot(
        self,
        text: str,
        *,
        chat_id: int,
        image_ref: str | None = None,
        to_admins: bool = False,
    ) -> SlotPublishResult:
        self.calls.append(
            {
                "text": text,
                "chat_id": chat_id,
                "image_ref": image_ref,
                "to_admins": to_admins,
            }
        )
        if self.raise_on_publish:
            raise self.raise_on_publish
        return self.result


def make_scheduler(
    path: Path,
    config: RubricsConfig,
    *,
    pipeline: FakePipeline | None = None,
    publisher: FakeSlotPublisher | None = None,
    alerter: MockAlerter | None = None,
    **scheduler_kwargs,
) -> SlotScheduler:
    return SlotScheduler(
        config,
        SlotRunRepository(path),
        RubricStateRepository(path),
        pipeline or FakePipeline(),
        publisher or FakeSlotPublisher(),
        alerter or MockAlerter(),
        **scheduler_kwargs,
    )


@pytest.fixture
async def slots_db(tmp_path: Path) -> Path:
    path = tmp_path / "slots.db"
    await Database(path).initialize()
    return path


# --- tick timing and grace (plan section 10) ---


@pytest.mark.asyncio
async def test_before_slot_time_nothing_happens(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.tick(now=_at(DAY, 6, 59))

    assert result.is_quiet()
    assert pipeline.generate_calls == []
    assert await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat()) is None


@pytest.mark.asyncio
async def test_due_slot_within_grace_publishes(slots_db: Path) -> None:
    config = make_config([make_rubric(grace_minutes=60)])
    pipeline = FakePipeline()
    publisher = FakeSlotPublisher()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline, publisher=publisher)

    now = _at(DAY, 7, 30)  # 30 min late, still within grace
    result = await scheduler.tick(now=now)

    assert result.executed == ("good_morning",)
    assert result.published == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "published"
    assert run.text == GENERATED_TEXT
    assert run.attempts == 1
    assert run.target == str(MAIN_CHAT)
    assert run.max_message_id == "mid.5000"
    assert run.published_at == now
    assert publisher.calls == [
        {
            "text": GENERATED_TEXT,
            "chat_id": MAIN_CHAT,
            "image_ref": None,
            "to_admins": False,
        }
    ]


@pytest.mark.asyncio
async def test_slot_time_follows_config_timezone(slots_db: Path) -> None:
    # 07:00 Europe/Moscow is 04:00 UTC: a UTC-only scheduler would fire at
    # the wrong moment and compute the wrong slot_date (plan section 9)
    from zoneinfo import ZoneInfo

    config = make_config([make_rubric()], timezone="Europe/Moscow")
    scheduler = make_scheduler(slots_db, config)

    now = _at(DAY, 4, 6)  # 07:06 MSK
    result = await scheduler.tick(now=now)

    assert result.published == ("good_morning",)
    local_date = now.astimezone(ZoneInfo("Europe/Moscow")).date()
    run = await SlotRunRepository(slots_db).get("good_morning", local_date.isoformat())
    assert run is not None
    assert run.status == "published"


@pytest.mark.asyncio
async def test_slot_past_grace_skipped_missed_grace(slots_db: Path) -> None:
    config = make_config([make_rubric(grace_minutes=60)])
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.tick(now=_at(DAY, 9, 0))

    assert result.skipped == (("good_morning", "missed_grace"),)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "skipped"
    assert run.skip_reason == "missed_grace"
    assert pipeline.generate_calls == []


@pytest.mark.asyncio
async def test_slots_of_one_tick_execute_in_time_order(slots_db: Path) -> None:
    # plan section 2: 07:00 and 07:05 due in the same tick run strictly in
    # time order; jitter must not let weather overtake the morning post
    config = make_config(
        [
            make_rubric("good_morning", time_str="07:00", jitter_minutes=5),
            make_rubric("weather", time_str="07:05", jitter_minutes=2),
        ]
    )
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.tick(now=_at(DAY, 7, 10))

    assert pipeline.generate_calls == ["good_morning", "weather"]
    assert result.published == ("good_morning", "weather")


def test_slot_jitter_is_deterministic_and_bounded() -> None:
    rubric = make_rubric("weather", jitter_minutes=2)

    first = slot_jitter_minutes(rubric, DAY.isoformat())
    second = slot_jitter_minutes(rubric, DAY.isoformat())

    assert first == second
    assert 0 <= first <= 2
    assert slot_jitter_minutes(make_rubric(jitter_minutes=0), DAY.isoformat()) == 0


# --- UNIQUE claim (plan section 10) ---


@pytest.mark.asyncio
async def test_second_tick_never_reruns_claimed_slot(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    await scheduler.tick(now=_at(DAY, 7, 30))
    second = await scheduler.tick(now=_at(DAY, 7, 31))

    assert second.is_quiet()
    assert pipeline.generate_calls == ["good_morning"]


@pytest.mark.asyncio
async def test_previously_claimed_slot_is_not_executed(slots_db: Path) -> None:
    # a row claimed by "another process" (or a crashed predecessor) blocks
    # the UNIQUE(rubric_id, slot_date) insert for this scheduler
    config = make_config([make_rubric()])
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)
    claimed = await SlotRunRepository(slots_db).try_claim(
        "good_morning", DAY.isoformat(), target=str(MAIN_CHAT)
    )
    assert claimed is not None

    result = await scheduler.tick(now=_at(DAY, 7, 30))

    assert result.is_quiet()
    assert pipeline.generate_calls == []
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "pending"  # untouched: not our row


# --- skip reasons (plan section 10) ---


@pytest.mark.asyncio
async def test_paused_rubric_skipped_with_reason(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)
    await RubricStateRepository(slots_db).set_paused("good_morning", True)

    result = await scheduler.tick(now=_at(DAY, 7, 30))

    assert result.skipped == (("good_morning", "rubric_paused"),)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "skipped"
    assert run.skip_reason == "rubric_paused"
    assert pipeline.generate_calls == []


@pytest.mark.asyncio
async def test_generative_without_topic_for_weekday_skipped(slots_db: Path) -> None:
    sunday = DAY
    while sunday.weekday() != 6:  # find a real Sunday for the slot
        sunday += timedelta(days=1)
    config = make_config(
        [
            make_rubric(
                "topic_day",
                kind="generative",
                days=("sun",),
                topics_by_weekday={"mon": ("здоровье",)},
            )
        ]
    )
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.tick(now=_at(sunday, 7, 30))

    assert result.skipped == (("topic_day", "no_topic"),)
    run = await SlotRunRepository(slots_db).get("topic_day", sunday.isoformat())
    assert run is not None
    assert run.skip_reason == "no_topic"
    assert pipeline.generate_calls == []


@pytest.mark.asyncio
async def test_sunday_quiet_day_daytime_rubrics_have_no_slots(
    slots_db: Path,
) -> None:
    # Plan section 2: Sunday is the quiet day - morning, weather and the
    # night post only. The daytime rubrics (topic of the day 12:00, second
    # post 15:30) have no Sunday slots at all: not even a skipped run.
    sunday = DAY
    while sunday.weekday() != 6:  # find a real Sunday
        sunday += timedelta(days=1)
    weekdays = ("mon", "tue", "wed", "thu", "fri", "sat")
    config = make_config(
        [
            make_rubric(
                "topic_day",
                time_str="12:00",
                kind="generative",
                days=weekdays,
                topics_by_weekday={day: ("тема дня",) for day in weekdays},
            ),
            make_rubric("second_post", time_str="15:30", days=weekdays),
            make_rubric("good_night", time_str="21:30"),
        ]
    )
    pipeline = FakePipeline()
    publisher = FakeSlotPublisher()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline, publisher=publisher)

    daytime = await scheduler.tick(now=_at(sunday, 16, 0))  # past both slots

    assert daytime.is_quiet()
    assert pipeline.generate_calls == []
    assert publisher.calls == []
    runs = SlotRunRepository(slots_db)
    assert await runs.get("topic_day", sunday.isoformat()) is None
    assert await runs.get("second_post", sunday.isoformat()) is None

    # the quiet day still runs its daily evening slot
    evening = await scheduler.tick(now=_at(sunday, 21, 35))
    assert evening.published == ("good_night",)


@pytest.mark.asyncio
async def test_daily_cap_exhausted_skips_slot(slots_db: Path) -> None:
    config = make_config(
        [make_rubric("morning", time_str="07:00"), make_rubric("night", time_str="07:02")],
        daily_cap=1,
    )
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.tick(now=_at(DAY, 7, 5))

    assert result.published == ("morning",)
    assert result.skipped == (("night", "daily_cap"),)
    night = await SlotRunRepository(slots_db).get("night", DAY.isoformat())
    assert night is not None
    assert night.status == "skipped"
    assert night.skip_reason == "daily_cap"


def _grid_plus_smoke_stub() -> list[RubricConfig]:
    """The shipped grid of 6 (plan section 2) plus the smoke stub: the
    7-slot day a shakedown config runs before the stub is disabled."""
    weekdays = ("mon", "tue", "wed", "thu", "fri", "sat")
    return [
        make_rubric("good_morning", time_str="07:00", grace_minutes=60),
        make_rubric("weather", time_str="07:05", grace_minutes=30),
        make_rubric("smoke_test", time_str="10:07", grace_minutes=60),
        make_rubric(
            "topic_day",
            time_str="12:00",
            kind="generative",
            days=weekdays,
            grace_minutes=30,
            topics_by_weekday={day: ("тема дня",) for day in weekdays},
        ),
        make_rubric("second_post", time_str="15:30", days=weekdays, grace_minutes=30),
        make_rubric("good_news", time_str="19:00", kind="digest", grace_minutes=20),
        make_rubric("good_night", time_str="21:30", kind="generative", grace_minutes=60),
    ]


async def _run_the_day(scheduler: SlotScheduler) -> tuple[list[str], list[tuple[str, str]]]:
    """Tick through the day so every slot fires within its own grace."""
    published: list[str] = []
    skipped: list[tuple[str, str]] = []
    for hour, minute in ((7, 10), (10, 10), (12, 10), (15, 40), (19, 10), (22, 0)):
        result = await scheduler.tick(now=_at(DAY, hour, minute))
        published.extend(result.published)
        skipped.extend(result.skipped)
    return published, skipped


@pytest.mark.asyncio
async def test_seven_slots_at_cap6_dry_run_keeps_the_night_slot(slots_db: Path) -> None:
    # plan section 14 stage 1: a shakedown adds the smoke stub as a 7th
    # slot over the 6-slot grid while daily_cap stays 6. Dry-run previews
    # go to the admins, not the channel, so they must not consume the cap -
    # otherwise the last slot of the day (good_night 21:30) is cut on
    # every working day and the shakedown never exercises it
    config = make_config(_grid_plus_smoke_stub(), daily_cap=6)
    publisher = FakeSlotPublisher()
    scheduler = make_scheduler(slots_db, config, publisher=publisher, dry_run=True)

    published, skipped = await _run_the_day(scheduler)

    assert skipped == []
    assert published == [
        "good_morning",
        "weather",
        "smoke_test",
        "topic_day",
        "second_post",
        "good_news",
        "good_night",
    ]
    assert len(publisher.calls) == 7
    assert all(call["to_admins"] is True for call in publisher.calls)
    night = await SlotRunRepository(slots_db).get("good_night", DAY.isoformat())
    assert night is not None
    assert night.status == "published"
    assert night.is_dry_run is True


@pytest.mark.asyncio
async def test_seven_slots_at_cap6_live_cuts_the_seventh_slot(slots_db: Path) -> None:
    # live publishing keeps daily_cap as the safety net (plan section 2):
    # with 7 enabled slots the 7th of the day is claimed and skipped, not
    # published over the cap. The shipped config avoids this state by
    # keeping the smoke stub disabled - see test_rubrics_config
    config = make_config(_grid_plus_smoke_stub(), daily_cap=6)
    scheduler = make_scheduler(slots_db, config)

    published, skipped = await _run_the_day(scheduler)

    assert len(published) == 6
    assert skipped == [("good_night", "daily_cap")]
    night = await SlotRunRepository(slots_db).get("good_night", DAY.isoformat())
    assert night is not None
    assert night.status == "skipped"
    assert night.skip_reason == "daily_cap"


# --- generation chain (plan section 7) ---


@pytest.mark.asyncio
async def test_generation_retries_then_succeeds(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    pipeline = FakePipeline(
        outcomes=[failed_outcome(), GenerationOutcome(status="generated", text="v2")]
    )
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.tick(now=_at(DAY, 7, 30))

    assert pipeline.generate_calls == ["good_morning", "good_morning"]
    assert result.published == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "published"
    assert run.text == "v2"
    assert run.attempts == 2


@pytest.mark.asyncio
async def test_generation_exhausted_falls_back_to_bank_post(slots_db: Path) -> None:
    config = make_config([make_rubric(on_llm_failure="bank")])
    pipeline = FakePipeline(
        outcomes=[failed_outcome()],  # repeats: 3 attempts, all fail
        fallback=GenerationOutcome(status="generated", text=BANK_TEXT),
    )
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.tick(now=_at(DAY, 7, 30))

    assert pipeline.generate_calls == ["good_morning"] * 3
    assert pipeline.fallback_calls == ["good_morning"]
    assert result.published == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "published"
    assert run.text == BANK_TEXT
    assert run.attempts == 3


@pytest.mark.asyncio
async def test_generation_exhausted_without_bank_policy_fails_and_alerts(
    slots_db: Path,
) -> None:
    config = make_config([make_rubric(on_llm_failure="skip")])
    pipeline = FakePipeline(outcomes=[failed_outcome()])
    alerter = MockAlerter()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline, alerter=alerter)

    result = await scheduler.tick(now=_at(DAY, 7, 30))

    assert pipeline.generate_calls == ["good_morning"] * 3
    assert pipeline.fallback_calls == []
    assert result.failed == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "failed"
    assert run.attempts == 3
    assert run.error == "generation failed"
    assert any("Rubric slot failed" in alert for alert in alerter.alerts)


@pytest.mark.asyncio
async def test_config_error_does_not_regenerate(slots_db: Path) -> None:
    config = make_config([make_rubric(on_llm_failure="bank")])
    pipeline = FakePipeline(
        outcomes=[
            failed_outcome(error="401 Unauthorized", error_kind="config")
        ],
        fallback=GenerationOutcome(status="generated", text=BANK_TEXT),
    )
    alerter = MockAlerter()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline, alerter=alerter)

    result = await scheduler.tick(now=_at(DAY, 7, 30))

    assert pipeline.generate_calls == ["good_morning"]  # no point retrying a 401
    assert result.published == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.text == BANK_TEXT
    assert any("LLM config" in alert for alert in alerter.alerts)


# --- restart recovery semantics (plan section 10) ---


@pytest.mark.asyncio
async def test_startup_pending_within_grace_is_rerun(slots_db: Path) -> None:
    config = make_config([make_rubric(grace_minutes=60)])
    runs = SlotRunRepository(slots_db)
    crashed = await runs.try_claim(
        "good_morning", DAY.isoformat(), target=str(MAIN_CHAT)
    )
    assert crashed is not None
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.startup_recover(now=_at(DAY, 7, 30))

    assert pipeline.generate_calls == ["good_morning"]
    assert result.published == ("good_morning",)
    run = await runs.get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "published"


@pytest.mark.asyncio
async def test_startup_pending_past_grace_skipped(slots_db: Path) -> None:
    config = make_config([make_rubric(grace_minutes=60)])
    runs = SlotRunRepository(slots_db)
    crashed = await runs.try_claim(
        "good_morning", DAY.isoformat(), target=str(MAIN_CHAT)
    )
    assert crashed is not None
    pipeline = FakePipeline()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    result = await scheduler.startup_recover(now=_at(DAY, 9, 0))

    assert pipeline.generate_calls == []
    assert result.skipped == (("good_morning", "missed_grace"),)
    run = await runs.get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "skipped"
    assert run.skip_reason == "missed_grace"


@pytest.mark.asyncio
async def test_startup_generated_publishes_saved_text_without_llm(
    slots_db: Path,
) -> None:
    config = make_config([make_rubric()])
    runs = SlotRunRepository(slots_db)
    crashed = await runs.try_claim(
        "good_morning", DAY.isoformat(), target=str(MAIN_CHAT)
    )
    assert crashed is not None
    await runs.save(
        replace(crashed, status="generated", text="Сохранённый текст поста")
    )
    pipeline = FakePipeline()
    publisher = FakeSlotPublisher()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline, publisher=publisher)

    result = await scheduler.startup_recover(now=_at(DAY, 7, 30))

    assert pipeline.generate_calls == []  # no repeated LLM call
    assert result.published == ("good_morning",)
    assert publisher.calls[0]["text"] == "Сохранённый текст поста"
    run = await runs.get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "published"


@pytest.mark.asyncio
async def test_startup_publishing_becomes_unknown_delivery_with_alert(
    slots_db: Path,
) -> None:
    config = make_config([make_rubric()])
    runs = SlotRunRepository(slots_db)
    crashed = await runs.try_claim(
        "good_morning", DAY.isoformat(), target=str(MAIN_CHAT)
    )
    assert crashed is not None
    await runs.save(
        replace(crashed, status="publishing", text="Возможно, ушло в канал")
    )
    pipeline = FakePipeline()
    publisher = FakeSlotPublisher()
    alerter = MockAlerter()
    scheduler = make_scheduler(
        slots_db, config, pipeline=pipeline, publisher=publisher, alerter=alerter
    )

    result = await scheduler.startup_recover(now=_at(DAY, 7, 30))

    assert publisher.calls == []  # never re-sent blindly
    assert pipeline.generate_calls == []
    assert result.unknown_delivery == ("good_morning",)
    run = await runs.get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "unknown_delivery"
    assert len(alerter.alerts) == 1
    assert "unknown" in alerter.alerts[0].lower()


# --- breakers (plan section 11) ---


@pytest.mark.asyncio
async def test_publish_infra_failure_global_pause_with_retry(
    slots_db: Path,
) -> None:
    base = _anchored_now()
    slot_time = (base - timedelta(seconds=30)).strftime("%H:%M")
    slot_date = base.date().isoformat()
    config = make_config([make_rubric(grace_minutes=120)])
    config = replace(config, rubrics=(make_rubric(time_str=slot_time, grace_minutes=120),))
    pipeline = FakePipeline()
    publisher = FakeSlotPublisher(
        result=SlotPublishResult(
            success=False, error="MaxApiError: 503", error_kind="infra"
        )
    )
    alerter = MockAlerter()
    scheduler = make_scheduler(
        slots_db,
        config,
        pipeline=pipeline,
        publisher=publisher,
        alerter=alerter,
        stale_minutes=0,
        global_pause_retry_seconds=60.0,
    )

    # (1) the send fails as infra: the text is kept, the pause trips, alert
    result1 = await scheduler.tick(now=base)
    assert result1.executed == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", slot_date)
    assert run is not None
    assert run.status == "generated"
    assert run.text == GENERATED_TEXT
    assert len(alerter.alerts) == 1
    assert "globally paused" in alerter.alerts[0]

    # (2) while paused the scheduler is idle and claims nothing
    result2 = await scheduler.tick(now=base + timedelta(seconds=10))
    assert result2.globally_paused is True
    assert len(publisher.calls) == 1

    # (3) after the retry deadline the saved text is re-sent, no new LLM call
    publisher.result = SlotPublishResult(success=True, message_id="mid.7")
    result3 = await scheduler.tick(now=base + timedelta(seconds=70))
    assert pipeline.generate_calls == ["good_morning"]  # exactly one LLM round
    run = await SlotRunRepository(slots_db).get("good_morning", slot_date)
    assert run is not None
    assert run.status == "published"
    assert run.max_message_id == "mid.7"
    assert publisher.calls[-1]["text"] == GENERATED_TEXT
    assert len(alerter.recoveries) == 1


@pytest.mark.asyncio
async def test_publish_content_failure_fails_slot(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    publisher = FakeSlotPublisher(
        result=SlotPublishResult(
            success=False, error="400 message too long", error_kind="content"
        )
    )
    alerter = MockAlerter()
    scheduler = make_scheduler(slots_db, config, publisher=publisher, alerter=alerter)

    result = await scheduler.tick(now=_at(DAY, 7, 30))

    assert result.failed == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "failed"
    assert run.error == "400 message too long"
    assert any("publish rejected" in alert for alert in alerter.alerts)


@pytest.mark.asyncio
async def test_consecutive_failures_pause_rubric(slots_db: Path) -> None:
    config = make_config([make_rubric(on_llm_failure="skip")])
    pipeline = FakePipeline(outcomes=[failed_outcome()])
    alerter = MockAlerter()
    scheduler = make_scheduler(
        slots_db, config, pipeline=pipeline, alerter=alerter, pause_after_failures=2
    )
    runs = SlotRunRepository(slots_db)

    # day 1 and day 2: the slot fails all attempts
    await scheduler.tick(now=_at(DAY, 7, 30))
    await scheduler.tick(now=_at(DAY2, 7, 30))
    state = await RubricStateRepository(slots_db).get("good_morning")
    assert state.paused is True
    assert any("paused automatically" in alert for alert in alerter.alerts)

    # day 3: the paused rubric no longer calls the pipeline
    calls_before = len(pipeline.generate_calls)
    result = await scheduler.tick(now=_at(DAY3, 7, 30))
    assert len(pipeline.generate_calls) == calls_before
    assert result.skipped == (("good_morning", "rubric_paused"),)
    run = await runs.get("good_morning", DAY3.isoformat())
    assert run is not None
    assert run.skip_reason == "rubric_paused"


@pytest.mark.asyncio
async def test_source_breaker_excludes_after_three_empty_responses() -> None:
    alerter = MockAlerter()
    breaker = SourceHealthBreaker(alerter, limit=3)

    await breaker.record("culture_feed", 0)
    await breaker.record("culture_feed", 0)
    assert breaker.is_excluded("culture_feed") is False
    assert alerter.alerts == []

    await breaker.record("culture_feed", 0)
    assert breaker.is_excluded("culture_feed") is True
    assert breaker.excluded == frozenset({"culture_feed"})
    assert len(alerter.alerts) == 1
    assert "culture_feed" in alerter.alerts[0]

    # a non-empty answer resets the streak of another feed
    await breaker.record("science_feed", 5)
    assert breaker.empty_streak("science_feed") == 0


@pytest.mark.asyncio
async def test_scheduler_records_source_stats_from_outcomes(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    pipeline = FakePipeline(
        outcomes=[
            GenerationOutcome(
                status="generated",
                text=GENERATED_TEXT,
                source_stats=(SourceStat(source="culture_feed", items=0),),
            )
        ]
    )
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline)

    await scheduler.tick(now=_at(DAY, 7, 30))

    assert scheduler.sources.empty_streak("culture_feed") == 1
    assert scheduler.sources.is_excluded("culture_feed") is False


# --- dry run ---


@pytest.mark.asyncio
async def test_dry_run_publishes_to_admins(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    publisher = FakeSlotPublisher()
    scheduler = make_scheduler(slots_db, config, publisher=publisher, dry_run=True)

    await scheduler.tick(now=_at(DAY, 7, 30))

    assert publisher.calls[0]["to_admins"] is True
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.is_dry_run is True
    assert run.status == "published"


# --- manual slot run (/factory run <rubric>, plan section 11) ---


@pytest.mark.asyncio
async def test_run_slot_now_executes_before_scheduled_time(slots_db: Path) -> None:
    config = make_config([make_rubric()])
    pipeline = FakePipeline()
    publisher = FakeSlotPublisher()
    scheduler = make_scheduler(slots_db, config, pipeline=pipeline, publisher=publisher)

    # 06:00 is before the 07:00 slot: the manual lever still runs today's slot
    result = await scheduler.run_slot_now("good_morning", now=_at(DAY, 6, 0))

    assert result is not None
    assert result.executed == ("good_morning",)
    assert result.published == ("good_morning",)
    run = await SlotRunRepository(slots_db).get("good_morning", DAY.isoformat())
    assert run is not None
    assert run.status == "published"
    assert pipeline.generate_calls == ["good_morning"]


@pytest.mark.asyncio
async def test_run_slot_now_unknown_rubric_returns_none(slots_db: Path) -> None:
    scheduler = make_scheduler(slots_db, make_config([make_rubric()]))

    assert await scheduler.run_slot_now("nosuch", now=_at(DAY, 6, 0)) is None


@pytest.mark.asyncio
async def test_run_slot_now_respects_daily_cap(slots_db: Path) -> None:
    config = make_config(
        [make_rubric("good_morning"), make_rubric("weather", kind="weather")],
        daily_cap=1,
    )
    scheduler = make_scheduler(slots_db, config)

    first = await scheduler.run_slot_now("good_morning", now=_at(DAY, 6, 0))
    second = await scheduler.run_slot_now("weather", now=_at(DAY, 6, 5))

    assert first is not None and first.published == ("good_morning",)
    # daily_cap is the safety net of manual runs (plan section 2)
    assert second is not None
    assert second.skipped == (("weather", "daily_cap"),)
    run = await SlotRunRepository(slots_db).get("weather", DAY.isoformat())
    assert run is not None
    assert run.status == "skipped"
    assert run.skip_reason == "daily_cap"


# --- loop heartbeat (plan section 11 watchdog) ---


@pytest.mark.asyncio
async def test_scheduler_loop_writes_heartbeat_event(slots_db: Path) -> None:
    db = Database(slots_db)
    scheduler = make_scheduler(slots_db, make_config([make_rubric()]))

    task = asyncio.create_task(slot_scheduler_loop(scheduler, db))
    try:
        # the first heartbeat lands with the first tick; the real clock of
        # the scheduler decides quiet/busy, so only the event itself is
        # asserted here
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    last = await db.get_last_event("slot_heartbeat")
    assert last is not None
    assert isinstance(last["details"], str)
