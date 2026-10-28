from __future__ import annotations

import asyncio
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# sqlite3.connect(timeout=...) is the busy_timeout in seconds shared by
# every writer of this database file (the slot scheduler loop, the daily
# summary and the backup service).
_BUSY_TIMEOUT_SECONDS = 30.0
_MAX_ERROR_LEN = 300


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_db_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Baseline digest-pipeline schema (idempotent, matches pre-migration db)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sources_state (
            name TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Content-factory posts table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
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
            message_id INTEGER,
            rejected_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP,
            last_attempt_at TIMESTAMP
        )
        """
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """posts.message_id INTEGER -> TEXT (MAX mid is a "mid.<id>" string).

    SQLite has no ALTER COLUMN: the table is rebuilt with the exact v2
    schema except message_id TEXT, copying every row (ints cast to str by
    CAST, NULL stays NULL) and keeping ids, UNIQUE(news_url) and defaults.
    CREATE TABLE runs in autocommit (Python sqlite3 starts no transaction
    before DDL), so a hard kill mid-migration can leave a dangling
    posts_new; dropping it first makes the step safe to re-run. The rebuild
    also resets the AUTOINCREMENT counter to max(id), so the pre-rebuild
    sqlite_sequence value is restored to keep ids of deleted rows unused.
    """
    conn.execute("DROP TABLE IF EXISTS posts_new")
    conn.execute(
        """
        CREATE TABLE posts_new (
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
        )
        """
    )
    conn.execute(
        """
        INSERT INTO posts_new (
            id, news_url, source, title, text, status, attempts, error,
            prompt_version, llm_model, tokens, message_id, rejected_text,
            created_at, published_at, last_attempt_at
        )
        SELECT
            id, news_url, source, title, text, status, attempts, error,
            prompt_version, llm_model, tokens, CAST(message_id AS TEXT),
            rejected_text, created_at, published_at, last_attempt_at
        FROM posts
        """
    )
    seq_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'posts'"
    ).fetchone()
    conn.execute("DROP TABLE posts")
    conn.execute("ALTER TABLE posts_new RENAME TO posts")
    if seq_row is not None:
        # the renamed entry (or its absence, for an empty posts) may sit
        # below the pre-rebuild counter: raise it back, never lower it
        conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'posts' AND seq < ?",
            (seq_row[0], seq_row[0]),
        )
        conn.execute(
            """
            INSERT INTO sqlite_sequence (name, seq)
            SELECT 'posts', ? WHERE NOT EXISTS
                (SELECT 1 FROM sqlite_sequence WHERE name = 'posts')
            """,
            (seq_row[0],),
        )


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Autonomous-channel tables (plan §9): slot_runs, digest_items,
    rubric_state, content_bank (§6) and filter_log.

    slot_runs.slot_date is a calendar date computed by the scheduler in the
    config timezone (Europe/Moscow) and stored as 'YYYY-MM-DD' — a UTC date
    would invent duplicates/misses for near-midnight slots and on DST hops.
    All timestamps stay UTC, as everywhere else in this database. Existing
    tables are not touched.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slot_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rubric_id TEXT NOT NULL,
            slot_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending', 'generated', 'publishing', 'published',
                    'skipped', 'failed', 'unknown_delivery'
                )),
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
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_slot_runs_status ON slot_runs (status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_slot_runs_slot_date
        ON slot_runs (slot_date)
        """
    )
    # which news items went into which digest issue
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digest_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_run_id INTEGER NOT NULL REFERENCES slot_runs(id),
            url TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (slot_run_id, url)
        )
        """
    )
    # rubric pauses and target overrides surviving a restart
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rubric_state (
            rubric_id TEXT PRIMARY KEY,
            paused INTEGER NOT NULL DEFAULT 0,
            target TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # content bank (plan §6): type, text, metadata, review status, show
    # counters; months is a JSON array of month numbers (seasonality),
    # event_date is 'MM-DD' for "this day in history" facts
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_bank (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            text TEXT NOT NULL,
            metadata TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (review_status IN ('pending', 'approved', 'rejected')),
            show_count INTEGER NOT NULL DEFAULT 0,
            last_shown_at TIMESTAMP,
            months TEXT,
            event_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (item_type, text)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_content_bank_type
        ON content_bank (item_type)
        """
    )
    # filter chain decisions: classifier and checker LLM answers, rejection
    # reasons (transparency mode reads them back for the daily summary)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL,
            decision TEXT NOT NULL,
            url TEXT,
            title TEXT,
            reason TEXT,
            model TEXT,
            response TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_filter_log_created
        ON filter_log (created_at)
        """
    )


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_v1,
    _migrate_v2,
    _migrate_v3,
    _migrate_v4,
]


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """Database file path (repositories are built over the same file)."""
        return self._path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._path.exists()
        with _connect(self._path) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if existed and version < len(_MIGRATIONS):
            # safety copy of a pre-existing database file before upgrading it;
            # checkpoint WAL first so the copy holds every committed row
            # (a bare file copy misses frames still sitting in the -wal file)
            with _connect(self._path) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            backup = self._path.with_name(f"{self._path.name}.bak-v{version}")
            shutil.copy2(self._path, backup)
        with _connect(self._path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for index in range(version, len(_MIGRATIONS)):
                _MIGRATIONS[index](conn)
                conn.execute(f"PRAGMA user_version = {index + 1}")
            conn.commit()

    async def log_event(self, event: str, details: str | None = None) -> None:
        await asyncio.to_thread(self._log_event_sync, event, details)

    def _log_event_sync(self, event: str, details: str | None) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                "INSERT INTO bot_stats (event, details) VALUES (?, ?)",
                (event, details),
            )
            conn.commit()

    async def get_last_event(self, event: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_last_event_sync, event)

    def _get_last_event_sync(self, event: str) -> dict[str, Any] | None:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT created_at, details
                FROM bot_stats
                WHERE event = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (event,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return {"created_at": row["created_at"], "details": row["details"]}

    async def get_daily_stats(self, days: int = 7) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_daily_stats_sync, days)

    def _get_daily_stats_sync(self, days: int) -> list[dict[str, Any]]:
        # publishes come from slot_runs (the slot publisher records delivery
        # there, not as a bot_stats event); errors stay in bot_stats.
        with _connect(self._path) as conn:
            published = {
                row["day"]: int(row["publishes"])
                for row in conn.execute(
                    """
                    SELECT date(published_at) as day, COUNT(*) as publishes
                    FROM slot_runs
                    WHERE status = 'published'
                      AND published_at >= datetime('now', ?)
                    GROUP BY day
                    """,
                    (f"-{days} days",),
                ).fetchall()
            }
            errors = {
                row["day"]: int(row["errors"])
                for row in conn.execute(
                    """
                    SELECT date(created_at) as day, COUNT(*) as errors
                    FROM bot_stats
                    WHERE event = 'error'
                      AND created_at >= datetime('now', ?)
                    GROUP BY day
                    """,
                    (f"-{days} days",),
                ).fetchall()
            }
            days_all = sorted(set(published) | set(errors), reverse=True)
            return [
                {
                    "day": day,
                    "publishes": published.get(day, 0),
                    "errors": errors.get(day, 0),
                }
                for day in days_all
            ]

