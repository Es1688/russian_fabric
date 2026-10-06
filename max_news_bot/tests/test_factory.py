from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from max_news_bot.config.loader import AppConfig
from max_news_bot.core.factory import ContentFactory
from max_news_bot.core.models import NewsItem, Post
from max_news_bot.publishers.post import PostPublishResult
from max_news_bot.tests.conftest import make_factory_config
from max_news_bot.tests.mocks import (
    MockAlerter,
    MockFetcher,
    MockPostPublisher,
    MockWriter,
    error_result,
    skipped_result,
    written_result,
)
from max_news_bot.utils.db import Database
from max_news_bot.writers.base import WriteResult

# Long enough to pass validate_post_text (>= 200 visible chars, no links).
LONG_TEXT = (
    "Инженеры внедрили автоматическую генерацию тестовых сценариев и "
    "предиктивный анализ сбоев в непрерывном конвейере. Команды сообщают "
    "о сокращении времени регрессионного тестирования на десятки процентов, "
    "а количество инцидентов после релизов заметно снижается. Подход уже "
    "распространяется на смежные направления разработки."
)


_FRESH = object()  # sentinel: "now - 10 minutes" default, distinct from None


def make_item(
    n: int = 1,
    *,
    title: str | None = None,
    url: str | None = None,
    published_at: object = _FRESH,
) -> NewsItem:
    if published_at is _FRESH:
        published_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    return NewsItem(
        title=title or f"Technology story number {n} covers release",
        url=url or f"https://example.com/news/{n}",
        source="Test RSS",
        published_at=published_at,  # type: ignore[arg-type]
    )


def make_factory(
    app_config: AppConfig,
    db: Database,
    *,
    items: list[NewsItem] | None = None,
    writer=None,
    publisher: MockPostPublisher | None = None,
    alerter: MockAlerter | None = None,
    factory_config=None,
) -> tuple[ContentFactory, object, MockPostPublisher, MockAlerter]:
    fetcher = MockFetcher(items or [])
    writer = writer if writer is not None else MockWriter()
    publisher = publisher if publisher is not None else MockPostPublisher()
    alerter = alerter if alerter is not None else MockAlerter()
    config = app_config if factory_config is None else replace(
        app_config, factory=factory_config
    )
    factory = ContentFactory(config, fetcher, writer, publisher, alerter, db)
    return factory, writer, publisher, alerter


def _backdate_post(db_path, post_id: int, expression: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE posts SET created_at = datetime('now', ?) WHERE id = ?",
            (expression, post_id),
        )
        conn.commit()


