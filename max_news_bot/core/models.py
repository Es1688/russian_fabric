from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    category: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class Post:
    id: int | None
    news_url: str  # canonical URL (UNIQUE)
    source: str
    title: str
    text: str | None  # nullable: error rows have no text
    status: str  # draft|published|failed|skipped|previewed
    attempts: int = 0  # content failures only
    error: str | None = None  # truncated ~300, no headers/keys
    prompt_version: str | None = None  # sha1(prompt)[:8]
    llm_model: str | None = None
    tokens: int | None = None
    message_id: int | None = None  # channel sends; NULL in dry-run
    rejected_text: str | None = None  # validator rejection, truncated
    created_at: datetime | None = None
    published_at: datetime | None = None
    last_attempt_at: datetime | None = None


@dataclass(frozen=True)
class PublishedResult:
    success: bool
    published: int
    failed: int = 0
