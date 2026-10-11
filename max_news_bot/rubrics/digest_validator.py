"""Digest issue validator (plan section 5).

The digest has its own validation contract, deliberately different from
the factory post validator (``writers/validator.py``): a digest item
*must* carry a link to its source — "запрет ссылок из валидатора постов
фабрики на дайджест не переносится, ссылка в пункте обязательна". The
LLM only shortens: every number and every proper name of an item phrase
must come from the source record, and a phrase may never grow beyond
the record or the one-phrase length cap, so full foreign texts are
never reproduced.

Follows the project validator contract: ``(ok, reason)`` with a readable
cause, so the digest pipeline can drop the bad item and let the issue
degrade (plan section 5) instead of publishing an unverified shortening.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Sequence

from max_news_bot.bank.validator import (
    _WORD_RE,
    _is_sentence_initial,
    extract_names,
    extract_numbers,
)
from max_news_bot.writers.validator import visible_text

# MAX message hard limit, same value as config/rubrics.py MAX_POST_CHARS.
HARD_MAX_CHARS = 4000

# One phrase per item (plan section 5): a longer text reads as a retold
# article, not a shortening.
DEFAULT_PHRASE_MAX_CHARS = 220

# href value of an <a> tag; issue links are code-built, never model-built.
_HREF_RE = re.compile(r"<a\s[^>]*?href=\"([^\"]+)\"[^>]*>", re.IGNORECASE)
_ANCHOR_TAG_RE = re.compile(r"</?a\b[^>]*>", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def check_phrase_numbers(source_text: str, phrase: str) -> tuple[str, ...]:
    """A shortening may drop numbers but must never add one."""
    invented = extract_numbers(phrase) - extract_numbers(source_text)
    return tuple(
        f"в пункте появилось новое число {number}"
        for number in sorted(invented, key=int)
    )


def check_phrase_names(source_text: str, phrase: str) -> tuple[str, ...]:
    """A shortening may drop names but must never add one.

    Same reading of a "name" as the bank validator: a capitalized word
    that never occurs lowercase in the source record; an ordinary word
    opening the phrase may stay capitalized.
    """
    source_names = extract_names(source_text)
    ordinary = {word.lower() for word in _WORD_RE.findall(source_text)}
    problems: list[str] = []
    for match in _WORD_RE.finditer(phrase):
        word = match.group()
        if len(word) <= 1 or not word[:1].isupper():
            continue
        if word in source_names or word.lower() in ordinary:
            continue
        if _is_sentence_initial(phrase, match.start()):
            continue  # formatting may capitalize an ordinary opening word
        problems.append(f"в пункте появилось новое имя {word!r}")
    return tuple(problems)


def validate_item_phrase(
    source_text: str,
    phrase: str,
    *,
    phrase_max_chars: int = DEFAULT_PHRASE_MAX_CHARS,
    check_numbers: bool = True,
    check_names: bool = True,
) -> tuple[bool, str | None]:
    """One LLM-shortened phrase against its source record.

    Checks: the phrase is non-empty; its numbers and names all come from
    the record; it is not longer than the record (the model only
    shortens) and fits the one-phrase cap.
    """
    cleaned = phrase.strip()
    if not cleaned:
        return False, "пустая фраза пункта"
    problems: tuple[str, ...] = ()
    if check_numbers:
        problems += check_phrase_numbers(source_text, cleaned)
    if check_names:
        problems += check_phrase_names(source_text, cleaned)
    if len(visible_text(cleaned)) > len(visible_text(source_text)):
        problems += ("фраза длиннее исходной записи (LLM только сокращает)",)
    if len(visible_text(cleaned)) > phrase_max_chars:
        problems += (f"фраза длиннее {phrase_max_chars} символов (не одна фраза)",)
    if problems:
        return False, "; ".join(problems)
    return True, None


def issue_link_problems(text: str, expected_urls: Sequence[str]) -> tuple[str, ...]:
    """Every item must link exactly its source url; no other links allowed.

    ``expected_urls`` are the canonical urls of the issue items; the href
    values are entity-decoded before the comparison, because the assembly
    escapes "&" inside query strings.
    """
    problems: list[str] = []
    found = [unescape(url) for url in _HREF_RE.findall(text)]
    missing = sorted(set(expected_urls) - set(found))
    if missing:
        problems.append(f"пункт без ссылки на источник: {missing[:3]}")
    extra = sorted(set(found) - set(expected_urls))
    if extra:
        problems.append(f"посторонняя ссылка в выпуске: {extra[:3]}")
    if sorted(found) != sorted(expected_urls):
        problems.append("набор ссылок выпуска не совпадает с пунктами")
    stripped = _ANCHOR_TAG_RE.sub("", text)
    if _BARE_URL_RE.search(stripped):
        problems.append("ссылка вне тега <a> в выпуске")
    return tuple(problems)


def validate_issue_text(
    text: str,
    expected_urls: Sequence[str],
    *,
    max_chars: int | None = None,
    check_links: bool = True,
) -> tuple[bool, str | None]:
    """The assembled issue: links match the items, length fits the limits.

    ``max_chars`` is the rubric's soft limit; the MAX hard limit of 4000
    always applies. Links are the one check the factory post validator
    forbids and the digest requires (plan section 5).
    """
    problems: tuple[str, ...] = ()
    if check_links:
        problems += issue_link_problems(text, expected_urls)
    visible_len = len(visible_text(text))
    if visible_len > HARD_MAX_CHARS:
        problems += (f"выпуск {visible_len} > {HARD_MAX_CHARS} (предел MAX)",)
    if max_chars is not None and visible_len > max_chars:
        problems += (f"выпуск {visible_len} > {max_chars} (max_chars рубрики)",)
    if problems:
        return False, "; ".join(problems)
    return True, None
