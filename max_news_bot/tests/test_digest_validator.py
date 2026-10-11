"""Digest issue validator tests (plan section 5).

The one contract the factory post validator forbids and the digest
requires: a link per item. Numbers and names of an LLM phrase must come
from the source record, and a phrase may only shrink, never grow — the
model shortens, it does not retell.
"""

from __future__ import annotations

from max_news_bot.rubrics.digest_validator import (
    DEFAULT_PHRASE_MAX_CHARS,
    check_phrase_names,
    check_phrase_numbers,
    validate_issue_text,
    validate_item_phrase,
)
from max_news_bot.writers.validator import validate_post_text


SOURCE_RECORD = (
    "Юный художник из Гатчины получил первую премию всероссийского "
    "конкурса рисунков; в конкурсе участвовали 300 работ из 40 регионов."
)

GOOD_PHRASE = "Художник из Гатчины получил премию конкурса из 300 работ."

URL_A = "https://example.com/news/1"
URL_B = "https://example.com/news/2?b=1&a=2"


def issue_text(phrase: str = GOOD_PHRASE, *, url: str = URL_A) -> str:
    return (
        "Хорошие новости дня\n\n"
        f"1. <b>Юный художник из Гатчины получил премию</b>\n"
        f"{phrase}\n"
        f'<a href="{url}">Читать в источнике</a>'
    )


# --- phrase: numbers and names -------------------------------------------------


def test_phrase_with_source_numbers_passes() -> None:
    ok, reason = validate_item_phrase(SOURCE_RECORD, GOOD_PHRASE)

    assert ok, reason
    assert reason is None


def test_phrase_may_drop_numbers() -> None:
    # Shortening: "300 работ из 40 регионов" lost the 40 - that is legal.
    ok, _ = validate_item_phrase(SOURCE_RECORD, "Художник из Гатчины получил премию.")

    assert ok


def test_new_number_is_rejected() -> None:
    ok, reason = validate_item_phrase(
        SOURCE_RECORD, "Художник получил премию конкурса из 700 работ."
    )

    assert not ok
    assert "700" in (reason or "")


def test_new_name_is_rejected() -> None:
    # "Иван" opens the phrase (sentence-initial, excused); "Смирнов" does
    # not and is a fabricated name.
    ok, reason = validate_item_phrase(SOURCE_RECORD, "Иван Смирнов получил премию.")

    assert not ok
    assert "Смирнов" in (reason or "")


def test_sentence_initial_capital_is_not_a_new_name() -> None:
    # "Премию" opens the phrase and never occurs in the record - an
    # ordinary word spelled with a capital, not a fabricated name.
    ok, _ = validate_item_phrase(SOURCE_RECORD, "Премию получил художник из Гатчины.")

    assert ok


def test_source_name_may_disappear() -> None:
    ok, _ = validate_item_phrase(SOURCE_RECORD, "Объявлены итоги конкурса рисунков.")

    assert ok


def test_check_helpers_report_readonly_problems() -> None:
    assert check_phrase_numbers(SOURCE_RECORD, "собрали 500 работ") == (
        "в пункте появилось новое число 500",
    )
    assert "новое имя" in check_phrase_names(SOURCE_RECORD, "Пётр Иванов")[0]


# --- phrase: shortening only ----------------------------------------------------


def test_phrase_longer_than_record_is_rejected() -> None:
    # The record is one short sentence; the phrase is longer than it -
    # the model shortens, never expands.
    ok, reason = validate_item_phrase("Юный художник победил на конкурсе.", GOOD_PHRASE)

    assert not ok
    assert "длиннее исходной записи" in (reason or "")


def test_phrase_over_one_phrase_cap_is_rejected() -> None:
    padded = GOOD_PHRASE + " " + "слово " * 100

    ok, reason = validate_item_phrase(SOURCE_RECORD * 40, padded)

    assert not ok
    assert str(DEFAULT_PHRASE_MAX_CHARS) in (reason or "")


def test_empty_phrase_is_rejected() -> None:
    ok, reason = validate_item_phrase(SOURCE_RECORD, "   ")

    assert not ok
    assert "пустая фраза" in (reason or "")


def test_numbers_check_can_be_disabled_by_config() -> None:
    ok, _ = validate_item_phrase(
        SOURCE_RECORD,
        "Художник получил премию конкурса из 700 работ.",
        check_numbers=False,
    )

    assert ok


# --- issue: the link is required ------------------------------------------------


def test_issue_with_one_link_per_item_passes() -> None:
    text = issue_text()

    ok, reason = validate_issue_text(text, [URL_A])

    assert ok, reason


def test_issue_with_ampersand_url_passes() -> None:
    # The assembly escapes "&" inside the query string; the validator must
    # compare against the unescaped canonical url.
    text = issue_text(url="https://example.com/news/2?b=1&amp;a=2")

    ok, reason = validate_issue_text(text, [URL_B])

    assert ok, reason


def test_issue_without_link_is_rejected() -> None:
    text = issue_text().replace(f'<a href="{URL_A}">Читать в источнике</a>', "")

    ok, reason = validate_issue_text(text, [URL_A])

    assert not ok
    assert "без ссылки" in (reason or "")


def test_issue_with_foreign_link_is_rejected() -> None:
    text = issue_text(url="https://evil.example/click")

    ok, reason = validate_issue_text(text, [URL_A])

    assert not ok
    assert "посторонняя ссылка" in (reason or "")


def test_issue_with_bare_url_outside_anchor_is_rejected() -> None:
    text = issue_text() + "\nПодробнее: www.example.com"

    ok, reason = validate_issue_text(text, [URL_A])

    assert not ok
    assert "вне тега" in (reason or "")


def test_issue_missing_one_link_is_rejected() -> None:
    text = (
        "Хорошие новости дня\n\n"
        "1. <b>Первая</b>\nПервая фраза\n"
        f'<a href="{URL_A}">Читать в источнике</a>\n\n'
        "2. <b>Вторая</b>\nВторая фраза без ссылки"
    )

    ok, reason = validate_issue_text(text, [URL_A, URL_B])

    assert not ok
    assert URL_B in (reason or "")


def test_issue_over_max_chars_is_rejected() -> None:
    text = issue_text(phrase=GOOD_PHRASE + " слово" * 100)

    ok, reason = validate_issue_text(text, [URL_A], max_chars=300)

    assert not ok
    assert "max_chars" in (reason or "")


def test_issue_over_hard_limit_is_rejected() -> None:
    text = issue_text(phrase="слово " * 900)

    ok, reason = validate_issue_text(text, [URL_A])

    assert not ok
    assert "4000" in (reason or "")


# --- the factory link ban does not apply (plan section 5) ------------------------


def test_factory_post_validator_rejects_the_issue_but_digest_does_not() -> None:
    text = issue_text()

    # writers/validator.py forbids links in factory posts (they are
    # code-added there); the digest pipeline deliberately does not carry
    # that ban over - the link per item is required instead.
    factory_ok, factory_reason = validate_post_text(
        text, max_chars=3500, min_chars=200
    )
    digest_ok, digest_reason = validate_issue_text(text, [URL_A], max_chars=3500)

    assert not factory_ok
    assert factory_reason == "contains links"
    assert digest_ok, digest_reason
