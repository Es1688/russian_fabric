from __future__ import annotations

import sqlite3

import pytest

from max_news_bot.config.loader import AppConfig
from max_news_bot.utils.db import Database


def _user_version(path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _tables(path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


@pytest.mark.asyncio
async def test_log_event_and_get_last_event(db: Database) -> None:
    await db.log_event("run", "collected=3 published=1")

    event = await db.get_last_event("run")

    assert event is not None
    assert event["details"] == "collected=3 published=1"


@pytest.mark.asyncio
async def test_get_last_event_returns_none_when_missing(db: Database) -> None:
    assert await db.get_last_event("nonexistent") is None


@pytest.mark.asyncio
async def test_get_daily_stats(db: Database) -> None:
    # publications come from slot_runs, errors from bot_stats
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            """
            INSERT INTO slot_runs (rubric_id, slot_date, status, published_at)
            VALUES ('good_morning', '2026-10-09', 'published',
                    datetime('now'))
            """
        )
        conn.commit()
    await db.log_event("error", "publisher_failed")

    stats = await db.get_daily_stats(days=7)

    assert len(stats) >= 1
    assert stats[0]["publishes"] >= 1
    assert stats[0]["errors"] >= 1


# --- migrations ---


@pytest.mark.asyncio
async def test_migrations_from_scratch(db: Database, app_config: AppConfig) -> None:
    assert _user_version(app_config.data_path) == 4

    # fresh database runs v2+v3: posts.message_id is TEXT right away. The
    # posts/sent_news tables are legacy leftovers: nothing writes them
    # anymore, but the schema must keep creating them so a pre-unification
    # production database migrates without a rebuild.
    assert {"sent_news", "bot_stats", "sources_state", "posts"} <= _tables(
        app_config.data_path
    )
    with sqlite3.connect(app_config.data_path) as conn:
        columns = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(posts)")
        }
    assert columns["message_id"] == "TEXT"

    # fresh database: no safety copy expected
    backups = list(app_config.data_path.parent.glob("*.bak-*"))
    assert backups == []


@pytest.mark.asyncio
async def test_initialize_is_idempotent(db: Database, app_config: AppConfig) -> None:
    await db.initialize()
    await db.initialize()

    assert _user_version(app_config.data_path) == 4
    backups = list(app_config.data_path.parent.glob("*.bak-*"))
    assert backups == []


@pytest.mark.asyncio
async def test_migrations_over_legacy_db(app_config: AppConfig) -> None:
    # legacy production database: tables exist, user_version == 0
    path = app_config.data_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE sent_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO sent_news (url, source, title) VALUES (?, ?, ?)",
            ("https://example.com/legacy", "Legacy", "Legacy news"),
        )
        conn.commit()

    database = Database(path)
    await database.initialize()

    assert _user_version(path) == 4
    assert "posts" in _tables(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT url FROM sent_news WHERE url = ?",
            ("https://example.com/legacy",),
        ).fetchone()
    assert row is not None  # legacy data survived

    backups = list(path.parent.glob(f"{path.name}.bak-*"))
    assert len(backups) == 1


def _make_v2_db(path) -> None:
    """A v2 database with one published post (integer message_id)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
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
                message_id INTEGER,
                rejected_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                published_at TIMESTAMP,
                last_attempt_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO posts (news_url, source, title, text, status,
                attempts, message_id, tokens, published_at)
            VALUES ('https://example.com/one', 'Habr', 'One', 't1',
                'published', 1, 4242, 500, '2026-10-01 10:00:00')
            """
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()


