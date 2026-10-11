"""Docker healthcheck for the digest loop, the content factory and the
rubrics contour.

Digest rule (unchanged): fail if no "run" event within 2x post interval.
Factory rules (only when FACTORY_ENABLED=true):
- cycle liveness: a fresh "factory_run" event within 2x factory interval
  (idle heartbeats count as liveness);
- publications: "no post for X hours" is checked ONLY inside the active
  window (env FACTORY_ACTIVE_HOURS, default 08:00-23:00), X = window
  length + one interval; published and previewed posts both count.
Rubrics rules (only when RUBRICS_ENABLED=true; plan section 11 watchdog):
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

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TRUE_VALUES = ("1", "true", "yes", "on")

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
        if not _digest_healthy(conn):
            sys.exit(1)
        if _env_flag("FACTORY_ENABLED"):
            if not _factory_healthy(conn):
                sys.exit(1)
        if _env_flag("RUBRICS_ENABLED"):
            if not _rubrics_healthy(conn):
                sys.exit(1)
    sys.exit(0)


def _digest_healthy(conn: sqlite3.Connection) -> bool:
    interval_hours = int(os.getenv("POST_INTERVAL_HOURS", "6") or "6")
    allowed_seconds = interval_hours * 7200

    row = conn.execute(
        "SELECT strftime('%s', MAX(created_at)) FROM bot_stats WHERE event='run'"
    ).fetchone()

    if not row or not row[0]:
        return True

    return time.time() - int(row[0]) < allowed_seconds


def _factory_healthy(conn: sqlite3.Connection) -> bool:
    interval_hours = int(os.getenv("FACTORY_INTERVAL_HOURS", "4") or "4")
    now = time.time()

    # (1) cycle liveness: any factory_run event, idle heartbeats included
    row = conn.execute(
        "SELECT strftime('%s', MAX(created_at)) FROM bot_stats "
        "WHERE event='factory_run'"
    ).fetchone()
    if row and row[0]:
        if now - int(row[0]) > interval_hours * 2 * 3600:
            return False
    # no factory_run event at all -> first-boot grace period (digest-style)

    # (2) publications rule, only inside the active window
    window = _parse_active_hours(os.getenv("FACTORY_ACTIVE_HOURS", "08:00-23:00"))
    if window is None:
        return True
    if not _within_window(datetime.now().time(), window):
        return True

    # a factory idle by design (manual pause / auto-pause) is alive, not sick
    if _last_idle_reason(conn) in ("paused", "autopause"):
        return True

    limit_seconds = (_window_hours(window) + interval_hours) * 3600

    pub_row = conn.execute(
        "SELECT strftime('%s', MAX(COALESCE(published_at, last_attempt_at, "
        "created_at))) FROM posts WHERE status IN ('published', 'previewed')"
    ).fetchone()
    last_pub = int(pub_row[0]) if pub_row and pub_row[0] else None
    if last_pub is not None:
        return now - last_pub <= limit_seconds

    # never published: red only if recent cycles actually had candidates
    return not _had_candidates(conn, now - limit_seconds)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def _parse_active_hours(spec: str | None) -> tuple[dt_time, dt_time] | None:
    """Parse "HH:MM-HH:MM"; unparseable/empty -> None (rule disabled)."""
    if not spec or not str(spec).strip():
        return None
    parts = str(spec).strip().split("-")
    if len(parts) != 2:
        return None
    try:
        start = _parse_hhmm(parts[0])
        end = _parse_hhmm(parts[1])
    except ValueError:
        return None
    return start, end


def _parse_hhmm(value: str) -> dt_time:
    hours, _, minutes = value.strip().partition(":")
    hh, mm = int(hours), int(minutes or 0)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"invalid time {value!r}")
    return dt_time(hh, mm)


def _within_window(moment: dt_time, window: tuple[dt_time, dt_time]) -> bool:
    start, end = window
    if start == end:
        return True  # degenerate 24h window
    if start < end:
        return start <= moment < end
    # overnight window (e.g. 22:00-06:00)
    return moment >= start or moment < end


def _window_hours(window: tuple[dt_time, dt_time]) -> float:
    start, end = window
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    length = (end_minutes - start_minutes) % (24 * 60)
    if length == 0:
        length = 24 * 60
    return length / 60.0


def _last_idle_reason(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT details FROM bot_stats WHERE event='factory_run' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("state") == "idle":
        reason = data.get("reason")
        return str(reason) if reason else None
    return None


def _had_candidates(conn: sqlite3.Connection, since_epoch: float) -> bool:
    since = datetime.fromtimestamp(since_epoch).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT details FROM bot_stats WHERE event='factory_run' "
        "AND created_at >= ? ORDER BY id DESC LIMIT 50",
        (since,),
    ).fetchall()
    for (details,) in rows:
        try:
            data = json.loads(details or "")
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("state") != "run":
            continue
        if data.get("deduped", 0) > 0 or data.get("recovered", 0) > 0:
            return True
    return False


# --- rubrics contour (RUBRICS_ENABLED=true, plan section 11) -------------------


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
