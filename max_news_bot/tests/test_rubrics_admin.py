"""Rubric-argument extensions of /factory (plan section 11) and their
backward compatibility with the legacy command family.

Command paths go through the real dispatcher with a real SlotScheduler
over a tmp database; generation and publishing are the fakes borrowed
from test_slot_scheduler.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from max_news_bot.bot.handlers import create_dispatcher
from max_news_bot.bot.rubrics_admin import RubricsAdmin, format_tick_result
from max_news_bot.bot.slot_scheduler import TickResult, SlotScheduler
from max_news_bot.config.loader import AppConfig
from max_news_bot.tests.mocks import MockAlerter
from max_news_bot.tests.test_handlers import CHAT_ID, RecordingClient, make_update
from max_news_bot.tests.test_slot_scheduler import (
    FakePipeline,
    FakeSlotPublisher,
    MAIN_CHAT,
    make_config,
    make_rubric,
)
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    RubricStateRepository,
    SlotRunRepository,
)


UTC = timezone.utc
DAY = date(2026, 10, 9)  # Friday
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _texts(setup: SimpleNamespace) -> list[str]:
    return [text for _, text, _ in setup.client.sent]


def _last_text(setup: SimpleNamespace) -> str:
    return _texts(setup)[-1]


async def _invoke(
    setup: SimpleNamespace, text: str, *, user_id: int | None = None
) -> None:
    await setup.handle_update(
        make_update(text, user_id=user_id or setup.app_config.admin_ids[0])
    )


def _make_admin(
    path: Path,
    db: Database,
    *,
    rubrics: list | None = None,
    pipeline: FakePipeline | None = None,
    publisher: FakeSlotPublisher | None = None,
    daily_cap: int = 6,
) -> RubricsAdmin:
    config = make_config(
        rubrics
        if rubrics is not None
        else [
            make_rubric("good_morning"),
            make_rubric("weather", kind="weather", on_llm_failure="skip"),
        ],
        daily_cap=daily_cap,
    )
    scheduler = SlotScheduler(
        config,
        SlotRunRepository(path),
        RubricStateRepository(path),
        pipeline or FakePipeline(),
        publisher or FakeSlotPublisher(),
        MockAlerter(),
        clock=lambda: NOW,
    )
    return RubricsAdmin(
        config,
        scheduler,
        SlotRunRepository(path),
        RubricStateRepository(path),
        db,
        clock=lambda: NOW,
    )


@pytest.fixture
async def admin_setup(tmp_path: Path, app_config: AppConfig) -> SimpleNamespace:
    path = tmp_path / "admin.db"
    db = Database(path)
    await db.initialize()
    client = RecordingClient()
    admin = _make_admin(path, db)
    handle_update = create_dispatcher(
        app_config,
        db,
        datetime.now(timezone.utc),
        client=client,
        rubrics_admin=admin,
    )
    return SimpleNamespace(
        handle_update=handle_update,
        client=client,
        db=db,
        app_config=app_config,
        admin=admin,
        path=path,
    )


# --- /factory pause|resume with a rubric argument -------------------------------


async def test_factory_pause_rubric_persists_state(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause weather")

    assert "Rubric weather paused" in _last_text(admin_setup)
    states = RubricStateRepository(admin_setup.path)
    assert (await states.get("weather")).paused is True
    assert (await states.get("good_morning")).paused is False
    event = await admin_setup.db.get_last_event("rubric_pause")
    assert event is not None and "rubric=weather" in event["details"]


async def test_factory_pause_all_pauses_every_rubric(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause all")

    assert "All rubrics paused (2)" in _last_text(admin_setup)
    states = RubricStateRepository(admin_setup.path)
    for rubric_id in ("good_morning", "weather"):
        assert (await states.get(rubric_id)).paused is True


async def test_factory_resume_rubric_clears_pause(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause weather")
    await _invoke(admin_setup, "/factory resume weather")

    assert _last_text(admin_setup) == "Rubric weather resumed."
    states = RubricStateRepository(admin_setup.path)
    assert (await states.get("weather")).paused is False


async def test_factory_resume_all_clears_every_pause(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause all")
    await _invoke(admin_setup, "/factory resume all")

    assert "All rubrics resumed (2)" in _last_text(admin_setup)
    states = RubricStateRepository(admin_setup.path)
    for rubric_id in ("good_morning", "weather"):
        assert (await states.get(rubric_id)).paused is False


async def test_factory_unknown_rubric_lists_configured(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause nosuch")

    assert "Unknown rubric: nosuch" in _last_text(admin_setup)
    assert "good_morning, weather" in _last_text(admin_setup)


async def test_factory_resume_unknown_rubric_lists_configured(admin_setup) -> None:
    await _invoke(admin_setup, "/factory resume nosuch")

    assert "Unknown rubric: nosuch" in _last_text(admin_setup)


# --- /factory run <rubric> and bare /factory run in the rubrics contour ----------


async def test_factory_run_rubric_executes_today_slot(admin_setup) -> None:
    await _invoke(admin_setup, "/factory run good_morning")

    texts = _texts(admin_setup)
    assert "Running rubric good_morning slot now..." in texts
    assert "Done: executed=1 published=1 skipped=0 failed=0" in texts[-1]
    run = await SlotRunRepository(admin_setup.path).get(
        "good_morning", DAY.isoformat()
    )
    assert run is not None and run.status == "published"
    assert run.target == str(MAIN_CHAT)


async def test_factory_run_rubric_twice_a_day_is_unique_claimed(
    admin_setup,
) -> None:
    await _invoke(admin_setup, "/factory run good_morning")
    await _invoke(admin_setup, "/factory run good_morning")

    # UNIQUE(rubric_id, slot_date): the second manual run finds nothing to do
    assert "executed=0" in _last_text(admin_setup)


async def test_factory_run_rubric_paused_reports_skip_reason(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause good_morning")
    await _invoke(admin_setup, "/factory run good_morning")

    assert "skipped=1" in _last_text(admin_setup)
    assert "good_morning (rubric_paused)" in _last_text(admin_setup)


async def test_factory_run_rubric_respects_daily_cap(tmp_path, app_config) -> None:
    path = tmp_path / "cap.db"
    db = Database(path)
    await db.initialize()
    client = RecordingClient()
    admin = _make_admin(
        path,
        db,
        rubrics=[make_rubric("good_morning"), make_rubric("weather", kind="weather")],
        daily_cap=1,
    )
    handle_update = create_dispatcher(
        app_config,
        db,
        datetime.now(timezone.utc),
        client=client,
        rubrics_admin=admin,
    )
    setup = SimpleNamespace(
        handle_update=handle_update, client=client, app_config=app_config
    )

    await _invoke(setup, "/factory run good_morning")
    await _invoke(setup, "/factory run weather")

    # daily_cap is the safety net for manual runs (plan section 2)
    last = [text for _, text, _ in client.sent][-1]
    assert "weather (daily_cap)" in last


async def test_factory_bare_run_ticks_scheduler(admin_setup) -> None:
    # bare /factory run = one immediate scheduler tick; 12:00 UTC is past
    # the 07:00 slot + grace: the tick records a missed_grace skip
    await _invoke(admin_setup, "/factory run")

    texts = _texts(admin_setup)
    assert "Running scheduler tick..." in texts
    assert "Scheduler tick done" in texts[-1] or "Done:" in texts[-1]


async def test_factory_run_unknown_rubric_lists_configured(admin_setup) -> None:
    await _invoke(admin_setup, "/factory run nosuch")

    assert "Unknown rubric: nosuch" in _last_text(admin_setup)


# --- /factory status with the rubrics overview -----------------------------------


async def test_factory_status_appends_rubrics_overview(admin_setup) -> None:
    await _invoke(admin_setup, "/factory run good_morning")
    await _invoke(admin_setup, "/factory pause weather")
    await _invoke(admin_setup, "/factory status")

    status = _last_text(admin_setup)
    assert "Rubrics: enabled, timezone UTC, daily_cap 6" in status
    assert "Global MAX pause: no" in status
    assert "- good_morning (bank, 07:00): active; today: published" in status
    assert "- weather (weather, 07:00): paused; today: not run yet" in status


# --- /factory retract slot --------------------------------------------------------


async def test_factory_retract_slot_deletes_channel_message(admin_setup) -> None:
    runs = SlotRunRepository(admin_setup.path)
    run = await runs.try_claim("good_morning", DAY.isoformat())
    assert run is not None
    await runs.save(
        replace(run, status="published", max_message_id="mid.4242")
    )

    await _invoke(admin_setup, f"/factory retract slot {run.id}")

    assert admin_setup.client.deleted == ["mid.4242"]
    assert "message retracted" in _last_text(admin_setup)
    event = await admin_setup.db.get_last_event("slot_retract")
    assert event is not None and f"run={run.id}" in event["details"]


async def test_factory_retract_slot_without_message_refuses(admin_setup) -> None:
    runs = SlotRunRepository(admin_setup.path)
    run = await runs.try_claim("good_morning", DAY.isoformat())
    assert run is not None  # dry-run rows have no channel message

    await _invoke(admin_setup, f"/factory retract slot {run.id}")

    assert admin_setup.client.deleted == []
    assert "no channel message" in _last_text(admin_setup)


async def test_factory_retract_slot_unknown_run(admin_setup) -> None:
    await _invoke(admin_setup, "/factory retract slot 999")

    assert admin_setup.client.deleted == []
    assert "not found" in _last_text(admin_setup)


async def test_factory_retract_slot_usage_without_id(admin_setup) -> None:
    await _invoke(admin_setup, "/factory retract slot")

    assert "Usage: /factory retract slot &lt;run_id&gt;" in _last_text(admin_setup)


# --- bare pause/resume pause every rubric ------------------------------------------


async def test_bare_pause_pauses_all_rubrics(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause")

    text = _last_text(admin_setup)
    assert "All rubrics paused (2)" in text
    states = RubricStateRepository(admin_setup.path)
    assert (await states.get("weather")).paused is True
    assert (await states.get("good_morning")).paused is True


async def test_bare_resume_clears_every_pause(admin_setup) -> None:
    await _invoke(admin_setup, "/factory pause")
    await _invoke(admin_setup, "/factory resume")

    text = _last_text(admin_setup)
    assert "All rubrics resumed (2)" in text
    states = RubricStateRepository(admin_setup.path)
    assert (await states.get("weather")).paused is False


# --- format_tick_result ------------------------------------------------------------


def test_format_tick_result_globally_paused() -> None:
    assert format_tick_result(TickResult(globally_paused=True)) == (
        "Scheduler skipped: MAX API global pause is active."
    )


def test_format_tick_result_lists_skip_reasons() -> None:
    result = TickResult(
        executed=("weather",),
        published=("weather",),
        skipped=(("good_morning", "rubric_paused"),),
    )
    line = format_tick_result(result)
    assert "executed=1 published=1 skipped=1 failed=0" in line
    assert "good_morning (rubric_paused)" in line