async def _last_run_details(db: Database) -> dict:
    # direct SQL with an id tiebreak: Database.get_last_event orders by
    # created_at only, which is ambiguous for same-second events
    with sqlite3.connect(_db_path_of(db)) as conn:
        row = conn.execute(
            "SELECT details FROM bot_stats WHERE event='factory_run' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row[0]
    return json.loads(row[0])


def _db_path_of(db: Database):
    return db._path  # noqa: SLF001 - test helper


class ScriptedWriter:
    """Writer whose behavior can be flipped between runs."""

    def __init__(self) -> None:
        self.calls: list[NewsItem] = []
        self.result_fn = lambda item: error_result(item, "content")

    async def write_post(self, item: NewsItem) -> WriteResult:
        self.calls.append(item)
        return self.result_fn(item)


# --- happy path ---------------------------------------------------------------


async def test_happy_path_publishes_posts(db, app_config) -> None:
    cfg = make_factory_config(posts_per_cycle=2)
    items = [
        make_item(1, title="Python release improves compiler speed"),
        make_item(2, title="Startup raises funding for machine learning"),
    ]
    factory, writer, publisher, _ = make_factory(
        app_config, db, items=items, factory_config=cfg
    )

    result = await factory.run()

    assert result.collected == 2
    assert result.written == 2
    assert result.published == 2
    assert result.failed == 0
    assert len(writer.calls) == 2
    assert all(to_admins is False for _, to_admins in publisher.calls)

    posts = await db.get_recent_posts(10)
    assert len(posts) == 2
    assert all(post.status == "published" for post in posts)
    assert all(post.message_id == 1000 for post in posts)

    # the factory never reads or writes sent_news: digest and factory
    # are independent consumers, duplicates between them are allowed
    assert await db.is_sent(items[0].url) is False
    assert await db.is_sent(items[1].url) is False


async def test_skip_is_terminal_and_not_retried(db, app_config) -> None:
    item = make_item(1, title="Python release improves compiler speed")
    writer = MockWriter(skipped_result(item))
    factory, writer, _, _ = make_factory(
        app_config, db, items=[item], writer=writer
    )

    first = await factory.run()
    assert first.skipped == 1
    assert first.written == 0
    posts = await db.get_recent_posts(10)
    assert [post.status for post in posts] == ["skipped"]

    writer.calls.clear()
    second = await factory.run()
    assert second.skipped == 0
    assert writer.calls == []


# --- error classes ------------------------------------------------------------


async def test_content_error_bumps_attempts_then_excludes(db, app_config) -> None:
    cfg = make_factory_config(max_attempts=2, alert_after_failed_cycles=99)
    item = make_item(1, title="Python release improves compiler speed")
    writer = MockWriter(error_result(item, error_kind="content"))
    factory, writer, _, _ = make_factory(
        app_config, db, items=[item], writer=writer, factory_config=cfg
    )

    await factory.run()
    posts = await db.get_recent_posts(10)
    assert len(posts) == 1
    assert posts[0].status == "failed"
    assert posts[0].attempts == 1

    writer.calls.clear()
    await factory.run()
    posts = await db.get_recent_posts(10)
    assert posts[0].attempts == 2

    writer.calls.clear()
    result = await factory.run()
    assert writer.calls == []  # attempts >= max_attempts: news excluded
    assert result.failed == 0


async def test_infra_series_keeps_attempts_and_trips_circuit_breaker(
    db, app_config
) -> None:
    cfg = make_factory_config(posts_per_cycle=5, circuit_breaker_after=3)
    items = [
        make_item(1, title="Python release improves compiler speed"),
        make_item(2, title="Startup raises funding for machine learning"),
        make_item(3, title="Quantum computer solves optimization puzzle"),
        make_item(4, title="Database migration breaks production systems"),
        make_item(5, title="Mobile operators expand coverage worldwide"),
    ]
    writer = MockWriter(error_result(make_item(1), error_kind="infra"))
    factory, writer, _, _ = make_factory(
        app_config, db, items=items, writer=writer, factory_config=cfg
    )

    result = await factory.run()

    assert result.failed == 3
    assert len(writer.calls) == 3  # cycle broken, last two never attempted
    posts = await db.get_recent_posts(10)
    assert len(posts) == 3
    assert all(post.attempts == 0 for post in posts)  # infra never burns them


async def test_config_error_autopauses_alerts_and_breaks_cycle(db, app_config) -> None:
    cfg = make_factory_config(posts_per_cycle=5)
    items = [
        make_item(1, title="Python release improves compiler speed"),
        make_item(2, title="Startup raises funding for machine learning"),
        make_item(3, title="Quantum computer solves optimization puzzle"),
    ]
    writer = MockWriter(error_result(make_item(1), error_kind="config", error="llm http 401"))
    factory, writer, _, alerter = make_factory(
        app_config, db, items=items, writer=writer, factory_config=cfg
    )

    result = await factory.run()

    assert result.failed == 1
    assert len(writer.calls) == 1  # cycle broken on the first config error
    assert len(alerter.alerts) == 1  # immediate alert, no 3-cycle wait
    assert "auto-paused" in alerter.alerts[0]
    assert await db.get_state_flag("factory_autopause") is False

    # attempts untouched by the config error
    posts = await db.get_recent_posts(10)
    assert posts[0].attempts == 0

    # next cycle: idle heartbeat, no writer call
    writer.calls.clear()
    result = await factory.run()
    assert writer.calls == []
    details = await _last_run_details(db)
    assert details["state"] == "idle"
    assert details["reason"] == "autopause"


# --- publish / validation failures ---------------------------------------------


async def test_send_infra_failure_keeps_attempts(db, app_config) -> None:
    # plan P0.3: network/flood send failures are infra — attempts are not
    # burned, a half-day Telegram outage must not bury the day's posts
    item = make_item(1, title="Python release improves compiler speed")
    publisher = MockPostPublisher(
        result=PostPublishResult(
            success=False, message_id=None, error="network down", error_kind="infra"
        )
    )
    factory, _, publisher, _ = make_factory(
        app_config, db, items=[item], publisher=publisher
    )

    result = await factory.run()

    assert result.written == 1
    assert result.published == 0
    assert result.failed == 1
    posts = await db.get_recent_posts(10)
    assert posts[0].status == "failed"
    assert posts[0].attempts == 0
    assert posts[0].error == "network down"


async def test_send_content_failure_bumps_attempts(db, app_config) -> None:
    item = make_item(1, title="Python release improves compiler speed")
    publisher = MockPostPublisher(
        result=PostPublishResult(
            success=False,
            message_id=None,
            error="message is too long",
            error_kind="content",
        )
    )
    factory, _, publisher, _ = make_factory(
        app_config, db, items=[item], publisher=publisher
    )

    result = await factory.run()

    assert result.failed == 1
    posts = await db.get_recent_posts(10)
    assert posts[0].status == "failed"
    assert posts[0].attempts == 1


async def test_publish_infra_series_trips_circuit_breaker(db, app_config) -> None:
    cfg = make_factory_config(posts_per_cycle=5, circuit_breaker_after=3)
    items = [
        make_item(1, title="Python release improves compiler speed"),
        make_item(2, title="Startup raises funding for machine learning"),
        make_item(3, title="Quantum computer solves optimization puzzle"),
        make_item(4, title="Database migration breaks production systems"),
        make_item(5, title="Mobile operators expand coverage worldwide"),
    ]
    publisher = MockPostPublisher(
        result=PostPublishResult(
            success=False, message_id=None, error="network down", error_kind="infra"
        )
    )
    factory, writer, _, _ = make_factory(
        app_config, db, items=items, publisher=publisher, factory_config=cfg
    )

    result = await factory.run()

    assert result.failed == 3
    assert len(writer.calls) == 3  # breaker stopped the cycle after 3 infra
    posts = await db.get_recent_posts(10)
    assert len(posts) == 3
    assert all(post.attempts == 0 for post in posts)


async def test_validation_failure_saves_rejected_text(db, app_config) -> None:
    item = make_item(1, title="Python release improves compiler speed")
    writer = MockWriter(written_result(item, text="too short"))
    factory, writer, _, _ = make_factory(
        app_config, db, items=[item], writer=writer
    )

    result = await factory.run()

    assert result.written == 0
    assert result.failed == 1
    posts = await db.get_recent_posts(10)
    assert posts[0].status == "failed"
    assert posts[0].attempts == 1
    assert posts[0].rejected_text == "too short"
    assert "too short" in (posts[0].error or "")


async def test_validation_rejects_link_in_output(db, app_config) -> None:
    item = make_item(1, title="Python release improves compiler speed")
    writer = MockWriter(
        written_result(item, text="Read more at https://evil.example " + LONG_TEXT)
    )
    factory, _, _, _ = make_factory(app_config, db, items=[item], writer=writer)

    result = await factory.run()

    assert result.written == 0
    assert result.failed == 1
    posts = await db.get_recent_posts(10)
    assert posts[0].status == "failed"
    assert posts[0].error == "contains links"


# --- recovery ------------------------------------------------------------------


async def test_recovery_publishes_stale_draft_without_writer(db, app_config) -> None:
    stale = Post(
        id=None,
        news_url="https://example.com/news/9",
        source="Test RSS",
        title="Old story about database migration",
        text=LONG_TEXT,
        status="draft",
    )
    post_id = await db.save_attempt(stale)
    _backdate_post(app_config.data_path, post_id, "-2 hours")

    factory, writer, publisher, _ = make_factory(app_config, db, items=[])

    result = await factory.run()

    assert writer.calls == []  # recovery never calls the LLM
    assert result.recovered == 1
    posts = await db.get_recent_posts(10)
    assert posts[0].status == "published"
    assert posts[0].message_id == 1000
    assert len(publisher.calls) == 1


async def test_recovery_respects_dry_run(db, app_config) -> None:
    cfg = make_factory_config(dry_run=True)
    stale = Post(
        id=None,
        news_url="https://example.com/news/9",
        source="Test RSS",
        title="Old story about database migration",
        text=LONG_TEXT,
        status="draft",
    )
    post_id = await db.save_attempt(stale)
    _backdate_post(app_config.data_path, post_id, "-2 hours")

    factory, _, publisher, _ = make_factory(app_config, db, factory_config=cfg)

    result = await factory.run()

    assert result.recovered == 1
    assert publisher.calls[0][1] is True  # to_admins
    posts = await db.get_recent_posts(10)
    assert posts[0].status == "previewed"


# --- filtering and dedup --------------------------------------------------------


async def test_age_filter_drops_old_and_undated_news(db, app_config) -> None:
    cfg = make_factory_config(posts_per_cycle=5)
    items = [
        make_item(
            1,
            title="Python release improves compiler speed",
            published_at=datetime.now(timezone.utc) - timedelta(hours=48),
        ),
        make_item(2, title="Startup raises funding for machine learning", published_at=None),
        make_item(
            3,
            title="Quantum computer solves optimization puzzle",
            published_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ),
    ]
    factory, writer, _, _ = make_factory(
        app_config, db, items=items, factory_config=cfg
    )

    result = await factory.run()

    assert result.collected == 3
    assert result.written == 1
    assert [call.url for call in writer.calls] == [items[2].url]


async def test_sent_news_candidate_is_processed(db, app_config) -> None:
    item = make_item(1, title="Python release improves compiler speed")
    await db.mark_sent(item)
    factory, writer, _, _ = make_factory(app_config, db, items=[item])

    result = await factory.run()

    # a digest-sent item is still processed: the post is a rework,
    # not a repeat of the digest link
    assert [call.url for call in writer.calls] == [item.url]
    assert result.written == 1
    assert result.deduped == 1


async def test_jaccard_duplicate_title_dropped(db, app_config) -> None:
    existing = Post(
        id=None,
        news_url="https://example.com/other-url",
        source="Test RSS",
        title="Python release improves compiler speed dramatically",
        text=LONG_TEXT,
        status="published",
        published_at=datetime.now(timezone.utc),
    )
    await db.save_attempt(existing)

    item = make_item(
        1,
        title="Python release improves compiler speed",
        url="https://example.com/news/1",
    )
    factory, writer, _, _ = make_factory(app_config, db, items=[item])

    result = await factory.run()

    assert writer.calls == []
    assert result.deduped == 0


async def test_jaccard_duplicate_inside_batch(db, app_config) -> None:
    cfg = make_factory_config(posts_per_cycle=5)
    items = [
        make_item(1, title="Python release improves compiler speed"),
        make_item(2, title="Python release improves compiler speed again"),
    ]
    factory, writer, _, _ = make_factory(
        app_config, db, items=items, factory_config=cfg
    )

    result = await factory.run()

    assert result.written == 1
    assert len(writer.calls) == 1


# --- limits ---------------------------------------------------------------------


async def test_posts_per_cycle_limit(db, app_config) -> None:
    cfg = make_factory_config(posts_per_cycle=1)
    items = [
        make_item(1, title="Python release improves compiler speed"),
        make_item(2, title="Startup raises funding for machine learning"),
        make_item(3, title="Quantum computer solves optimization puzzle"),
    ]
    factory, writer, _, _ = make_factory(
        app_config, db, items=items, factory_config=cfg
    )

    result = await factory.run()

    assert result.written == 1
    assert len(writer.calls) == 1


async def test_max_posts_per_day_limit(db, app_config) -> None:
    cfg = make_factory_config(posts_per_cycle=5, max_posts_per_day=1)
    items = [
        make_item(1, title="Python release improves compiler speed"),
        make_item(2, title="Startup raises funding for machine learning"),
    ]
    factory, writer, _, _ = make_factory(
        app_config, db, items=items, factory_config=cfg
    )

    first = await factory.run()
    assert first.written == 1

    writer.calls.clear()
    second = await factory.run()
    assert writer.calls == []  # day budget spent (attempts counted by rows)
    assert second.written == 0


# --- dry run and idle heartbeats --------------------------------------------------


async def test_dry_run_previews_to_admins(db, app_config) -> None:
    cfg = make_factory_config(dry_run=True)
    item = make_item(1, title="Python release improves compiler speed")
    factory, _, publisher, _ = make_factory(
        app_config, db, items=[item], factory_config=cfg
    )

    result = await factory.run()

    assert result.previewed == 1
    assert result.published == 0
    assert publisher.calls[0][1] is True  # to_admins
    posts = await db.get_recent_posts(10)
    assert posts[0].status == "previewed"
    assert posts[0].message_id is None


async def test_paused_factory_writes_idle_heartbeat(db, app_config) -> None:
    item = make_item(1, title="Python release improves compiler speed")
    factory, writer, _, _ = make_factory(app_config, db, items=[item])
    await db.set_state_flag("factory", False)

    result = await factory.run()

    assert writer.calls == []
    assert result.written == 0
    details = await _last_run_details(db)
    assert details == {"state": "idle", "reason": "paused"}


async def test_quiet_hours_write_idle_heartbeat(db, app_config, monkeypatch) -> None:
    item = make_item(1, title="Python release improves compiler speed")
    factory, writer, _, _ = make_factory(app_config, db, items=[item])
    monkeypatch.setattr(factory, "_is_active_now", lambda: False)

    result = await factory.run()

    assert writer.calls == []
    details = await _last_run_details(db)
    assert details == {"state": "idle", "reason": "quiet_hours"}


# --- events and alerting -----------------------------------------------------------


async def test_factory_run_event_is_json_with_counters(db, app_config) -> None:
    item = make_item(1, title="Python release improves compiler speed")

    class UsageWriter:
        def __init__(self) -> None:
            self.calls: list[NewsItem] = []

        async def write_post(self, item: NewsItem) -> WriteResult:
            self.calls.append(item)
            return replace(written_result(item), tokens=77, latency_ms=120)

    factory, writer, _, _ = make_factory(
        app_config, db, items=[item], writer=UsageWriter()
    )

    result = await factory.run()
    assert result.written == 1

    details = await _last_run_details(db)
    assert details["state"] == "run"
    assert details["collected"] == 1
    assert details["filtered"] == 1
    assert details["deduped"] == 1
    assert details["written"] == 1
    assert details["skipped"] == 0
    assert details["published"] == 1
    assert details["previewed"] == 0
    assert details["failed"] == 0
    assert details["recovered"] == 0
    assert details["tokens"] == 77
    assert details["llm_latency_ms"] == {"avg": 120, "max": 120}
    assert details["dry_run"] is False


async def test_alert_dedup_single_alert_and_recovery(db, app_config) -> None:
    cfg = make_factory_config(alert_after_failed_cycles=2, max_attempts=10)
    item = make_item(1, title="Python release improves compiler speed")
    writer = ScriptedWriter()
    alerter = MockAlerter()
    factory, writer, _, alerter = make_factory(
        app_config, db, items=[item], writer=writer, alerter=alerter, factory_config=cfg
    )

    await factory.run()  # failed cycle 1
    assert alerter.alerts == []
    await factory.run()  # failed cycle 2 -> threshold reached
    assert len(alerter.alerts) == 1
    await factory.run()  # failed cycle 3 -> no repeat
    await factory.run()  # failed cycle 4 -> no repeat
    assert len(alerter.alerts) == 1

    writer.result_fn = written_result  # provider healed
    result = await factory.run()
    assert result.written == 1
    assert len(alerter.recoveries) == 1
    await factory.run()
    assert len(alerter.recoveries) == 1  # recovery sent exactly once


async def test_all_skip_cycle_is_not_an_incident(db, app_config) -> None:
    cfg = make_factory_config(alert_after_failed_cycles=1)
    item = make_item(1, title="Python release improves compiler speed")
    writer = MockWriter(skipped_result(item))
    alerter = MockAlerter()
    factory, _, _, alerter = make_factory(
        app_config, db, items=[item], writer=writer, alerter=alerter, factory_config=cfg
    )

    result = await factory.run()

    assert result.skipped == 1
    assert result.written == 0
    assert result.failed == 0
    assert alerter.alerts == []
