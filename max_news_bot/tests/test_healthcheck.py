from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from max_news_bot.scripts import healthcheck
from max_news_bot.scripts.healthcheck import (
    _factory_healthy,
    _parse_active_hours,
    _within_window,
    _window_hours,
    _rubrics_healthy,
)


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "max_news_bot.db")
    connection.executescript(
        """
        CREATE TABLE bot_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_url TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            text TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            prompt_version TEXT,
            llm_model TEXT,
            tokens INTEGER,
            message_id TEXT,
            rejected_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP,
            last_attempt_at TIMESTAMP
        );
        CREATE TABLE slot_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rubric_id TEXT NOT NULL,
            slot_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            skip_reason TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            target TEXT,
            is_dry_run INTEGER NOT NULL DEFAULT 0,
            text TEXT,
            image_ref TEXT,
            source_refs TEXT,
            max_message_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP,
            UNIQUE (rubric_id, slot_date)
        );
        """
    )
    connection.commit()
    return connection


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FACTORY_INTERVAL_HOURS", "4")
    monkeypatch.setenv("FACTORY_ACTIVE_HOURS", "08:00-23:00")
    monkeypatch.delenv("FACTORY_ENABLED", raising=False)


def _fixed_clock(monkeypatch: pytest.MonkeyPatch, hour: int, minute: int) -> None:
    class FixedDateTime(datetime):
        _now = datetime.now().replace(hour=hour, minute=minute, second=0)

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls._now

    monkeypatch.setattr(healthcheck, "datetime", FixedDateTime)


def _log(conn, event: str, details: str, created_at: str | None = None) -> None:
    if created_at is None:
        conn.execute("INSERT INTO bot_stats (event, details) VALUES (?, ?)", (event, details))
    else:
        conn.execute(
            f"INSERT INTO bot_stats (event, details, created_at) "
            f"VALUES (?, ?, {created_at})",
            (event, details),
        )
    conn.commit()


def _insert_post(conn, status: str, *, when: str = "datetime('now')") -> None:
    conn.execute(
        f"INSERT INTO posts (news_url, source, title, status, created_at, "
        f"published_at, last_attempt_at) "
        f"VALUES ('u{status}{conn.total_changes}', 's', 't', ?, {when}, {when}, {when})",
        (status,),
    )
    conn.commit()


# --- active hours parsing -----------------------------------------------------


def test_parse_active_hours_variants() -> None:
    assert _parse_active_hours("08:00-23:00") == (
        datetime(2000, 1, 1, 8, 0).time(),
        datetime(2000, 1, 1, 23, 0).time(),
    )
    assert _parse_active_hours(" 22:00 - 06:00 ") is not None
    assert _parse_active_hours("") is None
    assert _parse_active_hours(None) is None
    assert _parse_active_hours("garbage") is None
    assert _parse_active_hours("25:00-26:00") is None


def test_window_hours_same_day_and_overnight() -> None:
    same_day = _parse_active_hours("08:00-23:00")
    overnight = _parse_active_hours("22:00-06:00")
    assert _window_hours(same_day) == pytest.approx(15.0)
    assert _window_hours(overnight) == pytest.approx(8.0)


def test_within_window_overnight() -> None:
    window = _parse_active_hours("22:00-06:00")
    assert _within_window(datetime(2000, 1, 1, 23, 30).time(), window)
    assert _within_window(datetime(2000, 1, 1, 3, 0).time(), window)
    assert not _within_window(datetime(2000, 1, 1, 12, 0).time(), window)


# --- factory rules --------------------------------------------------------------


def test_no_events_is_grace_period(conn, env) -> None:
    assert _factory_healthy(conn) is True


def test_stale_heartbeat_is_unhealthy(conn, env) -> None:
    _log(conn, "factory_run", json.dumps({"state": "run", "deduped": 0}), "datetime('now','-1 day')")
    assert _factory_healthy(conn) is False


def test_idle_heartbeat_counts_as_liveness(conn, env) -> None:
    _log(conn, "factory_run", json.dumps({"state": "idle", "reason": "quiet_hours"}))
    assert _factory_healthy(conn) is True


def test_paused_factory_is_healthy_even_without_posts(conn, env, monkeypatch) -> None:
    _fixed_clock(monkeypatch, 12, 0)
    _log(conn, "factory_run", json.dumps({"state": "idle", "reason": "paused"}))
    _insert_post(conn, "published", when="datetime('now','-3 days')")
    assert _factory_healthy(conn) is True


def test_stale_publication_inside_window_is_unhealthy(conn, env, monkeypatch) -> None:
    _fixed_clock(monkeypatch, 12, 0)
    _log(conn, "factory_run", json.dumps({"state": "run", "deduped": 0}))
    _insert_post(conn, "published", when="datetime('now','-2 days')")
    assert _factory_healthy(conn) is False


