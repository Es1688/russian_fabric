"""News filter chain tests (plan sections 5, 7).

The LLM is a scripting double that records every call, so each test also
asserts how many LLM calls an item cost (0 for local rejections, 1 when the
first pass rejects, 2 otherwise) and that both passes always land in
``filter_log`` — the transparency-mode requirement of plan section 13.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from max_news_bot.core.models import FilterLogEntry, NewsItem
from max_news_bot.rubrics.news_filters import (
    CLASSIFIER_SYSTEM_PROMPT,
    CONFIRM_SYSTEM_PROMPT,
    LABEL_NEGATIVE,
    LABEL_NEUTRAL,
    LABEL_POSITIVE,
    STAGE_CLASSIFIER,
    STAGE_CONFIRM,
    STAGE_DEDUP,
    STAGE_FRESHNESS,
    STAGE_STOPLIST,
    STAGE_TOPIC,
    STATUS_ACCEPTED,
    STATUS_ERROR,
    STATUS_REJECTED,
    NewsFilterChain,
    classifier_user_content,
    find_stop_word,
    find_topic_exclusion,
    parse_classifier_label,
)
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import FilterLogRepository
from max_news_bot.writers.completion import CompletionResult, LlmCompletionError


UTC = timezone.utc
NOW = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
DEFAULT_PUBLISHED = NOW - timedelta(hours=10)

GOOD_TITLE = "Юный художник из Гатчины победил на конкурсе рисунков"
GOOD_URL = "https://example.com/news/1"


class MockLlm:
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


class BrokenFilterLog:
    """FilterLogRepository double that always fails: logging must never
    change the filtering result."""

    async def log(self, entry: FilterLogEntry) -> None:
        raise RuntimeError("db locked")


def make_item(
    title: str = GOOD_TITLE,
    url: str = GOOD_URL,
    *,
    published_at: datetime | None = DEFAULT_PUBLISHED,
    summary: str | None = None,
) -> NewsItem:
    return NewsItem(
        title=title,
        url=url,
        source="Культура",
        published_at=published_at,
        category=None,
        summary=summary,
    )


def make_chain(path: Path, llm: MockLlm, **overrides: Any) -> NewsFilterChain:
    return NewsFilterChain(llm, FilterLogRepository(path), **overrides)


async def log_entries(path: Path) -> list[FilterLogEntry]:
    """filter_log rows of the current test, oldest first."""
    entries = await FilterLogRepository(path).recent(limit=200)
    return list(reversed(entries))


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "news_filters.db"
    await Database(path).initialize()
    return path


# --- pure helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("позитивная", LABEL_POSITIVE),
        ("Позитивная.", LABEL_POSITIVE),
        ("Позитивная новость", LABEL_POSITIVE),
        ("positive", LABEL_POSITIVE),
        ("нейтральная", LABEL_NEUTRAL),
        ("Нейтральный", LABEL_NEUTRAL),
        ("негативная", LABEL_NEGATIVE),
        ("сомнительная", "сомнительная"),
        ("Questionable", "сомнительная"),
        ("возможно", None),
        ("", None),
        ("Ответ: позитивная", None),  # fail-safe: not a one-word answer
    ],
)
def test_parse_classifier_label(answer: str, expected: str | None) -> None:
    assert parse_classifier_label(answer) == expected


def test_find_stop_word_matches_word_prefix_only() -> None:
    # prefix, not substring: "посуда" and "рассудок" must survive "суд"
    assert find_stop_word("Ночной пожар в частном доме", ("суд", "пожар")) == "пожар"
    assert find_stop_word("Суд признал иск", ("суд",)) == "суд"
    assert find_stop_word("Хозяева собрали старинную посуду", ("суд",)) is None
    # case and ё folding
    assert find_stop_word("СМЕРТЕЛЬНАЯ доза", ("смерт",)) == "смерт"
    assert find_stop_word("Всё о тёрке", ("все",)) == "все"


def test_find_topic_exclusion_phrase_and_stem() -> None:
    exclusions = {"fundraising": ("сбор средств",), "payments": ("пенси",)}
    assert find_topic_exclusion("Открыт сбор средств на библиотеку", exclusions) == (
        "fundraising",
        "сбор средств",
    )
    assert find_topic_exclusion("Пенсии проиндексируют", exclusions) == (
        "payments",
        "пенси",
    )
    # "сборник" must not trigger the phrase
    assert find_topic_exclusion("Вышел сборник стихов", exclusions) is None


def test_classifier_user_content_marks_data_and_limits_summary() -> None:
    item = make_item(summary="Хорошие новости о конкурсе.")
    content = classifier_user_content(item)
    assert content.startswith("<article>")
    assert f"Заголовок: {GOOD_TITLE}" in content
    assert "Хорошие новости о конкурсе." in content
    assert content.rstrip().endswith(
        "позитивная, нейтральная, негативная или сомнительная."
    )

    long = classifier_user_content(make_item(summary="а" * 3000))
    assert len(long) < 3000  # summary truncated to the token budget


# --- local stages: stop-list and topical exclusions ------------------------


@pytest.mark.asyncio
async def test_stop_word_rejects_without_llm_calls(db_path: Path) -> None:
    llm = MockLlm()
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(
        make_item(title="Ночной пожар в частном доме"), now=NOW
    )

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_STOPLIST
    assert "пожар" in (verdict.reason or "")
    assert llm.call_count == 0  # local rejection, no LLM call spent
    entries = await log_entries(db_path)
    assert len(entries) == 1
    assert entries[0].stage == STAGE_STOPLIST
    assert entries[0].decision == "rejected"
    assert "пожар" in (entries[0].reason or "")
    assert entries[0].url == GOOD_URL
    assert entries[0].title == "Ночной пожар в частном доме"


@pytest.mark.asyncio
async def test_topic_exclusion_rejects(db_path: Path) -> None:
    llm = MockLlm()
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(
        make_item(title="Пенсии и льготы проиндексируют с нового года"), now=NOW
    )

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_TOPIC
    assert "payments_and_benefits" in (verdict.reason or "")
    assert llm.call_count == 0
    entries = await log_entries(db_path)
    assert entries[0].stage == STAGE_TOPIC
    assert entries[0].decision == "rejected"


@pytest.mark.asyncio
async def test_default_lists_cover_the_plan_examples(db_path: Path) -> None:
    chain = make_chain(db_path, MockLlm())
    for word in ("смерть", "ДТП", "пожар", "суд", "задержан", "война", "болезнь"):
        verdict = await chain.filter_one(
            make_item(title=f"Новости: {word} в городе"), now=NOW
        )
        assert verdict.stage == STAGE_STOPLIST, word


# --- the two classifier passes ---------------------------------------------


@pytest.mark.asyncio
async def test_positive_item_passes_both_independent_passes(db_path: Path) -> None:
    llm = MockLlm(["позитивная"])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(
        make_item(summary="Жюри отметило тёплый рисунок."), now=NOW
    )

    assert verdict.status == STATUS_ACCEPTED
    assert verdict.first_label == LABEL_POSITIVE
    assert verdict.second_label == LABEL_POSITIVE
    assert llm.call_count == 2

    # the two passes are independent: different system prompts, same article
    first_system, first_user = llm.calls[0]
    second_system, second_user = llm.calls[1]
    assert first_system == CLASSIFIER_SYSTEM_PROMPT
    assert second_system == CONFIRM_SYSTEM_PROMPT
    assert first_system != second_system
    assert first_user == second_user
    assert "<article>" in first_user
    assert f"Заголовок: {GOOD_TITLE}" in first_user
    assert "Жюри отметило тёплый рисунок." in first_user
    assert "ровно одним словом" in first_user

    # transparency: both answers in the log, in chain order
    entries = await log_entries(db_path)
    assert [(e.stage, e.decision, e.reason) for e in entries] == [
        (STAGE_CLASSIFIER, LABEL_POSITIVE, None),
        (STAGE_CONFIRM, LABEL_POSITIVE, None),
    ]
    assert entries[0].model == "test-model"
    assert entries[0].response == "позитивная"


@pytest.mark.asyncio
async def test_passes_disagree_rejects(db_path: Path) -> None:
    llm = MockLlm(["позитивная", "негативная"])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_CONFIRM
    assert "passes disagree" in (verdict.reason or "")
    assert "позитивная" in (verdict.reason or "")
    assert "негативная" in (verdict.reason or "")
    assert verdict.first_label == LABEL_POSITIVE
    assert verdict.second_label == LABEL_NEGATIVE
    assert llm.call_count == 2
    entries = await log_entries(db_path)
    assert [(e.stage, e.decision) for e in entries] == [
        (STAGE_CLASSIFIER, LABEL_POSITIVE),
        (STAGE_CONFIRM, LABEL_NEGATIVE),
    ]
    assert "passes disagree" in (entries[1].reason or "")


@pytest.mark.asyncio
async def test_negative_rejected_on_first_pass_saves_second_call(db_path: Path) -> None:
    llm = MockLlm(["негативная"])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_CLASSIFIER
    assert "не accepted" in (verdict.reason or "") or "not accepted" in (
        verdict.reason or ""
    )
    assert verdict.first_label == LABEL_NEGATIVE
    assert verdict.second_label is None  # never ran
    assert llm.call_count == 1
    entries = await log_entries(db_path)
    assert len(entries) == 1
    assert entries[0].decision == LABEL_NEGATIVE
    assert entries[0].reason is not None


@pytest.mark.asyncio
async def test_unreadable_answer_rejects_fail_safe(db_path: Path) -> None:
    llm = MockLlm(["возможно, всё сложно"])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_CLASSIFIER
    assert "unreadable" in (verdict.reason or "")
    assert llm.call_count == 1
    entries = await log_entries(db_path)
    assert entries[0].decision == "unreadable"
    assert entries[0].response == "возможно, всё сложно"


@pytest.mark.asyncio
async def test_unreadable_confirmation_answer_rejects(db_path: Path) -> None:
    llm = MockLlm(["позитивная", "затрудняюсь ответить"])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_CONFIRM
    assert "unreadable" in (verdict.reason or "")
    assert verdict.first_label == LABEL_POSITIVE


@pytest.mark.asyncio
async def test_neutral_rejected_by_default_accepted_when_configured(
    db_path: Path,
) -> None:
    default = make_chain(db_path, MockLlm(["нейтральная"]))
    verdict = await default.filter_one(make_item(), now=NOW)
    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_CLASSIFIER
    assert LABEL_NEUTRAL in (verdict.reason or "")

    widened = make_chain(
        db_path,
        MockLlm(["нейтральная"]),
        accepted_labels=(LABEL_POSITIVE, LABEL_NEUTRAL),
    )
    verdict = await widened.filter_one(make_item(), now=NOW)
    assert verdict.status == STATUS_ACCEPTED
    assert verdict.second_label == LABEL_NEUTRAL


# --- freshness ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_item_rejected_at_freshness_after_llm(db_path: Path) -> None:
    # plan order: freshness follows the LLM stages, so the calls are spent
    llm = MockLlm(["позитивная"])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(
        make_item(published_at=NOW - timedelta(hours=72)), now=NOW
    )

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_FRESHNESS
    assert "stale" in (verdict.reason or "")
    assert "48" in (verdict.reason or "")
    assert llm.call_count == 2
    entries = await log_entries(db_path)
    assert [e.stage for e in entries] == [
        STAGE_CLASSIFIER,
        STAGE_CONFIRM,
        STAGE_FRESHNESS,
    ]


@pytest.mark.asyncio
async def test_min_freshness_bound_rejects_too_fresh(db_path: Path) -> None:
    chain = make_chain(db_path, MockLlm(["позитивная"]), freshness_min_hours=24)

    verdict = await chain.filter_one(
        make_item(published_at=NOW - timedelta(hours=12)), now=NOW
    )

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_FRESHNESS
    assert "too fresh" in (verdict.reason or "")


@pytest.mark.asyncio
async def test_missing_publish_date_rejected(db_path: Path) -> None:
    chain = make_chain(db_path, MockLlm(["позитивная"]))

    verdict = await chain.filter_one(make_item(published_at=None), now=NOW)

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_FRESHNESS
    assert "no publish date" in (verdict.reason or "")


@pytest.mark.asyncio
async def test_future_publish_date_rejected_but_small_skew_tolerated(
    db_path: Path,
) -> None:
    chain = make_chain(db_path, MockLlm(["позитивная"]))

    broken = await chain.filter_one(
        make_item(published_at=NOW + timedelta(hours=3)), now=NOW
    )
    assert broken.status == STATUS_REJECTED
    assert "future" in (broken.reason or "")

    # a feed clock a few minutes ahead must not drop fresh news
    skewed = await chain.filter_one(
        make_item(published_at=NOW + timedelta(minutes=30)), now=NOW
    )
    assert skewed.status == STATUS_ACCEPTED


# --- repeats -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_sent_url_rejected(db_path: Path) -> None:
    async def is_seen(url: str) -> bool:
        return url == GOOD_URL

    chain = make_chain(db_path, MockLlm(["позитивная"]), is_seen=is_seen)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_REJECTED
    assert verdict.stage == STAGE_DEDUP
    assert "already sent" in (verdict.reason or "")


@pytest.mark.asyncio
async def test_batch_dedups_same_canonical_url(db_path: Path) -> None:
    chain = make_chain(db_path, MockLlm(["позитивная"]))
    report = await chain.filter_many(
        [
            make_item(),
            make_item(
                title="Концерт во дворе дома собрал соседей",
                url=f"{GOOD_URL}/?utm_source=rss",
            ),
        ],
        now=NOW,
    )

    assert report.accepted == (make_item(),)
    second = report.verdicts[1]
    assert second.status == STATUS_REJECTED
    assert second.stage == STAGE_DEDUP
    assert "duplicate url" in (second.reason or "")


@pytest.mark.asyncio
async def test_batch_dedups_similar_titles(db_path: Path) -> None:
    chain = make_chain(db_path, MockLlm(["позитивная"]))
    report = await chain.filter_many(
        [
            make_item(title="В Петербурге открылась новая выставка старинных книг"),
            make_item(
                title="В Петербурге открылась новая выставка старинных монет",
                url="https://example.com/news/2",
            ),
            # a similar headline that was NOT accepted must not block its own run
            make_item(
                title="В Петербурге открылась новая выставка старинных монет и медалей",
                url="https://example.com/news/3",
            ),
        ],
        now=NOW,
    )

    assert report.rejected[0].stage == STAGE_DEDUP
    assert "too similar" in (report.rejected[0].reason or "")
    # the third item repeats a *rejected* headline: no repeat to avoid
    assert report.verdicts[2].status == STATUS_ACCEPTED


@pytest.mark.asyncio
async def test_batch_mixed_report(db_path: Path) -> None:
    chain = make_chain(db_path, MockLlm(["позитивная"]))
    report = await chain.filter_many(
        [
            make_item(),
            make_item(title="ДТП перекрыло шоссе", url="https://example.com/news/2"),
            make_item(
                title="Юный художник из Гатчины победил на городском конкурсе",
                url="https://example.com/news/3",
            ),
            make_item(
                title="Концерт во дворе дома собрал соседей",
                url=f"{GOOD_URL}/",
            ),
        ],
        now=NOW,
    )

    assert len(report.verdicts) == 4
    assert report.accepted == (make_item(),)
    assert [v.stage for v in report.rejected] == [STAGE_STOPLIST, STAGE_DEDUP, STAGE_DEDUP]
    assert report.errors == ()
    # every rejection and both passes of the accepted item are logged
    entries = await log_entries(db_path)
    assert [e.stage for e in entries] == [
        STAGE_CLASSIFIER,
        STAGE_CONFIRM,
        STAGE_STOPLIST,
        STAGE_CLASSIFIER,
        STAGE_CONFIRM,
        STAGE_DEDUP,
        STAGE_CLASSIFIER,
        STAGE_CONFIRM,
        STAGE_DEDUP,
    ]


# --- failures: error verdicts, not silent rejections ----------------------------


@pytest.mark.asyncio
async def test_llm_infra_error_is_error_verdict(db_path: Path) -> None:
    llm = MockLlm([TimeoutError("dial timed out")])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_ERROR
    assert verdict.error_kind == "infra"
    assert verdict.stage == STAGE_CLASSIFIER
    entries = await log_entries(db_path)
    assert entries[0].decision == "error"
    assert entries[0].reason is not None and entries[0].reason.startswith("infra")


@pytest.mark.asyncio
async def test_llm_config_error_kind(db_path: Path) -> None:
    llm = MockLlm([LlmCompletionError("config", "llm http 402")])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_ERROR
    assert verdict.error_kind == "config"


@pytest.mark.asyncio
async def test_second_pass_error_keeps_first_label(db_path: Path) -> None:
    llm = MockLlm(["позитивная", TimeoutError("dial timed out")])
    chain = make_chain(db_path, llm)

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_ERROR
    assert verdict.first_label == LABEL_POSITIVE
    assert verdict.second_label is None
    # the first-pass answer is still logged before the error entry
    entries = await log_entries(db_path)
    assert [(e.stage, e.decision) for e in entries] == [
        (STAGE_CLASSIFIER, LABEL_POSITIVE),
        (STAGE_CONFIRM, "error"),
    ]


@pytest.mark.asyncio
async def test_log_failure_never_changes_the_result(tmp_path: Path) -> None:
    llm = MockLlm(["позитивная"])
    chain = NewsFilterChain(llm, BrokenFilterLog())  # type: ignore[arg-type]

    verdict = await chain.filter_one(make_item(), now=NOW)

    assert verdict.status == STATUS_ACCEPTED
    assert llm.call_count == 2
