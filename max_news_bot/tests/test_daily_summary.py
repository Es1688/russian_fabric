"""Daily summary of the rubrics contour (plan section 11): message body,
LLM spend estimate, bank reserves and the once-a-day delivery window."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from max_news_bot.bot.daily_summary import (
    DailySummaryService,
    parse_summary_time,
)
from max_news_bot.config.rubrics import DEFAULT_SUMMARY_TIME, WEEKDAYS
from max_news_bot.core.models import ContentBankItem
from max_news_bot.tests.test_slot_scheduler import make_config, make_rubric
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    FilterLogRepository,
    SlotRunRepository,
)


MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
FRIDAY = date(2026, 10, 9)
SUNDAY = date(2026, 10, 11)
SLOT_DATE = FRIDAY.isoformat()


@pytest.fixture
async def summary_db(tmp_path: Path) -> SimpleNamespace:
    path = tmp_path / "summary.db"
    db = Database(path)
    await db.initialize()
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    return SimpleNamespace(
        path=path,
        db=db,
        runs=SlotRunRepository(path),
        bank=ContentBankRepository(path),
        filters=FilterLogRepository(path),
        sent=sent,
        send=send,
    )


def _service(
    env: SimpleNamespace,
    *,
    summary_time: str = "22:40",
    rubrics: list | None = None,
) -> DailySummaryService:
    config = make_config(
        rubrics
        if rubrics is not None
        else [
            make_rubric("good_morning"),
            make_rubric("weather", kind="weather", on_llm_failure="skip"),
            make_rubric("good_news", kind="digest"),
            make_rubric("good_night", kind="generative"),
        ],
        timezone="Europe/Moscow",
    )
    config = replace(config, summary_time=summary_time)
    return DailySummaryService(
        config,
        env.runs,
        env.bank,
        env.filters,
        env.db,
        env.send,
    )


async def _claim(
    env: SimpleNamespace, rubric_id: str, status: str, **overrides
) -> None:
    run = await env.runs.try_claim(rubric_id, SLOT_DATE)
    assert run is not None
    await env.runs.save(replace(run, status=status, **overrides))


def _log_stats_event(env: SimpleNamespace, event: str, created_at: str) -> None:
    # bot_stats row with an explicit UTC created_at (log_event would stamp
    # it "now", which is outside the tested summary day)
    with sqlite3.connect(env.path) as conn:
        conn.execute(
            "INSERT INTO bot_stats (event, details, created_at) VALUES (?, '', ?)",
            (event, created_at),
        )
        conn.commit()


# --- message body -----------------------------------------------------------------


async def test_build_text_reports_the_full_day(summary_db) -> None:
    await _claim(summary_db, "good_morning", "published", attempts=1)
    await _claim(
        summary_db, "weather", "skipped", skip_reason="missed_grace"
    )
    await _claim(
        summary_db,
        "good_news",
        "failed",
        attempts=3,
        error="LlmError: <b>boom</b> & more",
    )
    night_run = await summary_db.runs.try_claim("good_night", SLOT_DATE)
    assert night_run is not None
    await summary_db.runs.save(
        replace(
            night_run,
            status="unknown_delivery",
            error="interrupted while publishing",
        )
    )

    text = await _service(summary_db).build_text(SLOT_DATE)

    assert text.splitlines()[0] == f"📋 Сводка канала за {SLOT_DATE} (пт)"
    assert "Вышло: 1 из 4 запланированных" in text
    assert "- good_morning — опубликовано" in text
    assert "Пропущено: 1" in text
    assert "- weather — missed_grace" in text
    assert "Сбои: 1" in text
    # dynamic values are html-escaped (replies go as html)
    assert "LlmError: &lt;b&gt;boom&lt;/b&gt; &amp; more" in text
    assert "<b>boom</b>" not in text
    assert "Доставка неизвестна: 1" in text
    assert f"/factory retract slot {night_run.id}" in text


async def test_build_text_marks_dry_run_and_in_flight(summary_db) -> None:
    run = await summary_db.runs.try_claim("good_morning", SLOT_DATE, is_dry_run=True)
    assert run is not None
    await summary_db.runs.save(replace(run, status="published"))
    await _claim(summary_db, "good_night", "generated")

    text = await _service(summary_db).build_text(SLOT_DATE)

    assert "- good_morning — опубликовано (dry-run превью)" in text
    assert "Не завершено к моменту сводки: 1" in text
    assert "- good_night — generated" in text


async def test_build_text_planned_counts_only_that_weekday(summary_db) -> None:
    # Sunday: rubrics restricted to mon-sat are not planned (plan section 2)
    config_rubrics = [
        make_rubric("good_morning"),  # every day
        make_rubric("topic_day", days=tuple(WEEKDAYS[:6])),  # mon-sat
    ]
    await _claim_runs_for(summary_db, SUNDAY.isoformat(), ["good_morning"])

    service = _service(summary_db, rubrics=config_rubrics)
    text = await service.build_text(SUNDAY.isoformat())

    assert "Вышло: 1 из 1 запланированных" in text
    assert "(вс)" in text.splitlines()[0]


async def _claim_runs_for(
    env: SimpleNamespace, slot_date: str, rubric_ids: list[str]
) -> None:
    for rubric_id in rubric_ids:
        run = await env.runs.try_claim(rubric_id, slot_date)
        assert run is not None
        await env.runs.save(replace(run, status="published"))


async def test_build_text_bank_reserves_with_low_watermark(summary_db) -> None:
    for index in range(31):
        await summary_db.bank.add(
            ContentBankItem(
                id=None,
                item_type="thought",
                text=f"thought {index}",
                review_status="approved",
            )
        )
    for index in range(2):
        await summary_db.bank.add(
            ContentBankItem(
                id=None,
                item_type="reserve",
                text=f"reserve {index}",
                review_status="approved",
            )
        )
    # pending review: not counted as a reserve
    await summary_db.bank.add(
        ContentBankItem(
            id=None, item_type="thought", text="pending thought",
            review_status="pending",
        )
    )

    text = await _service(summary_db).build_text(SLOT_DATE)

    # plan section 6: types below the 30-day stock get flagged
    assert "Банк (одобрено): reserve=2 ⚠ менее 30, thought=31" in text


async def test_build_text_bank_empty_reports_politely(summary_db) -> None:
    text = await _service(summary_db).build_text(SLOT_DATE)

    assert "Банк контента: пусто" in text


async def test_build_text_llm_estimate_counts_attempts_and_filters(
    summary_db,
) -> None:
    # attempts of LLM-kind rubrics (digest/generative) only
    await _claim(summary_db, "good_morning", "published", attempts=2)  # bank
    await _claim(summary_db, "good_news", "failed", attempts=3)  # digest
    await _claim(summary_db, "good_night", "published", attempts=1)  # generative

    # filter_log rows inside the local day window (2026-10-09 MSK is
    # 2026-10-08 21:00 .. 2026-10-09 21:00 UTC)
    with sqlite3.connect(summary_db.path) as conn:
        conn.executemany(
            "INSERT INTO filter_log (stage, decision, created_at) "
            "VALUES ('llm_classifier', 'passed', ?)",
            [
                ("2026-10-09 10:00:00",),
                ("2026-10-09 12:30:00",),
                ("2026-10-10 10:00:00",),  # next local day: not counted
            ],
        )
        conn.commit()

    text = await _service(summary_db).build_text(SLOT_DATE)

    # 3 digest attempts + 1 generative attempt are LLM calls; the bank
    # rubric's 2 attempts are not
    assert "LLM-вызовы (оценка): 6 — слоты 4, фильтры 2" in text


# --- channel counters line (plan section 2.6) ---------------------------------------


async def test_build_text_channel_line_counts_events_of_the_day(
    summary_db,
) -> None:
    # local day 2026-10-09 (MSK) = [2026-10-08 21:00, 2026-10-09 21:00) UTC
    inside = "2026-10-09 10:00:00"
    for event, count in (
        ("llm_primary_ok", 2),
        ("llm_fallback_used", 1),
        ("image_fal_ok", 1),
        ("image_none", 3),
    ):
        for _ in range(count):
            _log_stats_event(summary_db, event, inside)
    _log_stats_event(summary_db, "publish", inside)  # unrelated event

    text = await _service(summary_db).build_text(SLOT_DATE)

    lines = text.splitlines()
    estimate = next(
        index for index, line in enumerate(lines) if line.startswith("LLM-вызовы")
    )
    assert lines[estimate + 1] == (
        "LLM: primary 2 / fallback 1; картинки: fal 1 / без картинки 3"
    )


async def test_build_text_channel_line_ignores_other_dates(summary_db) -> None:
    # just before the local day starts ...
    _log_stats_event(summary_db, "llm_primary_ok", "2026-10-08 20:59:59")
    _log_stats_event(summary_db, "image_none", "2026-10-08 12:00:00")
    # ... and at/after the moment it ends: none of them count
    _log_stats_event(summary_db, "llm_fallback_used", "2026-10-09 21:00:00")
    _log_stats_event(summary_db, "image_fal_ok", "2026-10-10 12:00:00")

    text = await _service(summary_db).build_text(SLOT_DATE)

    assert "LLM: primary 0 / fallback 0; картинки: fal 0 / без картинки 0" in text


async def test_build_text_channel_line_with_zero_stats_is_meaningful(
    summary_db,
) -> None:
    text = await _service(summary_db).build_text(SLOT_DATE)

    assert "LLM: primary 0 / fallback 0; картинки: fal 0 / без картинки 0" in text


# --- delivery window and once-a-day rule -------------------------------------------


async def test_send_if_due_fires_inside_window_once(summary_db) -> None:
    service = _service(summary_db)

    before = datetime(2026, 10, 10, 22, 39, tzinfo=MSK)
    assert await service.send_if_due(now=before) is False

    due = datetime(2026, 10, 10, 22, 41, tzinfo=MSK)
    assert await service.send_if_due(now=due) is True
    assert len(summary_db.sent) == 1
    assert "Сводка канала за 2026-10-10" in summary_db.sent[0]

    event = await summary_db.db.get_last_event("daily_summary")
    assert event is not None
    assert event["details"] == "2026-10-10"

    # a restart inside the window must not duplicate the message
    again = datetime(2026, 10, 10, 22, 45, tzinfo=MSK)
    assert await service.send_if_due(now=again) is False
    assert len(summary_db.sent) == 1


async def test_send_if_due_ignores_window_before_and_after(summary_db) -> None:
    service = _service(summary_db)

    late = datetime(2026, 10, 10, 23, 0, tzinfo=MSK)  # past the grace
    assert await service.send_if_due(now=late) is False

    morning = datetime(2026, 10, 10, 9, 0, tzinfo=MSK)
    assert await service.send_if_due(now=morning) is False
    assert summary_db.sent == []


async def test_send_if_due_fires_again_next_day(summary_db) -> None:
    service = _service(summary_db)

    assert await service.send_if_due(
        now=datetime(2026, 10, 10, 22, 41, tzinfo=MSK)
    )
    assert await service.send_if_due(
        now=datetime(2026, 10, 11, 22, 41, tzinfo=MSK)
    )

    assert len(summary_db.sent) == 2
    assert "за 2026-10-11" in summary_db.sent[1]


async def test_send_if_due_uses_configured_summary_time(summary_db) -> None:
    service = _service(summary_db, summary_time="09:30")

    too_early = datetime(2026, 10, 10, 9, 29, tzinfo=MSK)
    assert await service.send_if_due(now=too_early) is False

    on_time = datetime(2026, 10, 10, 9, 31, tzinfo=MSK)
    assert await service.send_if_due(now=on_time) is True


# --- helpers ------------------------------------------------------------------------


def test_parse_summary_time_valid_and_invalid() -> None:
    assert parse_summary_time("22:40") == (22, 40)
    assert parse_summary_time("00:00") == (0, 0)
    with pytest.raises(ValueError):
        parse_summary_time("24:00")
    with pytest.raises(ValueError):
        parse_summary_time("9:99")


def test_default_summary_time_is_2240() -> None:
    # plan section 11: the fixed summary slot, after the last 21:30 post
    assert DEFAULT_SUMMARY_TIME == "22:40"