def test_stale_publication_outside_window_is_healthy(conn, env, monkeypatch) -> None:
    _fixed_clock(monkeypatch, 23, 30)  # after 23:00 the rule sleeps
    _log(conn, "factory_run", json.dumps({"state": "run", "deduped": 0}))
    _insert_post(conn, "published", when="datetime('now','-2 days')")
    assert _factory_healthy(conn) is True


def test_fresh_previewed_post_counts_as_publication(conn, env, monkeypatch) -> None:
    _fixed_clock(monkeypatch, 12, 0)
    _log(conn, "factory_run", json.dumps({"state": "run", "deduped": 0}))
    _insert_post(conn, "previewed")
    assert _factory_healthy(conn) is True


def test_never_published_red_only_with_candidates(conn, env, monkeypatch) -> None:
    _fixed_clock(monkeypatch, 12, 0)
    _log(conn, "factory_run", json.dumps({"state": "run", "deduped": 0, "recovered": 0}))
    assert _factory_healthy(conn) is True  # nothing to publish: not an incident

    _log(conn, "factory_run", json.dumps({"state": "run", "deduped": 3, "recovered": 0}))
    assert _factory_healthy(conn) is False  # candidates existed, no output


# --- rubrics contour rules (RUBRICS_ENABLED=true, plan section 11) ---------------


@pytest.fixture
def rubrics_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUBRICS_ENABLED", "1")
    monkeypatch.delenv("SLOT_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.delenv("SLOT_TZ", raising=False)


def _today() -> str:
    return datetime.now().date().isoformat()


def _yesterday() -> str:
    return (datetime.now() - timedelta(days=1)).date().isoformat()


def _slot_run(conn, rubric: str, slot_date: str, status: str) -> None:
    conn.execute(
        "INSERT INTO slot_runs (rubric_id, slot_date, status) VALUES (?, ?, ?)",
        (rubric, slot_date, status),
    )
    conn.commit()


def test_rubrics_missing_tables_is_first_boot_grace(tmp_path) -> None:
    # a database where the app has not run migration v4 yet
    bare = sqlite3.connect(tmp_path / "bare.db")
    bare.execute(
        "CREATE TABLE bot_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event TEXT NOT NULL, details TEXT, created_at TIMESTAMP "
        "DEFAULT CURRENT_TIMESTAMP)"
    )
    bare.commit()
    assert _rubrics_healthy(bare) is True


def test_rubrics_healthy_with_fresh_heartbeat_and_today_run(
    conn, rubrics_env
) -> None:
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "good_morning", _today(), "published")
    assert _rubrics_healthy(conn) is True


def test_rubrics_stale_heartbeat_is_unhealthy(conn, rubrics_env) -> None:
    # default SLOT_HEARTBEAT_SECONDS=900: older than 2x window is a hang
    _log(
        conn,
        "slot_heartbeat",
        "quiet",
        "datetime('now','-1 hour')",
    )
    _slot_run(conn, "good_morning", _today(), "published")
    assert _rubrics_healthy(conn) is False


def test_rubrics_silent_day_after_morning_is_unhealthy(
    conn, rubrics_env, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, 12, 0)
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "good_morning", _yesterday(), "published")
    # today has only a non-terminal row: the channel is silent
    _slot_run(conn, "good_morning", _today(), "pending")
    assert _rubrics_healthy(conn) is False


def test_rubrics_skipped_slot_counts_as_explained_outcome(
    conn, rubrics_env, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, 12, 0)
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "weather", _today(), "skipped")
    assert _rubrics_healthy(conn) is True


def test_rubrics_before_nine_silent_check_sleeps(
    conn, rubrics_env, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, 7, 30)  # morning slots not due yet
    _log(conn, "slot_heartbeat", "quiet")
    assert _rubrics_healthy(conn) is True


def test_rubrics_no_slot_runs_at_all_is_first_boot_grace(
    conn, rubrics_env, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, 12, 0)
    _log(conn, "slot_heartbeat", "quiet")
    assert _rubrics_healthy(conn) is True


def test_rubrics_missing_daily_summary_after_evening_is_unhealthy(
    conn, rubrics_env, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, 23, 40)  # summary slot 22:40 + slack
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "good_morning", _today(), "published")
    _log(conn, "daily_summary", _yesterday())
    assert _rubrics_healthy(conn) is False


def test_rubrics_today_summary_keeps_healthy(
    conn, rubrics_env, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, 23, 40)
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "good_morning", _today(), "published")
    _log(conn, "daily_summary", _today())
    assert _rubrics_healthy(conn) is True


def test_rubrics_summary_check_sleeps_before_evening(
    conn, rubrics_env, monkeypatch
) -> None:
    _fixed_clock(monkeypatch, 22, 50)  # summary may still be in flight
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "good_morning", _today(), "published")
    _log(conn, "daily_summary", _yesterday())
    assert _rubrics_healthy(conn) is True


def test_rubrics_never_sent_summary_is_grace(conn, rubrics_env, monkeypatch) -> None:
    _fixed_clock(monkeypatch, 23, 40)
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "good_morning", _today(), "published")
    assert _rubrics_healthy(conn) is True
