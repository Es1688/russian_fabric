from __future__ import annotations

import html
import re

# Telegram counts the rendered message, so tags are not part of the limit.
_HARD_MAX_CHARS = 4096

# Tag-like sequences only: "<b>", "</a>", "<a href=...>"; "< 3" stays text.
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

# Links are added by the code (not the LLM), any link in the model output is
# either a hallucination or a successful injection.
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\s", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\]\(\s*\S", re.IGNORECASE)

# Bare domains ("evil.example.com", "хабр.ру"): Telegram autolinks them, so a
# domain without a scheme is as clickable as a full URL. Curated TLD list
# (incl. Cyrillic zones and common lookalikes) keeps false positives low;
# ambiguous rejects are audited via rejected_text during dry-run.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w.-])"
    r"[a-z0-9а-яё]+(?:\.[a-z0-9а-яё-]+)*"
    r"\.(?:ru|рф|ру|su|com|net|org|io|dev|me|ai|app|info|co|xyz|online|site"
    r"|store|tech|space|news|life|page|link|world|team|tv|gg|fm|cc|li|is|to"
    r"|sh|biz|pro|edu|gov)"
    r"(?![\w-])",
    re.IGNORECASE,
)

# Refusal phrases: the model breaking out of the editor role. Substring
# match, case-insensitive, apostrophes normalized before comparison.
_REFUSAL_PHRASES: tuple[str, ...] = (
    "as an ai",
    "as a language model",
    "ai language model",
    "как языковая модель",
    "как искусственный интеллект",
    "i cannot",
    "i can't",
    "i couldn't",
    "я не могу",
    "не могу выполнить",
    "не могу помочь",
    "я всего лишь ии",
)


def validate_post_text(
    text: str, *, max_chars: int, min_chars: int
) -> tuple[bool, str | None]:
    """Validate LLM output before publishing.

    Checks (visible text, tags excluded):
    - no links (URL, bare domain, ``<a href``, markdown link) — links are
      code-added;
    - no refusal phrases ("As an AI…");
    - hard limit 4096 chars (Telegram), soft limit ``max_chars``,
      minimum ``min_chars``.
    """
    if (
        _URL_RE.search(text)
        or _ANCHOR_RE.search(text)
        or _MARKDOWN_LINK_RE.search(text)
        or _BARE_DOMAIN_RE.search(text)
    ):
        return False, "contains links"

    refusal = find_refusal_phrase(text)
    if refusal is not None:
        return False, f"refusal phrase: {refusal!r}"

    visible_len = len(visible_text(text))
    if visible_len > _HARD_MAX_CHARS:
        return False, f"too long: {visible_len} > {_HARD_MAX_CHARS} (hard limit)"
    if visible_len > max_chars:
        return False, f"too long: {visible_len} > {max_chars}"
    if visible_len < min_chars:
        return False, f"too short: {visible_len} < {min_chars}"
    return True, None


def visible_text(text: str) -> str:
    """User-visible text: tags stripped, entities decoded, edges trimmed."""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def find_refusal_phrase(text: str) -> str | None:
    """Return the first refusal phrase found in the text, or None."""
    normalized = text.replace("\u2019", "'").replace("\u2018", "'").lower()
    for phrase in _REFUSAL_PHRASES:
        if phrase in normalized:
            return phrase
    return None
