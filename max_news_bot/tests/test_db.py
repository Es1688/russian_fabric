from __future__ import annotations

import sqlite3

import pytest

from max_news_bot.config.loader import AppConfig
from max_news_bot.core.models import NewsItem, Post
from max_news_bot.utils.db import Database


def _user_version(path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _draft(url: str = "https://example.com/post-1", **overrides) -> Post:
    fields = {
        "id": None,
        "news_url": url,
        "source": "Habr",
        "title": "Some title",
        "text": "Some text",
        "status": "draft",
    }
    fields.update(overrides)
    return Post(**fields)


@pytest.mark.asyncio
async def test_new_item_is_not_sent(db: Database, sample_item: NewsItem) -> None:
    assert await db.is_sent(sample_item.url) is False


@pytest.mark.asyncio
async def test_mark_sent_makes_item_sent(db: Database, sample_item: NewsItem) -> None:
    await db.mark_sent(sample_item)
    assert await db.is_sent(sample_item.url) is True


@pytest.mark.asyncio
async def test_mark_sent_idempotent(db: Database, sample_item: NewsItem) -> None:
    await db.mark_sent(sample_item)
    await db.mark_sent(sample_item)
    assert await db.is_sent(sample_item.url) is True


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
    await db.log_event("publish", "published=2")
    await db.log_event("error", "publisher_failed")

    stats = await db.get_daily_stats(days=7)

    assert len(stats) >= 1
    assert stats[0]["publishes"] >= 1
    assert stats[0]["errors"] >= 1


# --- migrations ---


@pytest.mark.asyncio
async def test_migrations_from_scratch(db: Database, app_config: AppConfig) -> None:
    assert _user_version(app_config.data_path) == 2

    with sqlite3.connect(app_config.data_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"sent_news", "bot_stats", "sources_state", "posts"} <= tables

    # fresh database: no safety copy expected
    backups = list(app_config.data_path.parent.glob("*.bak-*"))
    assert backups == []


@pytest.mark.asyncio
async def test_initialize_is_idempotent(db: Database, app_config: AppConfig) -> None:
    await db.initialize()
    await db.initialize()

    assert _user_version(app_config.data_path) == 2
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

    assert _user_version(path) == 2
    assert await database.is_sent("https://example.com/legacy") is True
    assert await database.is_post_written("https://example.com/whatever") is False

    backups = list(path.parent.glob(f"{path.name}.bak-*"))
    assert len(backups) == 1


# --- posts ---


@pytest.mark.asyncio
async def test_save_attempt_insert_then_upsert(db: Database) -> None:
    post_id = await db.save_attempt(_draft())

    same_id = await db.save_attempt(
        _draft(status="failed", attempts=1, text=None, error="boom")
    )

    assert same_id == post_id

    posts = await db.get_recent_posts(10)
    assert len(posts) == 1
    assert posts[0].status == "failed"
    assert posts[0].attempts == 1
    assert posts[0].text is None
    assert posts[0].error == "boom"


@pytest.mark.asyncio
async def test_save_attempt_updates_last_attempt_at(db: Database, app_config: AppConfig) -> None:
    post_id = await db.save_attempt(_draft())

    with sqlite3.connect(app_config.data_path) as conn:
        conn.execute(
            "UPDATE posts SET last_attempt_at = NULL WHERE id = ?", (post_id,)
        )
        conn.commit()

    await db.save_attempt(_draft())

    posts = await db.get_recent_posts(10)
    assert posts[0].last_attempt_at is not None


@pytest.mark.asyncio
async def test_save_attempt_truncates_long_error(db: Database) -> None:
    await db.save_attempt(_draft(error="E" * 500))

    posts = await db.get_recent_posts(10)
    assert posts[0].error is not None
    assert len(posts[0].error) == 300


@pytest.mark.asyncio
async def test_is_post_written_by_status(db: Database) -> None:
    url = "https://example.com/post-1"
    assert await db.is_post_written(url) is False

    for status in ("draft", "published", "previewed", "skipped"):
        await db.save_attempt(_draft(status=status))
        assert await db.is_post_written(url) is True

    # failed alone is not "written"
    await db.save_attempt(_draft(status="failed"))
    assert await db.is_post_written(url) is False


@pytest.mark.asyncio
async def test_mark_post_published(db: Database) -> None:
    post_id = await db.save_attempt(_draft())

    await db.mark_post_published(post_id, 4242)

    posts = await db.get_recent_posts(10)
    assert posts[0].status == "published"
    assert posts[0].message_id == 4242
    assert posts[0].published_at is not None


@pytest.mark.asyncio
async def test_mark_post_published_without_message_id(db: Database) -> None:
    post_id = await db.save_attempt(_draft())

    await db.mark_post_published(post_id, None)

    posts = await db.get_recent_posts(10)
    assert posts[0].status == "published"
    assert posts[0].message_id is None


@pytest.mark.asyncio
async def test_mark_post_failed(db: Database) -> None:
    post_id = await db.save_attempt(_draft())

    await db.mark_post_failed(post_id, "E" * 500)

    posts = await db.get_recent_posts(10)
    assert posts[0].status == "failed"
    assert posts[0].error is not None
    assert len(posts[0].error) == 300


@pytest.mark.asyncio
async def test_find_stale_drafts(db: Database, app_config: AppConfig) -> None:
    await db.save_attempt(_draft(url="https://example.com/stale"))
    await db.save_attempt(_draft(url="https://example.com/fresh"))
    with sqlite3.connect(app_config.data_path) as conn:
        conn.execute(
            """
            UPDATE posts SET created_at = datetime('now', '-30 minutes')
            WHERE news_url = ?
            """,
            ("https://example.com/stale",),
        )
        conn.commit()

    stale = await db.find_stale_drafts(older_than_minutes=10)

    assert [post.news_url for post in stale] == ["https://example.com/stale"]

    published_id = await db.save_attempt(
        _draft(url="https://example.com/old-published", status="draft")
    )
    await db.mark_post_published(published_id, 1)
    with sqlite3.connect(app_config.data_path) as conn:
        conn.execute(
            "UPDATE posts SET created_at = datetime('now', '-2 hours') WHERE id = ?",
            (published_id,),
        )
        conn.commit()

    stale = await db.find_stale_drafts(older_than_minutes=10)
    assert [post.news_url for post in stale] == ["https://example.com/stale"]


@pytest.mark.asyncio
async def test_count_attempts_today(db: Database) -> None:
    assert await db.count_attempts_today() == 0

    await db.save_attempt(_draft(url="https://example.com/a"))
    assert await db.count_attempts_today() == 1

    # UPSERT retry of the same news: one row, still one attempt-today
    await db.save_attempt(_draft(url="https://example.com/a", status="failed"))
    assert await db.count_attempts_today() == 1

    await db.save_attempt(_draft(url="https://example.com/b"))
    assert await db.count_attempts_today() == 2


@pytest.mark.asyncio
async def test_get_recent_posts_order_and_limit(db: Database) -> None:
    for index in range(5):
        await db.save_attempt(_draft(url=f"https://example.com/{index}"))

    posts = await db.get_recent_posts(3)

    assert [post.news_url for post in posts] == [
        "https://example.com/4",
        "https://example.com/3",
        "https://example.com/2",
    ]
    assert all(post.id is not None for post in posts)
    assert all(post.created_at is not None for post in posts)


# --- sources_state flags ---


@pytest.mark.asyncio
async def test_state_flag_defaults_to_enabled(db: Database) -> None:
    assert await db.get_state_flag("factory") is True
    assert await db.get_state_flag("factory_autopause") is True


@pytest.mark.asyncio
async def test_state_flag_set_and_update(db: Database) -> None:
    await db.set_state_flag("factory", False)
    assert await db.get_state_flag("factory") is False

    await db.set_state_flag("factory", True)
    assert await db.get_state_flag("factory") is True

    await db.set_state_flag("factory_autopause", False)
    assert await db.get_state_flag("factory_autopause") is False
    assert await db.get_state_flag("factory") is True
