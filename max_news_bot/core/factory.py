from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from max_news_bot.config.loader import AppConfig
from max_news_bot.core.models import NewsItem, Post
from max_news_bot.parsers.base import Fetcher
from max_news_bot.publishers.alerter import Alerter
from max_news_bot.publishers.post import PostPublishResult, PostPublisherProtocol
from max_news_bot.utils.db import Database
from max_news_bot.utils.filters import KeywordFilter
from max_news_bot.utils.logging import get_logger
from max_news_bot.utils.text import canonical_url, jaccard, title_tokens
from max_news_bot.writers.base import WriteResult, Writer
from max_news_bot.writers.validator import validate_post_text


logger = get_logger(__name__)

# posts statuses treated as "already handled" (mirror of db._POST_WRITTEN_STATUSES)
_POST_WRITTEN_STATUSES = ("draft", "published", "previewed", "skipped")

_RECENT_POSTS_LIMIT = 200  # dedup horizon: ~20 days at max_posts_per_day
_TITLE_DUP_DAYS = 7
_TITLE_DUP_JACCARD = 0.7
_REJECTED_TEXT_MAX_CHARS = 2000
_ERROR_MAX_CHARS = 300
_MIN_POST_CHARS = 200  # validator floor from the plan (~200)

_ACTIVE_HOURS_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s*$"
)


@dataclass(frozen=True)
class FactoryResult:
    collected: int
    filtered: int
    deduped: int
    written: int
    skipped: int
    published: int
    previewed: int
    failed: int
    recovered: int


def parse_active_hours(spec: str | None) -> tuple[time, time] | None:
    """Parse "HH:MM-HH:MM" into (start, end); None means always active."""
    if spec is None or not str(spec).strip():
        return None
    match = _ACTIVE_HOURS_RE.match(str(spec))
    if match is None:
        raise ValueError(f"invalid factory.active_hours: {spec!r}")
    start = time(int(match.group(1)), int(match.group(2)))
    end = time(int(match.group(3)), int(match.group(4)))
    return start, end


def is_within_active_hours(moment: time, window: tuple[time, time]) -> bool:
    start, end = window
    if start <= end:
        return start <= moment < end
    # overnight window (e.g. 22:00-06:00)
    return moment >= start or moment < end


def _load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("unknown timezone=%s, falling back to UTC", name)
        return ZoneInfo("UTC")


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS]


