"""Anti-repeat machinery for bank-driven rubrics (plan section 3).

Two layers, exactly as the plan prescribes:

- the last 5-7 published posts of the rubric go into the formatting
  prompt (``recent_posts_section``), so the model sees what already went
  out;
- a hard Jaccard duplicate check (``filter_dissimilar``) reuses the
  legacy title-dedup of ``utils/text.py`` (``title_tokens`` +
  ``jaccard``) with the rubric's own ``similarity_threshold``; a bank
  record similar to a recent post is never selected.

Both operate on full post texts: bank records have no separate headline
field, the whole record is the dedup key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Sequence

from max_news_bot.core.models import ContentBankItem
from max_news_bot.utils.text import jaccard, title_tokens

# Never-shown sentinel: sorts before any real last_shown_at (tz-aware).
_NEVER_SHOWN = datetime.min.replace(tzinfo=timezone.utc)


def is_similar_to_recent(
    text: str, recent_texts: Sequence[str], threshold: float
) -> bool:
    """True when ``text`` is a near-duplicate of any recent post."""
    tokens = set(title_tokens(text))
    return any(
        jaccard(tokens, set(title_tokens(other))) >= threshold
        for other in recent_texts
    )


def filter_dissimilar(
    items: Iterable[ContentBankItem],
    recent_texts: Sequence[str],
    threshold: float,
) -> list[ContentBankItem]:
    """Drop bank records that duplicate the rubric's recent posts."""
    return [
        item
        for item in items
        if not is_similar_to_recent(item.text, recent_texts, threshold)
    ]


def least_recently_shown_first(
    items: Iterable[ContentBankItem],
) -> list[ContentBankItem]:
    """Fair rotation order: fewer shows first, then longer ago, then id.

    The stable sort keeps the bank-type order of the selection for equal
    keys, so the candidate list stays deterministic.
    """
    return sorted(
        items,
        key=lambda item: (
            item.show_count,
            item.last_shown_at or _NEVER_SHOWN,
            item.id or 0,
        ),
    )


def recent_posts_section(recent_texts: Sequence[str]) -> str:
    """Prompt block with the rubric's recent posts (empty when no history)."""
    if not recent_texts:
        return ""
    lines = ["Недавние посты рубрики (не повторяй их темы и формулировки):"]
    lines.extend(
        f"{number}. {text}" for number, text in enumerate(recent_texts, start=1)
    )
    return "\n".join(lines)
