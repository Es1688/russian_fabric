"""Bank record formatting validator (plan section 6).

The LLM never invents bank content - it only re-words a reviewed record
(plan section 6: "LLM не выдумывает, только оформляет запись"). This
validator enforces exactly that on the model output:

- numbers, years included, must survive unchanged: every digit run of the
  record appears in the post and the post adds none;
- proper names must survive and no new ones may appear: a capitalized
  word that never occurs lowercase in the record is treated as a name
  (Пётр, Петербург), and any capitalized word in the post that is
  neither a record name nor an ordinary record word - outside a
  sentence-initial position - is a fabrication.

Follows the writers/validator.py contract: ``(ok, reason)`` with a
readable cause, so the bank pipeline can reject the attempt and let the
slot scheduler retry with the next candidate (plan section 7).
"""

from __future__ import annotations

import re

# Digit runs: "1905", "7", "05" — years are digit runs too.
_NUMBER_RE = re.compile(r"\d+")
# Unicode letter runs (no digits, no underscore, no punctuation).
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# A capitalized word directly after these characters opens a sentence or
# a list item, so it may be an ordinary word spelled with a capital.
_SENTENCE_ENDINGS = frozenset(".!?…:;\n")
# Skipped between a sentence ending and the word: spaces, quotes, dashes.
_OPENERS = frozenset(" \t\"'«“([<")


def extract_numbers(text: str) -> set[str]:
    """Every digit run of the text as a set of strings."""
    return set(_NUMBER_RE.findall(text))


def extract_names(text: str) -> set[str]:
    """Capitalized words that never appear lowercase in the text.

    A word that also occurs lowercase somewhere in the record is an
    ordinary word capitalized by a sentence start; one that never does is
    a proper name. Single letters are punctuation-adjacent noise.
    """
    words = _WORD_RE.findall(text)
    ordinary = {word.lower() for word in words if not word[:1].isupper()}
    return {
        word
        for word in words
        if len(word) > 1 and word[:1].isupper() and word.lower() not in ordinary
    }


def _is_sentence_initial(text: str, position: int) -> bool:
    """True when the word at ``position`` opens a sentence or the text."""
    index = position - 1
    while index >= 0 and text[index] in _OPENERS:
        index -= 1
    return index < 0 or text[index] in _SENTENCE_ENDINGS


def check_numbers(source_text: str, output_text: str) -> tuple[str, ...]:
    """Numbers and years must match both ways: nothing lost, nothing new."""
    problems: list[str] = []
    source = extract_numbers(source_text)
    output = extract_numbers(output_text)
    for number in sorted(source - output, key=int):
        problems.append(f"число {number} из записи пропало в посте")
    for number in sorted(output - source, key=int):
        problems.append(f"в посте появилось новое число {number}")
    return tuple(problems)


def check_names(source_text: str, output_text: str) -> tuple[str, ...]:
    """Names of the record must survive; the post may add no new ones."""
    problems: list[str] = []
    source_names = extract_names(source_text)
    output_words = set(_WORD_RE.findall(output_text))
    ordinary = {word.lower() for word in _WORD_RE.findall(source_text)}

    for name in sorted(source_names):
        if name not in output_words:
            problems.append(f"имя {name!r} из записи пропало в посте")

    for match in _WORD_RE.finditer(output_text):
        word = match.group()
        if len(word) <= 1 or not word[:1].isupper():
            continue
        if word in source_names or word.lower() in ordinary:
            continue
        if _is_sentence_initial(output_text, match.start()):
            continue  # formatting may capitalize an ordinary opening word
        problems.append(f"в посте появилось новое имя {word!r}")
    return tuple(problems)


def validate_bank_formatting(
    source_text: str, output_text: str
) -> tuple[bool, str | None]:
    """Full bank formatting check: numbers/years and names unchanged."""
    problems = (*check_numbers(source_text, output_text), *check_names(source_text, output_text))
    if problems:
        return False, "; ".join(problems)
    return True, None
