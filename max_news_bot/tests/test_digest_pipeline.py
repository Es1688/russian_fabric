"""Good-news digest pipeline tests (plan section 5).

The LLM is a scripting double that records every call, so each test also
asserts the call budget shape: two classifier passes per accepted item
plus exactly one shortening call for the whole issue - and zero calls
when the stop-list rejects everything locally.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from max_news_bot.bank.pipeline import BankPipeline
from max_news_bot.bot.slot_scheduler import SourceStat
from max_news_bot.config.news_sources import NewsSource
from max_news_bot.config.rubrics import (
    RubricConfig,
    RubricsConfig,
    TargetConfig,
    WEEKDAYS,
)
from max_news_bot.tests.conftest import make_llm_channels
from max_news_bot.core.models import NewsItem
from max_news_bot.rubrics.digest import (
    ISSUE_TITLE,
    LINK_LABEL,
    MAX_ITEMS,
    SHORTEN_SYSTEM_PROMPT,
    DigestEntry,
    DigestPipeline,
    build_shorten_user_prompt,
    format_issue,
    parse_shorten_answer,
)
from max_news_bot.tests.mocks import MockFetcher
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    DigestItemsRepository,
    FilterLogRepository,
    SlotRunRepository,
)
from max_news_bot.writers.completion import CompletionResult, LlmCompletionError


UTC = timezone.utc
# 19:00 MSK is 16:00 UTC on the same October day.
NOW = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)
PUBLISHED = NOW - timedelta(hours=10)
SLOT_DATE = "2026-10-10"

PERSONA = "Ты — редактор канала для читателей старшего возраста."

FACT_OF_THE_DAY = (
    "10 октября 1879 года в Петербурге впервые зажглись электрические "
    "фонари на Невском проспекте. Горожане выходили смотреть на них целыми "
    "семьями, а газеты называли новое освещение чудом техники."
)

ITEM_1 = NewsItem(
    title="Юный художник из Гатчины победил на конкурсе рисунков",
    url="https://example.com/news/1?utm_source=rss",
    source="Культура",
    published_at=PUBLISHED,
    category="culture",
    summary=(
        "В всероссийском конкурсе участвовали 300 работ из 40 регионов; "
        "жюри отметило работу школьника из Гатчины."
    ),
)
ITEM_2 = NewsItem(
    title="В Петербурге открылась выставка старинных открыток",
    url="https://example.com/news/2",
    source="Культура",
    published_at=PUBLISHED - timedelta(minutes=30),
    category="culture",
    summary="Экспозиция музея собрала 120 открыток начала XX века.",
)
ITEM_3 = NewsItem(
    title="Российские школьники выиграли олимпиаду по астрономии",
    url="https://example.com/news/3",
    source="Наука",
    published_at=PUBLISHED - timedelta(hours=2),
    category="science",
    summary="Команда из пяти школьников обошла 20 команд на турнире в Казани.",
)

PHRASE_1 = "Школьник из Гатчины получил премию конкурса из 300 работ."
PHRASE_2 = "Выставка собрала 120 старинных открыток."
PHRASE_3 = "Школьники обошли 20 команд в Казани."

FIRE_ITEM = NewsItem(
    title="Пожар на складе быстро потушили",
    url="https://example.com/news/fire",
    source="Культура",
    published_at=PUBLISHED,
    category=None,
    summary=None,
)

FEED_CULTURE = NewsSource(
    name="Культура", url="https://example.com/culture.xml", section="culture"
)
FEED_SCIENCE = NewsSource(
    name="Наука", url="https://example.com/science.xml", section="science"
)


class ScriptedLlm:
    """LlmCompletion double: scripted items played in order (the last one
    repeats), every call recorded. A str becomes a CompletionResult, a
    BaseException is raised."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self._script = list(script or [])
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> CompletionResult:
        self.calls.append((system, user))
        if not self._script:
            return CompletionResult(content="позитивная")
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, CompletionResult):
            return item
        return CompletionResult(content=str(item), model="test-model")

    @property
    def call_count(self) -> int:
        return len(self.calls)


def shorten_answer(phrases: dict[int, str]) -> str:
    return "\n".join(
        f'<item id="{number}">{phrase}</item>' for number, phrase in phrases.items()
    )