class ContentFactory:
    """Second bot cycle: news -> LLM post -> auto-publish to the channel.

    Error classes (plan P0.3): content bumps ``attempts``, infra does not and
    trips the circuit breaker, config/access auto-pauses the factory and
    alerts the admins immediately.
    """

    def __init__(
        self,
        config: AppConfig,
        fetcher: Fetcher,
        writer: Writer,
        publisher: PostPublisherProtocol,
        alerter: Alerter,
        db: Database,
    ) -> None:
        self._config = config
        self._cfg = config.factory
        self._fetcher = fetcher
        self._writer = writer
        self._publisher = publisher
        self._alerter = alerter
        self._db = db
        self._filter = KeywordFilter(
            include_keywords=self._cfg.include_keywords,
            exclude_keywords=self._cfg.exclude_keywords,
        )
        self._tz = _load_timezone(self._cfg.timezone)
        self._active_window = parse_active_hours(self._cfg.active_hours)
        # failed-cycle streak lives in process memory; the "alert already
        # sent" fact is durable via bot_stats events (factory_alert /
        # factory_recovery), so a restart cannot spam a second alert.
        self._failed_streak = 0
        self._alert_sent = False
        self._alert_active: bool | None = None  # resolved lazily from the db

    async def run(self) -> FactoryResult:
        try:
            return await self._run_cycle()
        except asyncio.CancelledError:
            # task cancellation is not an attempt: no rows, no events
            raise

    async def _run_cycle(self) -> FactoryResult:
        # (0) kill switches first: manual pause, config autopause, quiet hours
        if not await self._db.get_state_flag("factory"):
            await self._idle_heartbeat("paused")
            return self._empty_result()
        if not await self._db.get_state_flag("factory_autopause"):
            await self._idle_heartbeat("autopause")
            return self._empty_result()
        if not self._is_active_now():
            await self._idle_heartbeat("quiet_hours")
            return self._empty_result()

        # (1) recovery: stale drafts are re-sent without calling the LLM;
        # occupies per-cycle and per-day budget like a normal attempt
        per_cycle_left = self._cfg.posts_per_cycle
        day_attempts = await self._db.count_attempts_today()
        recovered = 0
        failed = 0
        for post in await self._db.find_stale_drafts(self._cfg.stale_draft_minutes):
            if per_cycle_left <= 0 or day_attempts >= self._cfg.max_posts_per_day:
                break
            per_cycle_left -= 1
            day_attempts += 1
            if await self._recover_post(post):
                recovered += 1
            else:
                failed += 1

        # (2) collect from enabled sources
        items: list[NewsItem] = []
        for source in self._config.sources:
            if not source.enabled:
                continue
            try:
                fetched = await self._fetcher.fetch(
                    source,
                    max_news=self._cfg.max_news_per_source,
                    timeout=self._config.settings.request_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("factory_fetch_failed source=%s error=%s", source.name, exc)
                continue
            items.extend(fetched)
        collected = len(items)

        # (3) factory's own keyword filter (by title)
        filtered_items = [item for item in items if self._filter.passes(item)]
        filtered = len(filtered_items)

        # (4)+(5) age filter and URL canonicalization
        now = datetime.now(timezone.utc)
        max_age = timedelta(hours=self._cfg.max_news_age_hours)
        aged: list[tuple[str, NewsItem]] = []
        for item in filtered_items:
            if item.published_at is None:
                continue  # no date -> never selected by the factory
            if now - item.published_at > max_age:
                continue
            aged.append((canonical_url(item.url), item))

        # (6) dedup: posts table, fuzzy titles
        candidates = await self._deduplicate(aged, now)
        deduped = len(candidates)

        # (7)+(8) write and publish under per-cycle / per-day limits
        written = 0
        skipped = 0
        published = 0
        previewed = 0
        infra_streak = 0
        autopause = False
        latencies: list[int] = []
        tokens_total = 0
        for url, item, existing in candidates:
            if per_cycle_left <= 0 or day_attempts >= self._cfg.max_posts_per_day:
                break
            per_cycle_left -= 1
            day_attempts += 1

            try:
                result = await self._writer.write_post(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # writer must not crash the cycle
                result = WriteResult(
                    status="error",
                    post=None,
                    error_kind="infra",
                    error=_safe_error(exc),
                )
            latencies.append(result.latency_ms)
            tokens_total += result.tokens
            base_attempts = existing.attempts if existing is not None else 0

            if result.status == "skipped":
                # terminal: the skipped row itself dedups the news away
                skip_post = (
                    result.post
                    if result.post is not None
                    else Post(
                        id=None,
                        news_url=url,
                        source=item.source,
                        title=item.title,
                        text=None,
                        status="skipped",
                    )
                )
                await self._db.save_attempt(
                    replace(skip_post, news_url=url, status="skipped")
                )
                skipped += 1
                infra_streak = 0
                continue

            if result.status == "error":
                error_post = Post(
                    id=None,
                    news_url=url,
                    source=item.source,
                    title=item.title,
                    text=None,
                    status="failed",
                    attempts=base_attempts,
                    error=result.error,
                )
                if result.error_kind == "config":
                    # no attempts penalty, immediate alert, auto-pause, stop
                    await self._db.save_attempt(error_post)
                    failed += 1
                    autopause = True
                    await self._break_with_autopause(result.error or "config error")
                    break
                if result.error_kind == "infra":
                    await self._db.save_attempt(error_post)
                    failed += 1
                    infra_streak += 1
                    if infra_streak >= self._cfg.circuit_breaker_after:
                        logger.warning(
                            "factory circuit breaker after %s infra errors",
                            infra_streak,
                        )
                        break
                    continue
                # content error: the only class that burns attempts
                await self._db.save_attempt(
                    replace(error_post, attempts=base_attempts + 1)
                )
                failed += 1
                infra_streak = 0
                continue

            # status == "written": validate the LLM output (the factory
            # validates, not the writer) before anything is sent
            post = result.post
            if post is None or not post.text:
                # contract violation: a "written" result must carry post text
                await self._db.save_attempt(
                    Post(
                        id=None,
                        news_url=url,
                        source=item.source,
                        title=item.title,
                        text=None,
                        status="failed",
                        attempts=base_attempts + 1,
                        error="writer returned no post text",
                    )
                )
                failed += 1
                infra_streak = 0
                continue
            # carry the accumulated attempts so a send failure after a
            # rewrite bumps the pre-existing count, not a fresh one
            post = replace(post, news_url=url, attempts=base_attempts)
            valid, reason = validate_post_text(
                post.text or "",
                max_chars=self._cfg.max_post_chars,
                min_chars=_MIN_POST_CHARS,
            )
            if not valid:
                await self._db.save_attempt(
                    replace(
                        post,
                        status="failed",
                        text=None,
                        attempts=base_attempts + 1,
                        error=reason,
                        rejected_text=(post.text or "")[:_REJECTED_TEXT_MAX_CHARS],
                    )
                )
                failed += 1
                infra_streak = 0
                continue

            written += 1
            outcome, fail_kind = await self._publish_written(post)
            if outcome == "published":
                published += 1
                infra_streak = 0
            elif outcome == "previewed":
                previewed += 1
                infra_streak = 0
            else:
                failed += 1
                if fail_kind == "infra":
                    # network/flood send failures share the writer's circuit
                    # breaker: no attempts penalty, stop burning cycles (P0.3)
                    infra_streak += 1
                    if infra_streak >= self._cfg.circuit_breaker_after:
                        logger.warning(
                            "factory circuit breaker after %s infra errors",
                            infra_streak,
                        )
                        break
                else:
                    infra_streak = 0

        counters = FactoryResult(
            collected=collected,
            filtered=filtered,
            deduped=deduped,
            written=written,
            skipped=skipped,
            published=published,
            previewed=previewed,
            failed=failed,
            recovered=recovered,
        )
        await self._log_run_event(counters, latencies, tokens_total)
        await self._handle_alerting(counters, skip_streak=autopause)
        return counters

    # --- stages ---------------------------------------------------------------

    async def _deduplicate(
        self, aged: list[tuple[str, NewsItem]], now: datetime
    ) -> list[tuple[str, NewsItem, Post | None]]:
        recent_posts = await self._db.get_recent_posts(_RECENT_POSTS_LIMIT)
        recent_by_url = {post.news_url: post for post in recent_posts}
        week_ago = now - timedelta(days=_TITLE_DUP_DAYS)
        # (url, tokens): a post with the SAME canonical url is exempt from the
        # fuzzy check, otherwise a failed attempt would block its own retry
        recent_title_tokens = [
            (post.news_url, set(title_tokens(post.title)))
            for post in recent_posts
            if (post.published_at or post.created_at) is not None
            and (post.published_at or post.created_at) >= week_ago
        ]

        candidates: list[tuple[str, NewsItem, Post | None]] = []
        batch_titles: list[set[str]] = []
        for url, item in aged:
            existing = recent_by_url.get(url)
            if existing is None:
                if await self._db.is_post_written(url):
                    continue
            else:
                if existing.status in _POST_WRITTEN_STATUSES:
                    continue
                if existing.attempts >= self._cfg.max_attempts:
                    continue  # failed with no attempts budget left
            tokens = set(title_tokens(item.title))
            if any(
                other_url != url and jaccard(tokens, other) >= _TITLE_DUP_JACCARD
                for other_url, other in recent_title_tokens
            ):
                continue
            if any(
                jaccard(tokens, other) >= _TITLE_DUP_JACCARD
                for other in batch_titles
            ):
                continue
            batch_titles.append(tokens)
            candidates.append((url, item, existing))
        return candidates

    async def _publish_written(self, post: Post) -> tuple[str, str | None]:
        """Save the draft, send it; returns (published|previewed|failed, kind).

        ``kind`` is set for "failed" only: "content" burns an attempt,
        "infra" (network, flood control) leaves attempts intact (plan P0.3).
        """
        post_id = await self._db.save_attempt(post)
        try:
            result = await self._publisher.publish_post(
                post, to_admins=self._cfg.dry_run
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = PostPublishResult(
                success=False, message_id=None, error=_safe_error(exc)
            )

        if not result.success:
            kind = result.error_kind or "infra"
            attempts = post.attempts + 1 if kind == "content" else post.attempts
            # the generated text is kept for inspection via /posts
            await self._db.save_attempt(
                replace(
                    post,
                    status="failed",
                    attempts=attempts,
                    error=result.error,
                )
            )
            return "failed", kind

        if self._cfg.dry_run:
            await self._db.save_attempt(
                replace(post, id=post_id, status="previewed", message_id=None)
            )
            await self._db.log_event(
                "factory_publish", f"status=previewed dry_run=True id={post_id}"
            )
            return "previewed", None

        await self._db.mark_post_published(post_id, result.message_id)
        await self._db.log_event(
            "factory_publish",
            f"status=published id={post_id} message_id={result.message_id}",
        )
        return "published", None

    async def _recover_post(self, post: Post) -> bool:
        """Re-send a stale draft: text is already in the db, no LLM call."""
        try:
            result = await self._publisher.publish_post(
                post, to_admins=self._cfg.dry_run
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = PostPublishResult(
                success=False, message_id=None, error=_safe_error(exc)
            )

        if not result.success:
            kind = result.error_kind or "infra"
            attempts = post.attempts + 1 if kind == "content" else post.attempts
            await self._db.save_attempt(
                replace(
                    post,
                    status="failed",
                    attempts=attempts,
                    error=result.error,
                )
            )
            return False

        if self._cfg.dry_run:
            await self._db.save_attempt(
                replace(post, status="previewed", message_id=None)
            )
            await self._db.log_event(
                "factory_publish",
                f"status=previewed dry_run=True recovery=True id={post.id}",
            )
            return True

        # save_attempt (not mark_post_published) so that last_attempt_at is
        # bumped and the recovery is visible in count_attempts_today (P0.4)
        await self._db.save_attempt(
            replace(
                post,
                status="published",
                message_id=result.message_id,
                published_at=datetime.now(timezone.utc),
            )
        )
        await self._db.log_event(
            "factory_publish",
            f"status=published recovery=True id={post.id} "
            f"message_id={result.message_id}",
        )
        return True

    async def _break_with_autopause(self, reason: str) -> None:
        await self._db.set_state_flag("factory_autopause", False)
        text = (
            "Content factory auto-paused: LLM config/access error "
            f"({reason}). Fix the config/key and run /factory resume."
        )
        try:
            await self._alerter.send_alert(text)
        except Exception as exc:
            logger.error("factory_config_alert_failed error=%s", exc)
        await self._db.log_event(
            "factory_alert", f"kind=config error={reason}"[:_ERROR_MAX_CHARS]
        )

    # --- events and alerting ----------------------------------------------------

    def _is_active_now(self) -> bool:
        if self._active_window is None:
            return True
        return is_within_active_hours(
            datetime.now(self._tz).time(), self._active_window
        )

    async def _idle_heartbeat(self, reason: str) -> None:
        await self._db.log_event(
            "factory_run", json.dumps({"state": "idle", "reason": reason})
        )
        logger.info("factory idle reason=%s", reason)

    async def _log_run_event(
        self, counters: FactoryResult, latencies: list[int], tokens: int
    ) -> None:
        details: dict[str, Any] = {
            "state": "run",
            "collected": counters.collected,
            "filtered": counters.filtered,
            "deduped": counters.deduped,
            "written": counters.written,
            "skipped": counters.skipped,
            "published": counters.published,
            "previewed": counters.previewed,
            "failed": counters.failed,
            "recovered": counters.recovered,
            "llm_latency_ms": {
                "avg": sum(latencies) // len(latencies),
                "max": max(latencies),
            }
            if latencies
            else None,
            "tokens": tokens,
            "dry_run": self._cfg.dry_run,
        }
        await self._db.log_event(
            "factory_run", json.dumps(details, ensure_ascii=False)
        )
        logger.info("factory_run %s", details)

    async def _handle_alerting(
        self, counters: FactoryResult, *, skip_streak: bool
    ) -> None:
        """Failed-cycle streak alerting: one alert per incident + recovery.

        A failed cycle is written==0 and failed>0 (an all-SKIP cycle is not
        an incident); config/access errors alert immediately (P0.3) and stay
        outside this counter.
        """
        if skip_streak:
            return
        if counters.written == 0 and counters.failed > 0:
            self._failed_streak += 1
            if self._failed_streak < self._cfg.alert_after_failed_cycles:
                return
            if not self._alert_sent and not await self._unresolved_alert_in_db():
                text = (
                    f"Content factory: {self._failed_streak} consecutive failed "
                    f"cycles (last: failed={counters.failed}). "
                    "Check /posts and /factory status."
                )
                try:
                    await self._alerter.send_alert(text)
                except Exception as exc:
                    logger.error("factory_alert_delivery_failed error=%s", exc)
                await self._db.log_event(
                    "factory_alert",
                    f"kind=streak failed_cycles={self._failed_streak}",
                )
            self._alert_sent = True
            self._alert_active = True
            return

        self._failed_streak = 0
        if self._alert_active is None:
            self._alert_active = await self._unresolved_alert_in_db()
        if self._alert_active and (counters.written > 0 or counters.recovered > 0):
            # only a cycle that actually produced a post proves recovery; an
            # empty cycle (all deduped away) just resets the streak
            text = (
                "Content factory recovered: cycle succeeded "
                f"(written={counters.written} "
                f"published={counters.published} previewed={counters.previewed})."
            )
            try:
                await self._alerter.send_recovery(text)
            except Exception as exc:
                logger.error("factory_recovery_delivery_failed error=%s", exc)
            await self._db.log_event("factory_recovery", "streak_alert_closed")
            self._alert_active = False
            self._alert_sent = False

    async def _unresolved_alert_in_db(self) -> bool:
        """True if a factory_alert has no factory_recovery after it.

        Survives restarts: the streak itself may reset, but the incident
        marker does not, so alerts are never duplicated across restarts.
        """
        last_alert = await self._db.get_last_event("factory_alert")
        if last_alert is None:
            return False
        last_recovery = await self._db.get_last_event("factory_recovery")
        if last_recovery is None:
            return True
        return str(last_alert["created_at"]) > str(last_recovery["created_at"])

    @staticmethod
    def _empty_result() -> FactoryResult:
        return FactoryResult(
            collected=0,
            filtered=0,
            deduped=0,
            written=0,
            skipped=0,
            published=0,
            previewed=0,
            failed=0,
            recovered=0,
        )
