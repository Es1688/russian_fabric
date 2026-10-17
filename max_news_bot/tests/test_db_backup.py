"""Tests of the daily database backup task (plan section 9).

``VACUUM INTO`` snapshot semantics: the copy must be a standalone, valid
sqlite database holding every committed row (including frames still in
the WAL), taken once per local day inside the slot window, never twice
after a restart, with old copies pruned.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from max_news_bot.bot.db_backup import (
    BACKUP_EVENT,
    GENERATED_TTL_DAYS,
    DailyBackupService,
    daily_backup_loop,
)
from max_news_bot.utils.db import Database

TZ = ZoneInfo("Europe/Moscow")


class RecordingAlerter:
    def __init__(self) -> None:
        self.alerts: list[str] = []

    async def send_alert(self, text: str) -> None:
        self.alerts.append(text)

    async def send_recovery(self, text: str) -> None:  # pragma: no cover
        pass


def at_backup_slot(day: str = "2026-10-10", minutes: int = 0) -> datetime:
    """A UTC moment inside the 23:10 Europe/Moscow backup window."""
    local = datetime.fromisoformat(f"{day}T23:10:00").replace(tzinfo=TZ)
    return (local + timedelta(minutes=minutes)).astimezone(timezone.utc)


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "max_news_bot.db")
    await database.initialize()
    return database


async def test_backup_inside_window_creates_valid_copy(
    db: Database, tmp_path: Path
) -> None:
    # a committed row (bot_stats is written through WAL) must be in the copy
    await db.log_event("publish", "one post")
    service = DailyBackupService(db, directory=tmp_path / "backups")

    created = await service.backup_if_due(now=at_backup_slot())

    assert created is True
    backup = tmp_path / "backups" / "max_news_bot-2026-10-10.db"
    assert backup.exists()
    with sqlite3.connect(backup) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        events = conn.execute("SELECT event FROM bot_stats").fetchall()
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert version >= 4  # migrations applied
    assert ("publish",) in events
    assert journal != "wal"  # the copy is standalone, no -wal dependency
    marker = await db.get_last_event(BACKUP_EVENT)
    assert marker is not None and marker["details"].startswith("2026-10-10")


async def test_backup_once_per_day_marker_survives_restart(
    db: Database, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"
    first = DailyBackupService(db, directory=directory)
    assert await first.backup_if_due(now=at_backup_slot()) is True

    # a restarted process (new service instance) reads the marker back
    second = DailyBackupService(db, directory=directory)
    assert await second.backup_if_due(now=at_backup_slot(minutes=5)) is False
    assert list(directory.iterdir()) == [
        directory / "max_news_bot-2026-10-10.db"
    ]


async def test_existing_file_counts_as_done_without_marker(
    db: Database, tmp_path: Path
) -> None:
    # the marker row was lost but the file exists: no VACUUM INTO crash,
    # the day simply counts as taken
    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / "max_news_bot-2026-10-10.db").write_bytes(b"placeholder")

    service = DailyBackupService(db, directory=directory)
    created = await service.backup_if_due(now=at_backup_slot())

    assert created is False
    marker = await db.get_last_event(BACKUP_EVENT)
    assert marker is not None  # the day is recorded as done


async def test_outside_window_no_backup(db: Database, tmp_path: Path) -> None:
    service = DailyBackupService(db, directory=tmp_path / "backups")

    before = await service.backup_if_due(now=at_backup_slot(minutes=-1))
    after_grace = await service.backup_if_due(now=at_backup_slot(minutes=31))

    assert before is False
    assert after_grace is False
    assert not (tmp_path / "backups").exists()
    assert await db.get_last_event(BACKUP_EVENT) is None


async def test_next_day_backs_up_again(db: Database, tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    service = DailyBackupService(db, directory=directory)
    assert await service.backup_if_due(now=at_backup_slot(day="2026-10-10")) is True

    created = await service.backup_if_due(now=at_backup_slot(day="2026-10-11"))

    assert created is True
    assert (directory / "max_news_bot-2026-10-11.db").exists()


async def test_prune_keeps_last_n_copies(db: Database, tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    for day in range(1, 11):
        (directory / f"max_news_bot-2026-10-{day:02d}.db").write_bytes(b"x")
    # a foreign file in the directory is never touched by pruning
    foreign = directory / "unrelated.db"
    foreign.write_bytes(b"x")

    service = DailyBackupService(db, directory=directory, keep=7)
    created = await service.backup_if_due(now=at_backup_slot())

    assert created is False  # the "today" file already existed
    remaining = sorted(path.name for path in directory.iterdir())
    assert remaining == [
        # days 4..10 kept, days 1..3 pruned, plus today's marker file
        "max_news_bot-2026-10-04.db",
        "max_news_bot-2026-10-05.db",
        "max_news_bot-2026-10-06.db",
        "max_news_bot-2026-10-07.db",
        "max_news_bot-2026-10-08.db",
        "max_news_bot-2026-10-09.db",
        "max_news_bot-2026-10-10.db",
        "unrelated.db",
    ]


async def test_invalid_keep_rejected(db: Database) -> None:
    with pytest.raises(ValueError, match="keep"):
        DailyBackupService(db, keep=0)


# --- generated images TTL-prune (dual-channel plan §3.3) ------------------------


async def test_generated_prune_removes_only_old_png_files(
    db: Database, tmp_path: Path
) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    old, fresh, foreign = (
        generated / "101.png",
        generated / "102.png",
        generated / "notes.txt",
    )
    for path in (old, fresh, foreign):
        path.write_bytes(b"x")
    stale = (datetime.now(timezone.utc) - timedelta(days=GENERATED_TTL_DAYS + 1)).timestamp()
    os.utime(old, (stale, stale))
    os.utime(foreign, (stale, stale))  # old but not this contour's file

    service = DailyBackupService(db, directory=tmp_path / "backups", generated_dir=generated)
    assert await service.backup_if_due(now=at_backup_slot()) is True

    assert not old.exists()  # older than the TTL: gone
    assert fresh.exists()  # young: untouched
    assert foreign.exists()  # only *.png files are this service's business


async def test_generated_dir_defaults_next_to_the_database(db: Database) -> None:
    service = DailyBackupService(db, directory=db.path.parent / "backups")
    assert service.generated_dir == db.path.parent / "generated"


async def test_loop_shields_failure_and_alerts(db: Database) -> None:
    class BrokenService:
        async def backup_if_due(self, now=None) -> bool:
            raise RuntimeError("disk full")

    alerter = RecordingAlerter()
    task = asyncio.create_task(
        daily_backup_loop(BrokenService(), db, tick_seconds=0.01, alerter=alerter)
    )
    try:
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert alerter.alerts, "a silent backup death must alert the admins"
    assert all("disk full" in text for text in alerter.alerts)
