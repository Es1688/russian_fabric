"""Daily database backup task of the rubrics contour (plan section 9).

SQLite keeps the whole channel state in one file, so the plan asks for a
daily backup as a scheduler task, via ``VACUUM INTO`` (an online, consistent
snapshot that also checkpoints WAL frames into the copy - a bare file copy
would miss frames still sitting in the ``-wal`` file).

Delivery mirrors ``bot/daily_summary.py``: fire once per local day inside a
fixed slot window (default 23:10 Europe/Moscow - after the 22:40 summary,
before the day turns), record the done day as a bot_stats event and skip it
on a restart, so the backup is never duplicated. Old copies are pruned to
the last ``keep`` days.

The same daily maintenance is the TTL safety net for generated slot images
(dual-channel plan §3.3): the publisher deletes ``data/generated`` files
right after the confirmed publish, and this service removes whatever is
older than ``GENERATED_TTL_DAYS`` - leftovers of failed publishes and dead
slot runs.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from max_news_bot.bot.daily_summary import parse_summary_time
from max_news_bot.publishers.alerter import Alerter
from max_news_bot.utils.db import Database, _connect
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

BACKUP_EVENT = "daily_backup"
# After the 22:40 admin summary and the 21:30 last post, before midnight:
# the backup captures the finished day.
DEFAULT_BACKUP_TIME = "23:10"
# A restart inside this window still takes today's backup exactly once.
BACKUP_GRACE_MINUTES = 30
BACKUP_TICK_SECONDS = 60.0
DEFAULT_KEEP_BACKUPS = 7
# TTL of generated slot images (dual-channel plan §3.3): the publisher
# deletes the file on confirmed publish, this bounds crash leftovers.
GENERATED_TTL_DAYS = 7

# Backup file names this service owns inside its directory: stem + ISO date.
_BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.db$")


class DailyBackupService:
    """One ``VACUUM INTO`` copy of the database per local day."""

    def __init__(
        self,
        db: Database,
        *,
        directory: Path | None = None,
        generated_dir: Path | None = None,
        tz_name: str = "Europe/Moscow",
        backup_time: str = DEFAULT_BACKUP_TIME,
        keep: int = DEFAULT_KEEP_BACKUPS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._directory = (
            directory if directory is not None else db.path.parent / "backups"
        )
        self._generated_dir = (
            generated_dir if generated_dir is not None else db.path.parent / "generated"
        )
        self._tz = ZoneInfo(tz_name)
        self._hour, self._minute = parse_summary_time(backup_time)
        if keep < 1:
            raise ValueError(f"keep must be >= 1, got {keep}")
        self._keep = keep
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def directory(self) -> Path:
        """Directory the backup files are written to."""
        return self._directory

    @property
    def generated_dir(self) -> Path:
        """Directory of generated slot images pruned by TTL."""
        return self._generated_dir

    async def backup_if_due(self, now: datetime | None = None) -> bool:
        """Take the backup when the local time is inside today's slot window.

        Returns True when a new backup file was actually written. The
        once-per-day rule is durable: the done day is recorded as a
        bot_stats event and a restarted process reads it back instead of
        re-copying; an existing file for the day counts as done too.
        """
        now = now if now is not None else self._clock()
        local = now.astimezone(self._tz)
        slot = local.replace(
            hour=self._hour, minute=self._minute, second=0, microsecond=0
        )
        if not slot <= local <= slot + timedelta(minutes=BACKUP_GRACE_MINUTES):
            return False
        day = local.date().isoformat()
        last = await self._db.get_last_event(BACKUP_EVENT)
        if last is not None and (last.get("details") or "").startswith(day):
            return False  # already taken today
        dest = self._directory / f"{self._db.path.stem}-{day}.db"
        created = await self._backup(dest)
        await self._prune()
        await self._prune_generated()
        await self._db.log_event(BACKUP_EVENT, f"{day} file={dest.name}")
        if not created:
            logger.info("db_backup_already_present date=%s", day)
        else:
            logger.info("db_backup_taken date=%s path=%s", day, dest)
        return created

    # --- internals ---------------------------------------------------------------

    async def _backup(self, dest: Path) -> bool:
        """Write the snapshot; False when the file for the day exists."""
        if dest.exists():
            return False
        return await asyncio.to_thread(self._backup_sync, dest)

    def _backup_sync(self, dest: Path) -> bool:
        # VACUUM INTO refuses to overwrite an existing file, so the snapshot
        # goes to a temp name first and the finished copy is renamed into
        # place; a stale temp file (crash mid-backup) is removed upfront.
        self._directory.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.tmp")
        if tmp.exists():
            tmp.unlink()
        with _connect(self._db.path) as conn:
            conn.execute("VACUUM INTO ?", (str(tmp),))
        tmp.rename(dest)
        return True

    async def _prune(self) -> None:
        """Keep only the last ``keep`` daily copies (date-named files only)."""
        await asyncio.to_thread(self._prune_sync)

    def _prune_sync(self) -> None:
        if not self._directory.exists():
            return
        prefix = f"{self._db.path.stem}-"
        dated = sorted(
            path
            for path in self._directory.iterdir()
            if path.name.startswith(prefix)
            and _BACKUP_NAME_RE.match(path.name[len(prefix):])
        )
        for path in dated[: -self._keep]:
            try:
                path.unlink()
                logger.info("db_backup_pruned path=%s", path)
            except OSError as exc:
                logger.error("db_backup_prune_failed path=%s error=%s", path, exc)

    async def _prune_generated(self) -> None:
        await asyncio.to_thread(self._prune_generated_sync)

    def _prune_generated_sync(self) -> None:
        """Remove generated images older than GENERATED_TTL_DAYS.

        Only the ``*.png`` files this contour writes are touched (mtime is
        the age signal - run ids carry no date); any other file in the
        directory is left alone, like with the backup prune."""
        if not self._generated_dir.exists():
            return
        cutoff = self._clock().timestamp() - GENERATED_TTL_DAYS * 24 * 60 * 60
        for path in self._generated_dir.glob("*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    logger.info("generated_image_pruned path=%s", path)
            except OSError as exc:
                logger.error(
                    "generated_image_prune_failed path=%s error=%s", path, exc
                )


async def daily_backup_loop(
    service: DailyBackupService,
    db: Database,
    *,
    tick_seconds: float = BACKUP_TICK_SECONDS,
    alerter: Alerter | None = None,
) -> None:
    """Poll the backup slot once a minute and shield failures forever.

    A silent backup death is the "молчаливые сбои" risk of plan section 15:
    every failure is logged as an error event and reported to the admins.
    """
    while True:
        try:
            await service.backup_if_due()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("daily_backup_error error=%s", exc)
            await db.log_event("error", f"daily_backup_error={exc}")
            if alerter is not None:
                try:
                    await alerter.send_alert(f"Daily database backup failed: {exc}")
                except Exception as alert_exc:
                    logger.error("daily_backup_alert_failed error=%s", alert_exc)
        await asyncio.sleep(tick_seconds)
