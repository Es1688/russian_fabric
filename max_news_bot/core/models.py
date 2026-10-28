from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    category: str | None = None
    summary: str | None = None


# --- autonomous channel (plan §6, §9) ---

SLOT_RUN_STATUSES = (
    "pending",
    "generated",
    "publishing",
    "published",
    "skipped",
    "failed",
    "unknown_delivery",
)


@dataclass(frozen=True)
class SlotRun:
    """One rubric slot in one day (UNIQUE(rubric_id, slot_date), plan §9)."""

    id: int | None
    rubric_id: str
    slot_date: str  # 'YYYY-MM-DD' in the config timezone, not UTC
    status: str  # one of SLOT_RUN_STATUSES
    skip_reason: str | None = None
    error: str | None = None  # truncated ~300, no headers/keys
    attempts: int = 0
    target: str | None = None  # chat/channel id the slot was aimed at
    is_dry_run: bool = False
    text: str | None = None  # generated text kept for recovery without LLM
    image_ref: str | None = None
    source_refs: tuple[str, ...] = ()  # JSON array in the database
    max_message_id: str | None = None  # channel mid ("mid.<id>")
    created_at: datetime | None = None
    updated_at: datetime | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class DigestItemRef:
    """A news item included in a digest issue (slot run)."""

    id: int | None
    slot_run_id: int
    url: str
    source: str
    title: str
    position: int = 0
    created_at: datetime | None = None


@dataclass(frozen=True)
class RubricState:
    """Pause flag and target override surviving a restart (plan §9)."""

    rubric_id: str
    paused: bool = False
    target: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ContentBankItem:
    """A bank entry: fact, thought, postcard, recipe, advice, backup post."""

    id: int | None
    item_type: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)  # JSON in the db
    review_status: str = "pending"  # pending|approved|rejected
    show_count: int = 0
    last_shown_at: datetime | None = None
    months: tuple[int, ...] = ()  # 1..12 seasonality; empty = any month
    event_date: str | None = None  # 'MM-DD' for "this day" facts
    created_at: datetime | None = None


@dataclass(frozen=True)
class FilterLogEntry:
    """One filter-chain decision: classifier/checker answers, rejections."""

    id: int | None
    stage: str  # stoplist|topic|llm_classifier|llm_classifier_confirm|
    # llm_checker|freshness|dedup
    decision: str  # rejected|passed|unreadable|error or a classifier label
    url: str | None = None
    title: str | None = None
    reason: str | None = None
    model: str | None = None
    response: str | None = None  # raw LLM answer (transparency mode)
    created_at: datetime | None = None
