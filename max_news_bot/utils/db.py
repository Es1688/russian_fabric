from __future__ import annotations

import asyncio
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from max_news_bot.core.models import NewsItem, Post

# sqlite3.connect(timeout=...) is the busy_timeout in seconds; both the
# digest loop and the factory loop share this database file.
_BUSY_TIMEOUT_SECONDS = 30.0
_MAX_ERROR_LEN = 300

# posts statuses treated as "news already handled by the factory"
_POST_WRITTEN_STATUSES = ("draft", "published", "previewed", "skipped")


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


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_v1,
    _migrate_v2,
    _migrate_v3,
]


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_db_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_post(row: sqlite3.Row) -> Post:
    return Post(
        id=row["id"],
        news_url=row["news_url"],
        source=row["source"],
        title=row["title"],
        text=row["text"],
        status=row["status"],
        attempts=row["attempts"],
        error=row["error"],
        prompt_version=row["prompt_version"],
        llm_model=row["llm_model"],
        tokens=row["tokens"],
        message_id=row["message_id"],
        rejected_text=row["rejected_text"],
        created_at=_parse_dt(row["created_at"]),
        published_at=_parse_dt(row["published_at"]),
        last_attempt_at=_parse_dt(row["last_attempt_at"]),
    )


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path

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

    async def is_sent(self, url: str) -> bool:
        return await asyncio.to_thread(self._is_sent_sync, url)

    def _is_sent_sync(self, url: str) -> bool:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                "SELECT 1 FROM sent_news WHERE url = ? LIMIT 1", (url,)
            )
            return cursor.fetchone() is not None

    async def mark_sent(self, item: NewsItem) -> None:
        await asyncio.to_thread(self._mark_sent_sync, item)

    def _mark_sent_sync(self, item: NewsItem) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sent_news (url, source, title)
                VALUES (?, ?, ?)
                """,
                (item.url, item.source, item.title),
            )
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
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    date(created_at) as day,
                    SUM(CASE WHEN event = 'publish' THEN 1 ELSE 0 END) as publishes,
                    SUM(CASE WHEN event = 'error' THEN 1 ELSE 0 END) as errors
                FROM bot_stats
                WHERE created_at >= datetime('now', ?)
                GROUP BY day
                ORDER BY day DESC
                """,
                (f"-{days} days",),
            )
            rows = cursor.fetchall()
            return [
                {"day": row["day"], "publishes": row["publishes"], "errors": row["errors"]}
                for row in rows
            ]

    async def is_post_written(self, url: str) -> bool:
        return await asyncio.to_thread(self._is_post_written_sync, url)

    def _is_post_written_sync(self, url: str) -> bool:
        with _connect(self._path) as conn:
            placeholders = ",".join("?" for _ in _POST_WRITTEN_STATUSES)
            cursor = conn.execute(
                f"""
                SELECT 1 FROM posts
                WHERE news_url = ? AND status IN ({placeholders})
                LIMIT 1
                """,
                (url, *_POST_WRITTEN_STATUSES),
            )
            return cursor.fetchone() is not None

    async def save_attempt(self, post: Post) -> int:
        return await asyncio.to_thread(self._save_attempt_sync, post)

    def _save_attempt_sync(self, post: Post) -> int:
        published_at = (
            _to_db_dt(post.published_at) if post.published_at is not None else None
        )
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO posts (
                    news_url, source, title, text, status, attempts, error,
                    prompt_version, llm_model, tokens, message_id, rejected_text,
                    published_at, last_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(news_url) DO UPDATE SET
                    source = excluded.source,
                    title = excluded.title,
                    text = excluded.text,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    error = excluded.error,
                    prompt_version = excluded.prompt_version,
                    llm_model = excluded.llm_model,
                    tokens = excluded.tokens,
                    message_id = excluded.message_id,
                    rejected_text = excluded.rejected_text,
                    published_at = excluded.published_at,
                    last_attempt_at = CURRENT_TIMESTAMP
                """,
                (
                    post.news_url,
                    post.source,
                    post.title,
                    post.text,
                    post.status,
                    post.attempts,
                    post.error[:_MAX_ERROR_LEN] if post.error else None,
                    post.prompt_version,
                    post.llm_model,
                    post.tokens,
                    post.message_id,
                    post.rejected_text,
                    published_at,
                ),
            )
            cursor = conn.execute(
                "SELECT id FROM posts WHERE news_url = ?", (post.news_url,)
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row[0])

    async def mark_post_published(
        self, post_id: int, message_id: str | None
    ) -> None:
        await asyncio.to_thread(
            self._mark_post_published_sync, post_id, message_id
        )

    def _mark_post_published_sync(
        self, post_id: int, message_id: str | None
    ) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                UPDATE posts
                SET status = 'published',
                    message_id = ?,
                    published_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message_id, post_id),
            )
            conn.commit()

    async def mark_post_failed(self, post_id: int, error: str) -> None:
        await asyncio.to_thread(self._mark_post_failed_sync, post_id, error)

    def _mark_post_failed_sync(self, post_id: int, error: str) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                "UPDATE posts SET status = 'failed', error = ? WHERE id = ?",
                (error[:_MAX_ERROR_LEN], post_id),
            )
            conn.commit()

    async def find_stale_drafts(self, older_than_minutes: int) -> list[Post]:
        return await asyncio.to_thread(
            self._find_stale_drafts_sync, older_than_minutes
        )

    def _find_stale_drafts_sync(self, older_than_minutes: int) -> list[Post]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM posts
                WHERE status = 'draft'
                  AND created_at <= datetime('now', ?)
                ORDER BY created_at ASC
                """,
                (f"-{older_than_minutes} minutes",),
            )
            return [_row_to_post(row) for row in cursor.fetchall()]

    async def count_attempts_today(self) -> int:
        return await asyncio.to_thread(self._count_attempts_today_sync)

    def _count_attempts_today_sync(self) -> int:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM posts
                WHERE last_attempt_at IS NOT NULL
                  AND date(last_attempt_at) = date('now')
                """
            )
            return int(cursor.fetchone()[0])

    async def get_recent_posts(self, limit: int) -> list[Post]:
        return await asyncio.to_thread(self._get_recent_posts_sync, limit)

    def _get_recent_posts_sync(self, limit: int) -> list[Post]:
        with _connect(self._path) as conn:
            cursor = conn.execute(
                "SELECT * FROM posts ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [_row_to_post(row) for row in cursor.fetchall()]

    async def get_state_flag(self, name: str) -> bool:
        return await asyncio.to_thread(self._get_state_flag_sync, name)

    def _get_state_flag_sync(self, name: str) -> bool:
        # Absent flag means "enabled" (schema default is 1), e.g. a fresh
        # database has no paused factory.
        with _connect(self._path) as conn:
            cursor = conn.execute(
                "SELECT enabled FROM sources_state WHERE name = ?", (name,)
            )
            row = cursor.fetchone()
            if row is None:
                return True
            return bool(row[0])

    async def set_state_flag(self, name: str, value: bool) -> None:
        await asyncio.to_thread(self._set_state_flag_sync, name, value)

    def _set_state_flag_sync(self, name: str, value: bool) -> None:
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO sources_state (name, enabled, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (name, int(value)),
            )
            conn.commit()