def make_rubric(
    *,
    validators: tuple[str, ...] = ("length", "links_required", "numbers_match"),
    min_chars: int = 1,
    max_chars: int = 4000,
) -> RubricConfig:
    return RubricConfig(
        id="good_news",
        enabled=True,
        time="19:00",
        days=tuple(WEEKDAYS),
        grace_minutes=20,
        jitter_minutes=0,
        kind="digest",
        target="main",
        source={"feeds_file": "news_sources.yaml"},
        topics_by_weekday={},
        prompt_overlay=None,
        min_chars=min_chars,
        max_chars=max_chars,
        image=None,
        history_n=5,
        similarity_threshold=0.7,
        validators=validators,
        on_llm_failure="bank",
    )


def make_config(
    rubrics: list[RubricConfig], *, persona: str = PERSONA
) -> RubricsConfig:
    return RubricsConfig(
        llm=make_llm_channels(),
        timezone="Europe/Moscow",
        persona=persona,
        image_style="",
        targets={"main": TargetConfig(name="main", channel_id=-1001234567890)},
        daily_cap=6,
        weather_points=(),
        rubrics=tuple(rubrics),
    )


def make_pipeline(
    path: Path,
    llm: ScriptedLlm,
    *,
    rubric: RubricConfig | None = None,
    feeds: tuple[NewsSource, ...] = (FEED_CULTURE,),
    items_by_feed: dict[str, list[NewsItem]] | None = None,
    is_excluded_source: Any = None,
) -> DigestPipeline:
    rubric = rubric or make_rubric()
    config = make_config([rubric])
    fetcher = MockFetcher(items_by_source=items_by_feed or {})
    return DigestPipeline(
        config,
        feeds,
        fetcher,
        llm,
        FilterLogRepository(path),
        DigestItemsRepository(path),
        BankPipeline(config, ContentBankRepository(path), SlotRunRepository(path)),
        is_excluded_source=is_excluded_source,
    )


async def claim_run(path: Path, rubric_id: str = "good_news", slot_date: str = SLOT_DATE):
    run = await SlotRunRepository(path).try_claim(rubric_id, slot_date)
    assert run is not None
    return run


async def add_fact(path: Path) -> int:
    from max_news_bot.core.models import ContentBankItem

    return await ContentBankRepository(path).add(
        ContentBankItem(
            id=None,
            item_type="fact",
            text=FACT_OF_THE_DAY,
            metadata={},
            review_status="approved",
        )
    )


def three_good_feeds() -> dict[str, list[NewsItem]]:
    return {"Культура": [ITEM_1, ITEM_2], "Наука": [ITEM_3]}


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "digest.db"
    await Database(path).initialize()
    return path


# --- pure helpers ---------------------------------------------------------------


def test_parse_shorten_answer_maps_ids() -> None:
    content = shorten_answer({1: PHRASE_1, 2: PHRASE_2, 3: PHRASE_3})

    assert parse_shorten_answer(content) == {1: PHRASE_1, 2: PHRASE_2, 3: PHRASE_3}


def test_parse_shorten_answer_first_answer_wins_and_garbage_dropped() -> None:
    content = (
        f'<item id="1">{PHRASE_1}</item>\n'
        f'<item id="1">другая фраза</item>\n'
        "какой-то пояснительный текст без тегов\n"
        f"<item id=\"3\">{PHRASE_3}</item>"
    )

    assert parse_shorten_answer(content) == {1: PHRASE_1, 3: PHRASE_3}
    assert parse_shorten_answer("нет тегов совсем") == {}


def test_build_shorten_user_prompt_numbers_the_records() -> None:
    prompt = build_shorten_user_prompt([ITEM_1, ITEM_2, ITEM_3])

    assert '<article id="1">' in prompt
    assert '<article id="3">' in prompt
    assert ITEM_1.title in prompt
    assert "300 работ" in prompt  # the summary is the shortening material
    assert '<item id="N">фраза</item>' in prompt


