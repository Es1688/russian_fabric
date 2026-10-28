from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timezone
import html
import re
from typing import Any

import aiohttp
import feedparser

from max_news_bot.parsers.base import SourceConfig
from max_news_bot.core.models import NewsItem
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

_SUMMARY_MAX_CHARS = 1000
_TAG_RE = re.compile(r"<[^>]+>")


class RssFetcher:
    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        # Shared session (process-wide) wins; otherwise a short-lived one is
        # created per fetch call as before.
        self._session = session

    async def fetch(
        self, source: SourceConfig, max_news: int, timeout: int
    ) -> list[NewsItem]:
        if source.type != "rss" or not source.enabled:
            return []

        try:
            if self._session is not None:
                async with self._session.get(
                    source.url, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    response.raise_for_status()
                    payload = await response.read()
            else:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.get(source.url) as response:
                        response.raise_for_status()
                        payload = await response.read()
        except Exception as exc:
            logger.error(
                "source=%s error=%s",
                source.name,
                exc,
            )
            return []

        parsed = await asyncio.to_thread(feedparser.parse, payload)
        items = []
        for entry in parsed.entries[:max_news]:
            items.append(_entry_to_item(entry, source))
        return items


def _entry_to_item(entry: dict[str, Any], source: SourceConfig) -> NewsItem:
    published_at = _parse_entry_date(entry)
    title = str(entry.get("title", "")).strip()
    link = str(entry.get("link", "")).strip()
    return NewsItem(
        title=title,
        url=link,
        source=source.name,
        published_at=published_at,
        category=source.category,
        summary=_extract_summary(entry),
    )


def _extract_summary(entry: dict[str, Any]) -> str | None:
    raw = entry.get("summary") or entry.get("description") or ""
    text = html.unescape(_TAG_RE.sub(" ", str(raw)))
    text = " ".join(text.split())
    if not text:
        return None
    return text[:_SUMMARY_MAX_CHARS]


def _parse_entry_date(entry: dict[str, Any]) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            # feedparser structs are UTC; timegm (not mktime) keeps them UTC
            # regardless of the host timezone — the factory age filter
            # depends on this
            timestamp = calendar.timegm(value)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return None