@pytest.mark.asyncio
async def test_migrate_v3_message_id_int_to_text(app_config: AppConfig) -> None:
    path = app_config.data_path
    _make_v2_db(path)

    database = Database(path)
    await database.initialize()

    assert _user_version(path) == 4
    with sqlite3.connect(path) as conn:
        columns = {
            row[1]: row[2] for row in conn.execute("PRAGMA table_info(posts)")
        }
        assert columns["message_id"] == "TEXT"
        # int mid became a str, NULL stayed NULL, sibling columns survived
        rows = conn.execute(
            "SELECT news_url, message_id, typeof(message_id), tokens "
            "FROM posts"
        ).fetchall()
        by_url = {row[0]: row for row in rows}
        assert by_url["https://example.com/one"][1] == "4242"
        assert by_url["https://example.com/one"][2] == "text"
        assert by_url["https://example.com/one"][3] == 500
        # UNIQUE(news_url) survived the table rebuild
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO posts (news_url, source, title, status)
                VALUES ('https://example.com/one', 'Habr', 'Dup', 'draft')
                """
            )
        # column defaults survived the table rebuild
        conn.execute(
            """
            INSERT INTO posts (news_url, source, title, status)
            VALUES ('https://example.com/three', 'Habr', 'Three', 'draft')
            """
        )
        row = conn.execute(
            """
            SELECT attempts, created_at FROM posts
            WHERE news_url = 'https://example.com/three'
            """
        ).fetchone()
        assert row[0] == 0
        assert row[1] is not None


@pytest.mark.asyncio
async def test_migrate_v3_recovers_from_dangling_posts_new(
    app_config: AppConfig,
) -> None:
    # CREATE TABLE posts_new is committed in autocommit, the copy is not:
    # a hard kill in that window leaves a dangling posts_new. The next
    # initialize() must rebuild instead of failing on "table already exists".
    path = app_config.data_path
    _make_v2_db(path)
    with sqlite3.connect(path) as conn:
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
        conn.commit()

    database = Database(path)
    await database.initialize()  # must not raise

    assert _user_version(path) == 4
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT message_id FROM posts WHERE news_url = 'https://example.com/one'"
        ).fetchone()
    assert row == ("4242",)


@pytest.mark.asyncio
async def test_migrate_v3_preserves_autoincrement_counter(
    app_config: AppConfig,
) -> None:
    # the counter above max(id) means rows were deleted before: the rebuild
    # must not reset it to max(id), or their ids would be reused
    path = app_config.data_path
    _make_v2_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sqlite_sequence SET seq = 999 WHERE name = 'posts'")
        conn.commit()

    database = Database(path)
    await database.initialize()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO posts (news_url, source, title, status)
            VALUES ('https://example.com/next', 'Habr', 'Next', 'draft')
            """
        )
        new_id = conn.execute(
            "SELECT id FROM posts WHERE news_url = 'https://example.com/next'"
        ).fetchone()[0]
    assert new_id == 1000


_V4_TABLES = ("slot_runs", "digest_items", "rubric_state", "content_bank", "filter_log")


async def _make_v3_db(path) -> None:
    """A production-shaped v3 database: full v3 schema with data, no v4 tables."""
    await Database(path).initialize()
    with sqlite3.connect(path) as conn:
        for table in _V4_TABLES:
            conn.execute(f"DROP TABLE {table}")
        conn.execute(
            "INSERT INTO sent_news (url, source, title) VALUES (?, ?, ?)",
            ("https://example.com/keep", "Habr", "Kept news"),
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()


@pytest.mark.asyncio
async def test_migrate_v4_from_v3(app_config: AppConfig) -> None:
    path = app_config.data_path
    await _make_v3_db(path)

    database = Database(path)
    await database.initialize()

    assert _user_version(path) == 4
    # pre-existing data and tables survived
    tables = _tables(path)
    assert set(_V4_TABLES) <= tables
    assert {"sent_news", "bot_stats", "sources_state", "posts"} <= tables
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT url FROM sent_news WHERE url = ?",
            ("https://example.com/keep",),
        ).fetchone()
    assert row is not None

    # safety copy of the v3 file was made before upgrading
    backups = list(path.parent.glob(f"{path.name}.bak-v3"))
    assert len(backups) == 1


@pytest.mark.asyncio
async def test_migrate_v4_slot_runs_constraints(app_config: AppConfig) -> None:
    path = app_config.data_path
    await _make_v3_db(path)
    await Database(path).initialize()

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO slot_runs (rubric_id, slot_date)
            VALUES ('good_morning', '2026-10-09')
            """
        )
        conn.commit()
        # UNIQUE(rubric_id, slot_date): one rubric slot per day
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO slot_runs (rubric_id, slot_date)
                VALUES ('good_morning', '2026-10-09')
                """
            )
        # the same rubric on another date is a different slot
        conn.execute(
            """
            INSERT INTO slot_runs (rubric_id, slot_date)
            VALUES ('good_morning', '2026-10-10')
            """
        )
        # status vocabulary is checked by the database
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO slot_runs (rubric_id, slot_date, status)
                VALUES ('weather', '2026-10-09', 'exploded')
                """
            )
        # default status is pending, timestamps default to UTC now
        row = conn.execute(
            """
            SELECT status, created_at, updated_at FROM slot_runs WHERE id = 1
            """
        ).fetchone()
        assert row[0] == "pending"
        assert row[1] is not None and row[2] is not None


@pytest.mark.asyncio
async def test_migration_backup_includes_wal_frames(app_config: AppConfig) -> None:
    # a safety copy of just the db file misses committed rows that still sit
    # in the -wal file: initialize() must checkpoint WAL before copying
    path = app_config.data_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # an open connection prevents the close-time checkpoint, so committed
    # rows below exist only in the -wal file while initialize() runs
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        """
        CREATE TABLE sent_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    writer.execute(
        "INSERT INTO sent_news (url, source, title) VALUES (?, ?, ?)",
        ("https://example.com/legacy", "Legacy", "Legacy news"),
    )
    writer.commit()

    await Database(path).initialize()

    backup = path.with_name(f"{path.name}.bak-v0")
    assert backup.exists()
    with sqlite3.connect(backup) as conn:
        row = conn.execute(
            "SELECT url FROM sent_news WHERE url = ?", ("https://example.com/legacy",)
        ).fetchone()
    assert row is not None
    writer.close()
