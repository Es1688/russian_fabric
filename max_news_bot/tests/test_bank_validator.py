"""Bank record formatting validator tests (plan section 6).

The LLM only re-words a record: numbers (years included) and names must
survive unchanged, and nothing new may appear.
"""

from __future__ import annotations

from max_news_bot.bank.validator import (
    check_names,
    check_numbers,
    extract_names,
    extract_numbers,
    validate_bank_formatting,
)

# A realistic "this day" fact with a year and two names.
RECORD = (
    "27 мая 1703 года Пётр I заложил Петропавловскую крепость на Заячьем "
    "острове — так начался Санкт-Петербург."
)

# Same facts, re-worded: numbers and names intact, nothing added.
GOOD_OUTPUT = (
    "Этот день в истории. 27 мая 1703 года Пётр I заложил Петропавловскую "
    "крепость на Заячьем острове, и с этого начался Санкт-Петербург."
)


def test_extract_numbers_takes_digit_runs() -> None:
    # Hyphens and punctuation split runs: "1703-й" contributes "1703".
    assert extract_numbers(RECORD) == {"27", "1703"}
    assert extract_numbers("с 1900-х по 1917 год, 2 раза") == {
        "1900",
        "1917",
        "2",
    }


def test_extract_names_ignores_ordinary_capitalized_words() -> None:
    # "Пётр" never appears lowercase -> a name; "мая" is lowercase prose.
    names = extract_names(RECORD)
    assert "Пётр" in names
    assert "Петропавловскую" in names
    assert "Заячьем" in names
    # the hyphen splits the city name into two letter runs, both names
    assert "Санкт" in names and "Петербург" in names

    # an ordinary word that also occurs lowercase is never a name
    assert extract_names("Мама мыла раму, и мама устала.") == set()


def test_check_numbers_passes_when_unchanged() -> None:
    assert check_numbers(RECORD, GOOD_OUTPUT) == ()


def test_check_numbers_flags_missing_number() -> None:
    output = GOOD_OUTPUT.replace("1703", "в начале XVIII века")
    problems = check_numbers(RECORD, output)
    assert any("1703" in problem and "пропало" in problem for problem in problems)


def test_check_numbers_flags_new_number() -> None:
    output = GOOD_OUTPUT + " Сегодня этому событию 323 года."
    problems = check_numbers(RECORD, output)
    assert any("323" in problem and "новое число" in problem for problem in problems)


def test_check_numbers_flags_changed_year() -> None:
    output = GOOD_OUTPUT.replace("1703", "1704")
    problems = check_numbers(RECORD, output)
    assert any("1703" in problem for problem in problems)
    assert any("1704" in problem for problem in problems)


def test_check_names_flags_missing_name() -> None:
    output = GOOD_OUTPUT.replace("Пётр I ", "царь ")
    problems = check_names(RECORD, output)
    assert any("Пётр" in problem and "пропало" in problem for problem in problems)


def test_check_names_flags_new_name_mid_sentence() -> None:
    output = GOOD_OUTPUT.replace("крепость", "крепость вместе с Меншиковым")
    problems = check_names(RECORD, output)
    assert any("Меншиковым" in problem and "новое имя" in problem for problem in problems)


def test_check_names_allows_sentence_initial_ordinary_word() -> None:
    # "Спокойной" opens the post and never occurs in the record at all,
    # but it is not a name in a mid-sentence position.
    output = "Спокойной ночи. " + GOOD_OUTPUT
    problems = check_names(RECORD, output)
    assert not any("Спокойной" in problem for problem in problems)


def test_validate_bank_formatting_ok() -> None:
    ok, reason = validate_bank_formatting(RECORD, GOOD_OUTPUT)
    assert ok is True
    assert reason is None


def test_validate_bank_formatting_reports_combined_reason() -> None:
    output = GOOD_OUTPUT.replace("1703", "1913") + " Добавил Иванов."
    ok, reason = validate_bank_formatting(RECORD, output)
    assert ok is False
    assert reason is not None
    assert "1703" in reason  # lost number
    assert "1913" in reason  # new number
    assert "Иванов" in reason  # new name
