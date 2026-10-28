from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from max_news_bot.scripts import healthcheck
from max_news_bot.scripts.healthcheck import _rubrics_healthy


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
def rubrics_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SLOT_HEARTBEAT_SECONDS", raising=False)
    monkeypatch.delenv("SLOT_TZ", raising=False)


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


# --- watchdog rules (plan section 11; unconditional: the only contour) -----------


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


def test_stale_digest_run_event_is_not_checked(conn, rubrics_env) -> None:
    # the legacy digest/factory rules are gone: an old "run"/"factory_run"
    # event in a production database must not fail the healthcheck
    _log(conn, "run", "collected=1", "datetime('now','-30 days')")
    _log(conn, "factory_run", "{}", "datetime('now','-30 days')")
    _log(conn, "slot_heartbeat", "quiet")
    _slot_run(conn, "good_morning", _today(), "published")
    assert _rubrics_healthy(conn) is True
