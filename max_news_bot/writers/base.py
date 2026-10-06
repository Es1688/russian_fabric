from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from max_news_bot.core.models import NewsItem, Post


@dataclass(frozen=True)
class WriteResult:
    status: str  # "written" | "skipped" | "error"
    post: Post | None
    error_kind: str | None  # "content" | "infra" | "config"
    error: str | None  # truncated to ~300 chars, no headers/keys
    tokens: int = 0
    latency_ms: int = 0


class Writer(Protocol):
    async def write_post(self, item: NewsItem) -> WriteResult: ...