def test_format_issue_builds_headline_phrase_link_blocks() -> None:
    text = format_issue(
        [
            DigestEntry(item=ITEM_1, phrase=PHRASE_1),
            DigestEntry(item=ITEM_2, phrase=PHRASE_2),
        ]
    )

    assert text.startswith(ISSUE_TITLE)
    assert text.count(LINK_LABEL) == 2
    assert "1. <b>Юный художник из Гатчины победил на конкурсе рисунков</b>" in text
    assert PHRASE_1 in text and PHRASE_2 in text
    # xml-hostile characters of a headline survive the html assembly
    risky = NewsItem(
        title="Выставка «Кошки & собаки»",
        url="https://example.com/news/6?a=1&b=2",
        source="Культура",
    )
    text = format_issue([DigestEntry(item=risky, phrase="Фраза.")])
    assert "«Кошки &amp; собаки»" in text
    assert 'href="https://example.com/news/6?a=1&amp;b=2"' in text


# --- generate: the full issue ------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_full_issue(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 6 + [shorten_answer({1: PHRASE_1, 2: PHRASE_2, 3: PHRASE_3})]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.text is not None
    # one shortening call for the whole issue (the LLM only shortens)
    system, _user = llm.calls[6]
    assert system == SHORTEN_SYSTEM_PROMPT
    assert llm.call_count == 7

    text = outcome.text
    assert text.startswith(ISSUE_TITLE)
    assert text.count(LINK_LABEL) == 3
    # canonical urls (the utm_ parameter of item 1 is dropped by the code)
    assert '<a href="https://example.com/news/1">' in text
    assert '<a href="https://example.com/news/2">' in text
    assert '<a href="https://example.com/news/3">' in text
    # headline, one phrase, numbered
    assert "<b>Юный художник из Гатчины победил на конкурсе рисунков</b>" in text
    assert PHRASE_1 in text and PHRASE_2 in text and PHRASE_3 in text
    assert "\n1. <b>" in text and "\n2. <b>" in text and "\n3. <b>" in text

    assert outcome.source_refs == (
        "news:https://example.com/news/1",
        "news:https://example.com/news/2",
        "news:https://example.com/news/3",
    )
    assert outcome.source_stats == (
        SourceStat(source="Культура", items=2),
        SourceStat(source="Наука", items=1),
    )

    stored = await DigestItemsRepository(db_path).list_for_run(run.id)
    assert stored is not None and len(stored) == 3
    assert [entry.url for entry in stored] == [
        "https://example.com/news/1",
        "https://example.com/news/2",
        "https://example.com/news/3",
    ]
    assert [entry.position for entry in stored] == [0, 1, 2]

    # transparency mode: both classifier passes of every accepted item
    entries = await FilterLogRepository(db_path).recent(limit=20)
    stages = [entry.stage for entry in entries]
    assert stages.count("llm_classifier") == 3
    assert stages.count("llm_classifier_confirm") == 3


@pytest.mark.asyncio
async def test_generate_caps_the_issue_at_four_items(db_path: Path) -> None:
    extra = NewsItem(
        title="Парки города получили новые скамейки",
        url="https://example.com/news/4",
        source="Культура",
        published_at=PUBLISHED - timedelta(hours=3),
        summary="Пятнадцать скамеек установили к выходным.",
    )
    another = NewsItem(
        title="Библиотека расширила часы работы",
        url="https://example.com/news/5",
        source="Культура",
        published_at=PUBLISHED - timedelta(hours=4),
        summary="Читальный зал открыт до вечера.",
    )
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 10
        + [
            shorten_answer(
                {
                    1: PHRASE_1,
                    2: PHRASE_2,
                    3: PHRASE_3,
                    4: "Парки получили новые скамейки к выходным.",
                }
            )
        ]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        items_by_feed={"Культура": [ITEM_1, ITEM_2, ITEM_3, extra, another]},
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert (outcome.text or "").count(LINK_LABEL) == MAX_ITEMS
    stored = await DigestItemsRepository(db_path).list_for_run(run.id)
    assert len(stored) == MAX_ITEMS


# --- degradation (plan section 5) ----------------------------------------------------


