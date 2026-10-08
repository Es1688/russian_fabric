from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from max_news_bot.scripts import healthcheck
from max_news_bot.scripts.healthcheck import (
    _factory_healthy,
    _parse_active_hours,
    _within_window,
    _window_hours,
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
