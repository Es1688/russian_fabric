"""Docker healthcheck for the rubrics contour — the only publishing contour.

Rules (plan section 11 watchdog):
- liveness: a fresh "slot_heartbeat" event within 2x SLOT_HEARTBEAT_SECONDS
  (written by the scheduler loop even on a quiet day);
- silent day: after the morning slots plus grace (09:00 in SLOT_TZ, default
  Europe/Moscow) the day must hold at least one terminal slot_runs row
  (published/skipped/failed/unknown_delivery - an explained outcome);
  a database with no slot_runs at all is a first-boot grace period;
- daily summary: after 23:30 local the last "daily_summary" event must
  cover today (the summary slot is 22:40 config time); no event ever sent
  is a grace period until the first evening.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# slot statuses that mean "the day has an explained outcome"; anything
# pending/generated/publishing at 09:00+ is mid-flight or stuck
_SLOT_TERMINAL_STATUSES = ("published", "skipped", "failed", "unknown_delivery")
_SLOT_QUIET_AFTER = dt_time(9, 0)  # morning slots 07:00/07:05 + grace are done
_SLOT_SUMMARY_CHECK_AFTER = dt_time(23, 30)  # summary slot 22:40 + slack


def main() -> None:
    db_path = Path(os.getenv("DATABASE_PATH", "data/max_news_bot.db"))
    if not db_path.exists():
        sys.exit(0)

    with sqlite3.connect(db_path) as conn:
        if not _rubrics_healthy(conn):
            sys.exit(1)
    sys.exit(0)


def _rubrics_healthy(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "slot_runs"):
        # the app has not run its migrations yet: first-boot grace
        return True
    if not _slot_heartbeat_fresh(conn):
        return False
    if not _slots_not_silent(conn):
        return False
    return _daily_summary_fresh(conn)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _slot_heartbeat_fresh(conn: sqlite3.Connection) -> bool:
    interval_seconds = int(os.getenv("SLOT_HEARTBEAT_SECONDS", "900") or "900")
    row = conn.execute(
        "SELECT strftime('%s', MAX(created_at)) FROM bot_stats "
        "WHERE event='slot_heartbeat'"
    ).fetchone()
    if not row or not row[0]:
        return True  # no heartbeat yet -> first-boot grace
    return time.time() - int(row[0]) < interval_seconds * 2


def _slots_not_silent(conn: sqlite3.Connection) -> bool:
    now_local = _local_now()
    if now_local.time() < _SLOT_QUIET_AFTER:
        return True
    total = conn.execute("SELECT COUNT(*) FROM slot_runs").fetchone()
    if total and int(total[0]) == 0:
        return True  # contour never ran a slot: first-boot grace
    placeholders = ",".join("?" for _ in _SLOT_TERMINAL_STATUSES)
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM slot_runs
        WHERE slot_date = ? AND status IN ({placeholders})
        """,
        (now_local.date().isoformat(), *_SLOT_TERMINAL_STATUSES),
    ).fetchone()
    return bool(row and int(row[0]) > 0)


def _daily_summary_fresh(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT details FROM bot_stats WHERE event='daily_summary' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return True  # never sent yet: grace until the first evening
    now_local = _local_now()
    if now_local.time() < _SLOT_SUMMARY_CHECK_AFTER:
        return True
    last_date = str(row[0] or "").strip()
    return last_date >= now_local.date().isoformat()


def _local_now() -> datetime:
    """Now in the configured local timezone (dates/times only are used)."""
    name = os.getenv("SLOT_TZ", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        return datetime.now(ZoneInfo(name))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return datetime.now(timezone.utc)


if __name__ == "__main__":
    main()