@pytest.mark.asyncio
async def test_shorter_issue_when_fewer_than_three_news_pass(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 4 + [shorten_answer({1: PHRASE_1, 2: PHRASE_2})]
    )
    pipeline = make_pipeline(
        db_path, llm, items_by_feed={"Культура": [ITEM_1, ITEM_2, FIRE_ITEM]}
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    # the fire item is stopped locally (0 LLM calls for it), the issue
    # goes out shorter: two items, not a failure
    assert outcome.status == "generated"
    assert llm.call_count == 5  # 2 x 2 classifier passes + 1 shortening
    assert (outcome.text or "").count(LINK_LABEL) == 2
    stored = await DigestItemsRepository(db_path).list_for_run(run.id)
    assert len(stored) == 2
    assert "https://example.com/news/fire" not in (outcome.text or "")


@pytest.mark.asyncio
async def test_no_news_at_all_is_replaced_by_fact_of_the_day(db_path: Path) -> None:
    run = await claim_run(db_path)
    fact_id = await add_fact(db_path)
    llm = ScriptedLlm([])
    pipeline = make_pipeline(
        db_path, llm, items_by_feed={"Культура": [FIRE_ITEM]}
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.text == FACT_OF_THE_DAY
    assert outcome.source_refs == (f"bank:fact/{fact_id}",)
    # nothing reached the classifiers: the stop-list rejected locally
    assert llm.call_count == 0
    assert await DigestItemsRepository(db_path).list_for_run(run.id) == []


@pytest.mark.asyncio
async def test_degraded_fact_of_the_day_fails_when_bank_is_empty(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm([])
    pipeline = make_pipeline(db_path, llm, items_by_feed={"Культура": [FIRE_ITEM]})

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "content"
    assert outcome.error is not None and "reserve bank empty" in outcome.error


@pytest.mark.asyncio
async def test_fallback_post_delegates_to_the_bank(db_path: Path) -> None:
    run = await claim_run(db_path)
    fact_id = await add_fact(db_path)
    llm = ScriptedLlm([])
    pipeline = make_pipeline(db_path, llm)

    outcome = await pipeline.fallback_post(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.text == FACT_OF_THE_DAY
    assert outcome.source_refs == (f"bank:fact/{fact_id}",)
    assert llm.call_count == 0


# --- the LLM only shortens: validator drops bad phrases -----------------------------


@pytest.mark.asyncio
async def test_item_with_a_new_number_is_dropped(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 6
        + [
            shorten_answer(
                {1: PHRASE_1, 2: "Выставка собрала 700 старинных открыток.", 3: PHRASE_3}
            )
        ]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    text = outcome.text or ""
    assert text.count(LINK_LABEL) == 2
    assert "700" not in text
    stored = await DigestItemsRepository(db_path).list_for_run(run.id)
    assert [entry.url for entry in stored] == [
        "https://example.com/news/1",
        "https://example.com/news/3",
    ]
    entries = await FilterLogRepository(db_path).recent(limit=20)
    digest_rejects = [e for e in entries if e.stage == "digest_validator"]
    assert len(digest_rejects) == 1
    assert digest_rejects[0].decision == "rejected"
    assert "700" in (digest_rejects[0].reason or "")


@pytest.mark.asyncio
async def test_item_with_a_new_name_is_dropped(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 6
        + [shorten_answer({1: PHRASE_1, 2: "Выставку открыл Смирнов.", 3: PHRASE_3})]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert (outcome.text or "").count(LINK_LABEL) == 2


@pytest.mark.asyncio
async def test_stop_word_in_a_phrase_drops_the_item(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 6
        + [shorten_answer({1: PHRASE_1, 2: "Пожар не помешал открытию выставки.", 3: PHRASE_3})]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert (outcome.text or "").count(LINK_LABEL) == 2
    entries = await FilterLogRepository(db_path).recent(limit=20)
    output_stops = [e for e in entries if e.stage == "digest_output"]
    assert len(output_stops) == 1
    assert "пожар" in (output_stops[0].reason or "")


@pytest.mark.asyncio
async def test_phrase_longer_than_the_record_is_dropped(db_path: Path) -> None:
    # A short record: any full-size phrase is longer than it, which the
    # validator rejects - the model shortens, never expands.
    short_item = NewsItem(
        title="Открылась выставка открыток",
        url="https://example.com/news/2",
        source="Культура",
        published_at=PUBLISHED - timedelta(minutes=30),
        summary="Собрано 120 открыток.",
    )
    long_phrase = (
        "Выставка собрала 120 старинных открыток и продлится до конца "
        "зимнего сезона в музее."
    )
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 6 + [shorten_answer({1: PHRASE_1, 2: long_phrase, 3: PHRASE_3})]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        items_by_feed={"Культура": [ITEM_1, short_item, ITEM_3]},
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert (outcome.text or "").count(LINK_LABEL) == 2
    entries = await FilterLogRepository(db_path).recent(limit=20)
    rejected = [e for e in entries if e.stage == "digest_validator"]
    assert len(rejected) == 1
    assert "длиннее исходной записи" in (rejected[0].reason or "")


@pytest.mark.asyncio
async def test_all_phrases_rejected_fails_the_attempt(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 6 + [shorten_answer({1: "Ровно 999 причин радоваться."})]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "content"
    assert outcome.error is not None and "no item passed" in outcome.error
    assert await DigestItemsRepository(db_path).list_for_run(run.id) == []


# --- infra failures -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shorten_llm_infra_error_fails_with_retryable_kind(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(["позитивная"] * 6 + [LlmCompletionError("infra", "timeout")])
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "infra"


@pytest.mark.asyncio
async def test_shorten_llm_config_error_is_not_retryable(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(["позитивная"] * 6 + [LlmCompletionError("config", "http 402")])
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "failed"
    assert outcome.error_kind == "config"


# --- repeats between issues (plan section 5, "отсутствие повторов") -----------------


@pytest.mark.asyncio
async def test_url_of_an_earlier_issue_is_skipped(db_path: Path) -> None:
    runs = SlotRunRepository(db_path)
    earlier = await runs.try_claim("good_news", "2026-10-09")
    assert earlier is not None
    await DigestItemsRepository(db_path).add(
        earlier.id, [replace(ITEM_1, url="https://example.com/news/1")]
    )
    run = await claim_run(db_path)
    # the seen url still walks both classifier passes (dedup is the last
    # chain stage), so the script carries six classifier answers
    llm = ScriptedLlm(["позитивная"] * 6 + [shorten_answer({1: PHRASE_2, 2: PHRASE_3})])
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    text = outcome.text or ""
    assert "https://example.com/news/1" not in text
    assert text.count(LINK_LABEL) == 2
    stored = await DigestItemsRepository(db_path).list_for_run(run.id)
    assert [entry.url for entry in stored] == [
        "https://example.com/news/2",
        "https://example.com/news/3",
    ]


# --- feeds and config ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_excluded_feed_is_not_fetched(db_path: Path) -> None:
    run = await claim_run(db_path)
    llm = ScriptedLlm(["позитивная"] * 2 + [shorten_answer({1: PHRASE_3})])
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed={"Культура": [ITEM_1], "Наука": [ITEM_3]},
        is_excluded_source=lambda name: name == "Культура",
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.source_stats == (SourceStat(source="Наука", items=1),)
    assert "https://example.com/news/1" not in (outcome.text or "")


@pytest.mark.asyncio
async def test_malformed_items_are_dropped_before_filtering(db_path: Path) -> None:
    run = await claim_run(db_path)
    broken_url = NewsItem(title="Нет ссылки", url="", source="Культура")
    broken_title = NewsItem(title="", url="https://example.com/x", source="Культура")
    llm = ScriptedLlm(["позитивная"] * 2 + [shorten_answer({1: PHRASE_1})])
    pipeline = make_pipeline(
        db_path, llm, items_by_feed={"Культура": [broken_url, broken_title, ITEM_1]}
    )

    outcome = await pipeline.generate(make_rubric(), run, now=NOW)

    assert outcome.status == "generated"
    # only ITEM_1 cost classifier calls
    assert llm.call_count == 3


def test_unknown_validator_name_aborts_startup(db_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown validators"):
        make_pipeline(db_path, ScriptedLlm(), rubric=make_rubric(validators=("nope",)))


@pytest.mark.asyncio
async def test_validator_config_can_widen_checks(db_path: Path) -> None:
    # numbers_match off: a fabricated number now stays in the issue
    run = await claim_run(db_path)
    llm = ScriptedLlm(
        ["позитивная"] * 6
        + [shorten_answer({1: PHRASE_1, 2: "Выставка собрала 700 открыток.", 3: PHRASE_3})]
    )
    pipeline = make_pipeline(
        db_path,
        llm,
        feeds=(FEED_CULTURE, FEED_SCIENCE),
        items_by_feed=three_good_feeds(),
        rubric=make_rubric(validators=("length", "links_required")),
    )

    outcome = await pipeline.generate(
        make_rubric(validators=("length", "links_required")), run, now=NOW
    )

    assert outcome.status == "generated"
    assert (outcome.text or "").count(LINK_LABEL) == 3
