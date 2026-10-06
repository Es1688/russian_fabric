from __future__ import annotations

import pytest

from max_news_bot.utils.sanitize import sanitize_html


def test_plain_text_is_escaped_and_kept() -> None:
    assert sanitize_html("R&D и 5 < 10 > 3") == "R&amp;D и 5 &lt; 10 &gt; 3"


def test_empty_string_stays_empty() -> None:
    assert sanitize_html("") == ""


def test_whitelisted_tags_survive() -> None:
    text = "<b>жирный</b> и <i>курсив</i>"
    assert sanitize_html(text) == text


def test_nested_whitelisted_tags_survive() -> None:
    text = "<b>жирный <i>жирный курсив</i></b>"
    assert sanitize_html(text) == text


def test_safe_anchor_survives() -> None:
    text = '<a href="https://example.com/page">читать</a>'
    assert sanitize_html(text) == text


def test_anchor_href_is_escaped() -> None:
    result = sanitize_html('<a href="https://example.com/?a=1&amp;b=2">x</a>')
    assert result.startswith('<a href="https://example.com')
    assert "<a href=" in result and "</a>" in result


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
    ],
)
def test_dangerous_href_tag_is_removed_content_kept(href: str) -> None:
    result = sanitize_html(f'до <a href="{href}">клик</a> после')
    assert result == "до клик после"


def test_href_less_anchor_pair_is_removed_content_kept() -> None:
    assert sanitize_html("<a>текст</a>") == "текст"


def test_unknown_tags_are_dropped_content_kept() -> None:
    assert sanitize_html("<script>alert(1)</script>текст") == "alert(1)текст"
    assert sanitize_html("<u>слово</u>") == "слово"


def test_ampersand_in_text_is_escaped() -> None:
    assert sanitize_html("A & B <b>c</b>") == "A &amp; B <b>c</b>"


def test_already_escaped_entities_stay_single_escaped() -> None:
    assert sanitize_html("A &amp; B") == "A &amp; B"


def test_stray_lt_is_escaped_as_text() -> None:
    assert sanitize_html("если x < 3, то") == "если x &lt; 3, то"


def test_unclosed_tag_triggers_full_fallback() -> None:
    result = sanitize_html("<b>жирный текст без закрытия & <i>курсив")
    assert result == "жирный текст без закрытия &amp; курсив"
    assert "<" not in result and ">" not in result


def test_close_without_open_triggers_full_fallback() -> None:
    result = sanitize_html("текст</b>продолжение")
    assert result == "текстпродолжение"


def test_misnested_tags_trigger_full_fallback() -> None:
    result = sanitize_html("<b><i>перекрёстно</b></i>")
    assert result == "перекрёстно"


def test_nested_anchors_trigger_full_fallback() -> None:
    result = sanitize_html(
        '<a href="https://a.example">x<a href="https://b.example">y</a></a>'
    )
    assert result == "xy"


def test_fallback_strips_all_tags_and_escapes_everything() -> None:
    result = sanitize_html("<b>жирный & <u>вложенный</u> unclosed")
    assert result == "жирный &amp; вложенный unclosed"
    assert "<" not in result and ">" not in result


def test_uppercase_tags_are_normalized() -> None:
    assert sanitize_html("<B>жирный</B>") == "<b>жирный</b>"


def test_paired_broken_markup_inside_valid_markup_falls_back_everywhere() -> None:
    # one broken tag poisons the whole message, not just its fragment
    result = sanitize_html("ок <b>жирный</b> ок <i>битый")
    assert result == "ок жирный ок битый"
