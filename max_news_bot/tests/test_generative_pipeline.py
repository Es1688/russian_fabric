"""Generative pipeline tests (plan sections 3, 7): morning/night posts.

The LLM is a scripting double that records every call, so each test also
asserts the hard budget of the plan - no more than 3 LLM calls per slot,
generation and safety checker combined, across all scheduler attempts.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from max_news_bot.bot.slot_scheduler import SlotPublishResult, SlotScheduler
from max_news_bot.config.rubrics import (
    RubricConfig,
    RubricsConfig,
    TargetConfig,
    WEEKDAYS,
)
from max_news_bot.tests.conftest import make_llm_channels
from max_news_bot.core.models import ContentBankItem
from max_news_bot.images.base import ImageGenerationError
from max_news_bot.images.fallback import FallbackImageGenerator
from max_news_bot.rubrics.generative import (
    LLM_CALLS_PER_SLOT,
    GenerativePipeline,
    build_image_prompt,
    parse_checker_answer,
)
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    FilterLogRepository,
    RubricStateRepository,
    SlotRunRepository,
)
from max_news_bot.writers.completion import (
    CompletionResult,
    LlmCompletionClient,
    LlmCompletionError,
)
from max_news_bot.writers.validator import find_cliche_phrase
from max_news_bot.tests.mocks import MockAlerter


UTC = timezone.utc
ALL_DAYS = tuple(WEEKDAYS)
# Friday, October: month 10, event_date "10-09"
DAY = date(2026, 10, 9)
NOW = datetime(DAY.year, DAY.month, DAY.day, 18, 30, tzinfo=UTC)
MAIN_CHAT = -1001234567890

PERSONA = "Ты — редактор канала для читателей старшего возраста."
OVERLAY = "Заверши пост короткой мыслью из банка, своих цитат не добавляй."
TOPIC_FRI = "Дача и сезонные дела северо-запада."
# Topic grid of the plan section 2 on the fixed test week 2026-10-05..10
# (Monday..Saturday; Sunday is the quiet day and has no topic).
WEEK_START = date(2026, 10, 5)
TOPIC_BY_DAY = {
    "mon": "Здоровье и бодрость: общие советы.",
    "tue": "Безопасность: мошенники, звонки, СМС.",
    "wed": "Дача и сезонные дела северо-запада.",
    "thu": "Технологии простым языком.",
    "fri": "История, культура, «в этот день».",
    "sat": "Кулинария, семья, мягкий юмор.",
}

GOOD_TEXT = (
    "Спокойной ночи. Пусть сон будет крепким, а утро добрым. Сегодняшний "
    "день закончился, и вы сделали всё, что могли. Пусть завтрашний день "
    "принесёт вам тёплые встречи, приятные заботы и немного тишины для "
    "отдыха. Дышите глубже, укрывайтесь теплее и позвольте себе отдохнуть "
    "по-настоящему."
)
GOOD_TEXT_2 = (
    "Добрый вечер. День закрыл свои страницы, и впереди тихий вечер. "
    "Положите телефон подальше, заварите чай, откройте любимую книгу или "
    "просто посмотрите в окно. Ночь — время отдыха, а не решений и тревог. "
    "Пусть сны будут светлыми, а утро начнётся с тёплого чая и добрых "
    "новостей."
)
CLICHE_TEXT = GOOD_TEXT_2 + " Как говорится, день завершён."
RESERVE_TEXT = (
    "Дорогие друзья, сегодняшний выпуск готовится заново, поэтому мы "
    "делимся простой мыслью: хорошее утро начинается со спокойного вечера. "
    "Пусть этот вечер будет добрым к вам. Побудьте в тишине, вспомните "
    "что-то приятное, пожелайте близким спокойной ночи. Мы вернёмся утром "
    "с новой встречей и тёплыми словами для вас."
)
SEED_THOUGHT_A = (
    "Мысль из банка: «Хорошее утро начинается со спокойного вечера» "
    "(народная мудрость)."
)
SEED_THOUGHT_B = (
    "Мысль из банка: «Дерево держится корнями, а человек — семьёй» "
    "(пословица)."
)
SEED_FACT = "Запись банка: блокада Ленинграда длилась 872 дня, с 1941 по 1944 год."
SEED_EVENT = (
    "Событие этого дня из банка: календарная запись с датой десять-девять."
)
IMAGE_STYLE = "Тёплые цвета, крупные элементы, высокий контраст."
PNG_BYTES = b"\x89PNG\r\n\x1a\n fake generated image bytes"


class MockLlm:
    """LlmCompletion double: scripted items played in order (the last one
    repeats), every call recorded. A str becomes a CompletionResult, a
    BaseException is raised, a CompletionResult passes through."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self._script = list(script or [])
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> CompletionResult:
        self.calls.append((system, user))
        if not self._script:
            return CompletionResult(content="нет")
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, CompletionResult):
            return item
        return CompletionResult(content=str(item), model="test-model")

    @property
    def call_count(self) -> int:
        return len(self.calls)


class MockImageGenerator:
    """ImageGenerator double: scripted bytes / errors, prompts recorded."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> bytes:
        self.prompts.append(prompt)
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class RecordingPublisher:
    """SlotPublisher double; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_slot(
        self,
        text: str,
        *,
        chat_id: int,
        image_ref: str | None = None,
        to_admins: bool = False,
    ):
        self.calls.append(
            {
                "text": text,
                "chat_id": chat_id,
                "image_ref": image_ref,
                "to_admins": to_admins,
            }
        )
        return SlotPublishResult(success=True, message_id="mid.7000")


def make_rubric(
    rubric_id: str = "good_night",
    *,
    time_str: str = "21:30",
    kind: str = "generative",
    validators: tuple[str, ...] = ("length", "cliches"),
    bank_types: tuple[str, ...] | None = ("thought",),
    prompt_overlay: str | None = None,
    topics_by_weekday: dict[str, tuple[str, ...]] | None = None,
    on_llm_failure: str = "bank",
    min_chars: int = 200,
    max_chars: int = 1200,
    image: str | None = "postcard",
    history_n: int = 5,
    similarity_threshold: float = 0.7,
) -> RubricConfig:
    return RubricConfig(
        id=rubric_id,
        enabled=True,
        time=time_str,
        days=ALL_DAYS,
        grace_minutes=60,
        jitter_minutes=0,
        kind=kind,
        target="main",
        source={"bank_types": list(bank_types)} if bank_types else None,
        topics_by_weekday=topics_by_weekday or {},
        prompt_overlay=prompt_overlay,
        min_chars=min_chars,
        max_chars=max_chars,
        image=image,
        history_n=history_n,
        similarity_threshold=similarity_threshold,
        validators=validators,
        on_llm_failure=on_llm_failure,
    )


def make_config(
    rubrics: list[RubricConfig],
    *,
    persona: str = PERSONA,
    image_style: str = "",
) -> RubricsConfig:
    return RubricsConfig(
        llm=make_llm_channels(),
        timezone="UTC",
        persona=persona,
        image_style=image_style,
        targets={"main": TargetConfig(name="main", channel_id=MAIN_CHAT)},
        daily_cap=6,
        weather_points=(),
        rubrics=tuple(rubrics),
    )


def make_pipeline(
    path: Path,
    rubrics: list[RubricConfig],
    llm: MockLlm,
    *,
    persona: str = PERSONA,
    image_style: str = "",
    images: FallbackImageGenerator | None = None,
    generated_dir: Path | None = None,
    db: Database | None = None,
) -> GenerativePipeline:
    return GenerativePipeline(
        make_config(rubrics, persona=persona, image_style=image_style),
        llm,
        ContentBankRepository(path),
        SlotRunRepository(path),
        FilterLogRepository(path),
        images=images,
        generated_dir=generated_dir,
        db=db,
    )


async def add_bank_item(
    path: Path,
    item_type: str,
    text: str,
    *,
    months: tuple[int, ...] = (),
    event_date: str | None = None,
    review_status: str = "approved",
    show_count: int = 0,
) -> int:
    repo = ContentBankRepository(path)
    return await repo.add(
        ContentBankItem(
            id=None,
            item_type=item_type,
            text=text,
            metadata={},
            review_status=review_status,
            show_count=show_count,
            months=months,
            event_date=event_date,
        )
    )


async def claim_run(path: Path, rubric_id: str, slot_date: str, attempts: int = 1):
    run = await SlotRunRepository(path).try_claim(rubric_id, slot_date)
    assert run is not None
    return replace(run, attempts=attempts)


async def publish_history(path: Path, rubric_id: str, slot_date: str, text: str):
    """A previously published post of the rubric (anti-repeat history)."""
    run = await claim_run(path, rubric_id, slot_date)
    await SlotRunRepository(path).save(
        replace(run, status="published", text=text)
    )


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "generative.db"
    await Database(path).initialize()
    return path


# --- checker answer parsing ---


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("да", "danger"),
        ("Да", "danger"),
        ("Да, есть медицинские советы", "danger"),
        ("yes", "danger"),
        ("нет", "pass"),
        ("Нет.", "pass"),
        ("Нет, ничего такого нет", "pass"),
        ("no", "pass"),
        ("возможно", None),
        ("", None),
        ("Сложно сказать однозначно", None),
    ],
)
def test_parse_checker_answer(answer: str, expected: str | None) -> None:
    assert parse_checker_answer(answer) == expected


def test_find_cliche_phrase() -> None:
    assert find_cliche_phrase("Как говорится, день прошёл") is not None
    assert find_cliche_phrase("Само собой разумеется, что это так") is not None
    assert find_cliche_phrase(GOOD_TEXT) is None


def test_build_image_prompt_style_plus_topic() -> None:
    prompt = build_image_prompt("  Стиль.  ", "Тема\n   дня.")
    assert prompt == "Стиль.\nТема дня."
    assert build_image_prompt("", "Тема.") == "Тема."
    assert build_image_prompt("Стиль.", "") == "Стиль."


def test_build_image_prompt_capped_at_500_chars() -> None:
    prompt = build_image_prompt("x" * 450, "y" * 300)
    assert len(prompt) == 500
    assert prompt.startswith("x")


# --- one attempt: prompt, validators, checker ---


@pytest.mark.asyncio
async def test_generate_happy_path(db_path: Path) -> None:
    rubric = make_rubric(prompt_overlay=OVERLAY)
    seed_id = await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    await publish_history(db_path, "good_night", "2026-10-02", GOOD_TEXT_2)
    llm = MockLlm([GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW)

    assert outcome.status == "generated"
    assert outcome.text == GOOD_TEXT
    assert outcome.image_ref == "postcard"
    assert outcome.source_refs == (f"bank:{seed_id}",)
    # budget: one generation + one checker call, nothing more
    assert llm.call_count == 2

    system, user = llm.calls[0]
    assert "старшего возраста" in system  # persona (§3)
    assert OVERLAY in system  # rubric prompt_overlay
    assert SEED_THOUGHT_A in user  # bank seed as the only material
    assert GOOD_TEXT_2[:60] in user  # anti-repeat history in the prompt

    checker_system, checker_user = llm.calls[1]
    assert checker_system is not system
    assert GOOD_TEXT in checker_user
    assert "да или нет" in checker_user

    entries = await FilterLogRepository(db_path).recent()
    assert [entry.stage for entry in entries] == ["llm_checker"]
    assert entries[0].decision == "passed"
    assert entries[0].title == "good_night"
    assert entries[0].response == "нет"

    seed = await ContentBankRepository(db_path).get(seed_id)
    assert seed is not None and seed.show_count == 1  # rotation bump


@pytest.mark.asyncio
async def test_topic_of_the_day_is_passed_to_prompt(db_path: Path) -> None:
    rubric = make_rubric(
        "topic_day",
        validators=("length", "cliches", "numbers_match"),
        bank_types=("event", "fact"),
        topics_by_weekday={"fri": (TOPIC_FRI,)},
        image=None,
    )
    await add_bank_item(db_path, "event", SEED_EVENT, event_date="10-09")
    llm = MockLlm([GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "topic_day", "2026-10-09"), now=NOW
    )

    assert outcome.status == "generated"
    assert outcome.image_ref is None
    _, user = llm.calls[0]
    assert TOPIC_FRI in user
    # first configured type with candidates wins: the event seed, not a fact
    assert SEED_EVENT in user


@pytest.mark.asyncio
async def test_no_topic_day_skips_without_llm_calls(db_path: Path) -> None:
    rubric = make_rubric(topics_by_weekday={"mon": ("Тема понедельника",)})
    llm = MockLlm()
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "skipped"
    assert outcome.skip_reason == "no_topic"
    assert llm.call_count == 0


# --- topic of the day: the weekday grid and the calendar (plan sections 2, 6) ---


@pytest.mark.parametrize("offset", range(6))
@pytest.mark.asyncio
async def test_topic_day_each_weekday_gets_its_configured_topic(
    db_path: Path, offset: int
) -> None:
    day = WEEK_START + timedelta(days=offset)
    topic = TOPIC_BY_DAY[WEEKDAYS[day.weekday()]]
    next_day_topic = TOPIC_BY_DAY[WEEKDAYS[(day.weekday() + 1) % 6]]
    rubric = make_rubric(
        "topic_day",
        bank_types=None,
        topics_by_weekday={key: (value,) for key, value in TOPIC_BY_DAY.items()},
        image=None,
    )
    llm = MockLlm([GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric,
        await claim_run(db_path, "topic_day", day.isoformat()),
        now=datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC),
    )

    assert outcome.status == "generated"
    _, user = llm.calls[0]
    assert f"Тема дня: {topic}" in user
    assert next_day_topic not in user  # only today's topic, no neighbours


@pytest.mark.asyncio
async def test_this_day_event_wins_over_fact_rotation(db_path: Path) -> None:
    """Calendar (plan sections 2, 6): an "event" record with today's
    event_date outranks the fact rotation, whatever the insertion order."""
    rubric = make_rubric(
        "topic_day",
        validators=("length", "cliches", "numbers_match"),
        bank_types=("event", "fact"),
        topics_by_weekday={"fri": (TOPIC_FRI,)},
        image=None,
    )
    await add_bank_item(db_path, "fact", SEED_FACT)  # lower id wins the rotation
    event_id = await add_bank_item(db_path, "event", SEED_EVENT, event_date="10-09")
    llm = MockLlm([GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "topic_day", "2026-10-09"), now=NOW
    )

    assert outcome.status == "generated"
    assert outcome.source_refs == (f"bank:{event_id}",)
    _, user = llm.calls[0]
    assert SEED_EVENT in user
    assert SEED_FACT not in user  # the calendar seed replaced the rotation


@pytest.mark.asyncio
async def test_event_of_another_day_falls_back_to_fact(db_path: Path) -> None:
    """No calendar match today: the next configured type takes the slot."""
    rubric = make_rubric(
        "topic_day",
        validators=("length", "cliches", "numbers_match"),
        bank_types=("event", "fact"),
        topics_by_weekday={"fri": (TOPIC_FRI,)},
        image=None,
    )
    await add_bank_item(db_path, "event", SEED_EVENT, event_date="12-31")
    fact_id = await add_bank_item(db_path, "fact", SEED_FACT)
    llm = MockLlm([GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "topic_day", "2026-10-09"), now=NOW
    )

    assert outcome.status == "generated"
    assert outcome.source_refs == (f"bank:{fact_id}",)
    _, user = llm.calls[0]
    assert SEED_FACT in user
    assert SEED_EVENT not in user


@pytest.mark.asyncio
async def test_local_validator_failure_then_regeneration_stays_in_budget(
    db_path: Path,
) -> None:
    rubric = make_rubric()
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    await add_bank_item(db_path, "thought", SEED_THOUGHT_B)
    llm = MockLlm([CLICHE_TEXT, GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)
    run = await claim_run(db_path, "good_night", "2026-10-09")

    first = await pipeline.generate(rubric, replace(run, attempts=1), now=NOW)
    second = await pipeline.generate(rubric, replace(run, attempts=2), now=NOW)

    assert first.status == "failed"
    assert first.error_kind == "content"
    assert "cliche" in (first.error or "")
    assert second.status == "generated"
    # 2 generations + 1 checker = 3: the plan's per-slot budget exactly
    assert llm.call_count == 3
    # the bank rotated: the second attempt used the other seed
    assert SEED_THOUGHT_A in llm.calls[0][1]
    assert SEED_THOUGHT_B in llm.calls[1][1]


@pytest.mark.asyncio
async def test_similarity_gate_rejects_near_duplicate(db_path: Path) -> None:
    rubric = make_rubric()
    await publish_history(db_path, "good_night", "2026-10-08", GOOD_TEXT)
    llm = MockLlm([GOOD_TEXT])  # same text as yesterday
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "failed"
    assert "too similar" in (outcome.error or "")
    assert llm.call_count == 1  # rejected locally, no checker call spent


@pytest.mark.asyncio
async def test_numbers_must_come_from_bank_material(db_path: Path) -> None:
    rubric = make_rubric(validators=("length", "cliches", "numbers_match"))
    await add_bank_item(db_path, "thought", SEED_FACT)
    llm = MockLlm(
        [
            GOOD_TEXT + " Это было в 1942 году.",
            GOOD_TEXT + " Это было в 1941 году.",
            "нет",
        ]
    )
    pipeline = make_pipeline(db_path, [rubric], llm)
    run = await claim_run(db_path, "good_night", "2026-10-09")

    invented = await pipeline.generate(rubric, replace(run, attempts=1), now=NOW)
    backed = await pipeline.generate(rubric, replace(run, attempts=2), now=NOW)

    assert invented.status == "failed"
    assert "numbers not backed" in (invented.error or "")
    assert "1942" in (invented.error or "")
    assert backed.status == "generated"
    assert llm.call_count == 3


@pytest.mark.asyncio
async def test_numbers_without_material_are_rejected(db_path: Path) -> None:
    rubric = make_rubric(
        validators=("length", "cliches", "numbers_match"), bank_types=None
    )
    llm = MockLlm([GOOD_TEXT + " В 1941 году."])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "failed"
    assert "numbers not backed" in (outcome.error or "")


@pytest.mark.asyncio
async def test_checker_rejection_is_logged(db_path: Path) -> None:
    rubric = make_rubric()
    llm = MockLlm([GOOD_TEXT, "Да, есть медицинские советы"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "failed"
    assert outcome.error_kind == "content"
    assert "safety checker rejected" in (outcome.error or "")
    entries = await FilterLogRepository(db_path).recent()
    assert entries[0].stage == "llm_checker"
    assert entries[0].decision == "rejected"
    assert entries[0].response == "Да, есть медицинские советы"


@pytest.mark.asyncio
async def test_checker_unreadable_answer_rejects_fail_safe(db_path: Path) -> None:
    rubric = make_rubric()
    llm = MockLlm([GOOD_TEXT, "Возможно, частично"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "failed"
    assert "unreadable" in (outcome.error or "")
    assert (await FilterLogRepository(db_path).recent())[0].decision == "unreadable"


@pytest.mark.asyncio
async def test_llm_error_taxonomy(db_path: Path) -> None:
    rubric = make_rubric()
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)

    infra = make_pipeline(db_path, [rubric], MockLlm([TimeoutError("dial timed out")]))
    outcome = await infra.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )
    assert outcome.status == "failed"
    assert outcome.error_kind == "infra"

    config = make_pipeline(
        db_path,
        [rubric],
        MockLlm([LlmCompletionError("config", "llm http 402")]),
    )
    outcome = await config.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-10"), now=NOW
    )
    assert outcome.status == "failed"
    assert outcome.error_kind == "config"


# --- the hard budget: at most 3 LLM calls per slot ---


@pytest.mark.asyncio
async def test_budget_never_exceeds_three_calls(db_path: Path) -> None:
    rubric = make_rubric()
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    llm = MockLlm([CLICHE_TEXT])  # every generation fails the cliche check
    pipeline = make_pipeline(db_path, [rubric], llm)
    run = await claim_run(db_path, "good_night", "2026-10-09")

    outcomes = [
        await pipeline.generate(rubric, replace(run, attempts=attempt), now=NOW)
        for attempt in (1, 2, 3)  # the scheduler's attempt loop
    ]

    assert all(outcome.status == "failed" for outcome in outcomes)
    assert llm.call_count == 3  # 3 generations, 0 wasted checker calls


@pytest.mark.asyncio
async def test_exhausted_budget_never_publishes_unchecked_post(db_path: Path) -> None:
    rubric = make_rubric()
    llm = MockLlm([GOOD_TEXT, "да", GOOD_TEXT_2])
    pipeline = make_pipeline(db_path, [rubric], llm)
    run = await claim_run(db_path, "good_night", "2026-10-09")

    first = await pipeline.generate(rubric, replace(run, attempts=1), now=NOW)
    second = await pipeline.generate(rubric, replace(run, attempts=2), now=NOW)
    third = await pipeline.generate(rubric, replace(run, attempts=3), now=NOW)

    # gen(1) -> checker(2) rejects -> regen(3) passes locally, but no budget
    # is left for the safety check: rejected fail-safe, never published
    assert first.status == "failed"
    assert "safety checker rejected" in (first.error or "")
    assert second.status == "failed"
    assert "safety check skipped" in (second.error or "")
    # attempt 3 finds the slot budget fully spent: no call at all
    assert third.status == "failed"
    assert "budget exhausted" in (third.error or "")
    assert llm.call_count == 3


@pytest.mark.asyncio
async def test_recovered_run_budget_accounts_previous_process(db_path: Path) -> None:
    rubric = make_rubric()
    llm = MockLlm([GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)

    # crash leftover: the previous process burned 2 attempts; the budget
    # starts with 2 calls spent, so only one generation fits
    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09", attempts=3), now=NOW
    )

    assert outcome.status == "failed"
    assert "safety check skipped" in (outcome.error or "")
    assert llm.call_count == 1


# --- fallback: reserve post from the bank, no LLM ---


@pytest.mark.asyncio
async def test_fallback_post_month_aware_no_llm(db_path: Path) -> None:
    rubric = make_rubric()
    any_month_id = await add_bank_item(db_path, "reserve", RESERVE_TEXT)
    await add_bank_item(db_path, "reserve", "Декабрьский резерв" * 20, months=(12,))
    llm = MockLlm()
    pipeline = make_pipeline(db_path, [rubric], llm)

    outcome = await pipeline.fallback_post(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "generated"
    assert outcome.text == RESERVE_TEXT
    assert outcome.source_refs == (f"bank:{any_month_id}",)
    assert llm.call_count == 0  # the bank reserve costs no LLM call
    item = await ContentBankRepository(db_path).get(any_month_id)
    assert item is not None and item.show_count == 1


@pytest.mark.asyncio
async def test_fallback_post_empty_bank_fails(db_path: Path) -> None:
    rubric = make_rubric()
    pipeline = make_pipeline(db_path, [rubric], MockLlm())

    outcome = await pipeline.fallback_post(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "failed"
    assert "reserve" in (outcome.error or "")


@pytest.mark.asyncio
async def test_fallback_post_length_validated(db_path: Path) -> None:
    rubric = make_rubric()
    await add_bank_item(db_path, "reserve", "Слишком короткий резерв.")
    pipeline = make_pipeline(db_path, [rubric], MockLlm())

    outcome = await pipeline.fallback_post(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "failed"
    assert "length" in (outcome.error or "")


def test_unknown_validator_name_aborts_startup(db_path: Path) -> None:
    with pytest.raises(ValueError, match="bogus"):
        make_pipeline(db_path, [make_rubric(validators=("length", "bogus"))], MockLlm())


# --- generated images: image: generate (dual-channel plan §3.3) ---


@pytest.mark.asyncio
async def test_image_generate_writes_file_and_generated_ref(
    db_path: Path, tmp_path: Path
) -> None:
    rubric = make_rubric(image="generate")
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    generator = MockImageGenerator([PNG_BYTES])
    generated_dir = tmp_path / "generated"
    db = Database(db_path)
    pipeline = make_pipeline(
        db_path,
        [rubric],
        MockLlm([GOOD_TEXT, "нет"]),
        image_style=IMAGE_STYLE,
        images=FallbackImageGenerator(generator, None, db=db),
        generated_dir=generated_dir,
        db=db,
    )
    run = await claim_run(db_path, "good_night", "2026-10-09")

    outcome = await pipeline.generate(rubric, run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.image_ref == f"generated:{run.id}.png"
    assert outcome.alert is None
    assert (generated_dir / f"{run.id}.png").read_bytes() == PNG_BYTES
    # deterministic prompt, no LLM round: style + first ~200 chars of the post
    assert generator.prompts == [f"{IMAGE_STYLE}\n{GOOD_TEXT[:200]}"]
    assert all(len(prompt) <= 500 for prompt in generator.prompts)
    # the success event is emitted by the failover wrapper, provider-named
    event = await db.get_last_event("image_fal_ok")
    assert event is not None and "provider=fal" in event["details"]
    assert await db.get_last_event("image_none") is None


@pytest.mark.asyncio
async def test_image_failure_degrades_to_text_only_with_alert_and_event(
    db_path: Path, tmp_path: Path
) -> None:
    # fal fails with no fallback wired: text-only post + alert + event
    rubric = make_rubric(image="generate")
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    db = Database(db_path)
    alerter = MockAlerter()
    pipeline = make_pipeline(
        db_path,
        [rubric],
        MockLlm([GOOD_TEXT, "нет"]),
        images=FallbackImageGenerator(
            MockImageGenerator([ImageGenerationError("infra", "fal http 500")]),
            None,
            db=db,
            alerter=alerter,
        ),
        generated_dir=tmp_path / "generated",
        db=db,
    )

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    # a broken picture must not fail the slot
    assert outcome.status == "generated"
    assert outcome.text == GOOD_TEXT
    assert outcome.image_ref is None
    assert outcome.alert is not None and "generation failed" in outcome.alert
    event = await db.get_last_event("image_none")
    assert event is not None and "kind=infra" in event["details"]
    assert alerter.alerts == []  # infra failures stay in logs and the summary


@pytest.mark.asyncio
async def test_image_config_error_alerts_admins_once_per_day(
    db_path: Path, tmp_path: Path
) -> None:
    rubric = make_rubric(image="generate")
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    db = Database(db_path)
    alerter = MockAlerter()
    pipeline = make_pipeline(
        db_path,
        [rubric],
        MockLlm([GOOD_TEXT, "нет", GOOD_TEXT, "нет"]),
        images=FallbackImageGenerator(
            MockImageGenerator([ImageGenerationError("config", "fal http 401")]),
            None,
            db=db,
            alerter=alerter,
        ),
        generated_dir=tmp_path / "generated",
        db=db,
    )

    first = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )
    second = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-10"), now=NOW
    )

    assert first.image_ref is None and second.image_ref is None
    assert first.alert and second.alert  # both slots carry the outcome alert
    assert len(alerter.alerts) == 1  # the immediate admin alert is deduped
    assert "401" in alerter.alerts[0]
    event = await db.get_last_event("image_none")
    assert event is not None and "kind=config" in event["details"]


class _FlakyAlerter(MockAlerter):
    """Delivery double failing the first send, then recording normally."""

    def __init__(self) -> None:
        super().__init__()
        self._failures = 1

    async def send_alert(self, text: str) -> None:
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("max is unreachable")
        await super().send_alert(text)


@pytest.mark.asyncio
async def test_image_config_alert_retries_after_delivery_failure(
    db_path: Path, tmp_path: Path
) -> None:
    rubric = make_rubric(image="generate")
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    db = Database(db_path)
    alerter = _FlakyAlerter()
    pipeline = make_pipeline(
        db_path,
        [rubric],
        MockLlm([GOOD_TEXT, "нет", GOOD_TEXT, "нет"]),
        images=FallbackImageGenerator(
            MockImageGenerator(
                [
                    ImageGenerationError("config", "fal http 401"),
                    ImageGenerationError("config", "fal http 401"),
                ]
            ),
            None,
            db=db,
            alerter=alerter,
        ),
        generated_dir=tmp_path / "generated",
        db=db,
    )

    first = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )
    assert first.image_ref is None  # text-only degradation, slot survives
    assert alerter.alerts == []  # the delivery itself failed

    second = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-10"), now=NOW
    )
    assert second.image_ref is None
    # the failed delivery must not consume the daily dedup: the retry lands
    assert len(alerter.alerts) == 1
    assert "401" in alerter.alerts[0]


@pytest.mark.asyncio
async def test_fallback_generator_rescues_the_image(
    db_path: Path, tmp_path: Path
) -> None:
    """Contract for the future second provider: a wired reserve saves the
    picture, the wiring itself does not change (dual-channel plan §3.5)."""
    rubric = make_rubric(image="generate")
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    db = Database(db_path)
    generated_dir = tmp_path / "generated"
    pipeline = make_pipeline(
        db_path,
        [rubric],
        MockLlm([GOOD_TEXT, "нет"]),
        images=FallbackImageGenerator(
            MockImageGenerator([ImageGenerationError("infra", "fal http 500")]),
            MockImageGenerator([PNG_BYTES]),
            db=db,
        ),
        generated_dir=generated_dir,
        db=db,
    )
    run = await claim_run(db_path, "good_night", "2026-10-09")

    outcome = await pipeline.generate(rubric, run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.image_ref == f"generated:{run.id}.png"
    assert outcome.alert is None
    assert (generated_dir / f"{run.id}.png").read_bytes() == PNG_BYTES
    # the reserve served the call: the provider-named wrapper event
    assert await db.get_last_event("image_art_used") is not None


@pytest.mark.asyncio
async def test_fallback_post_with_image_generate(db_path: Path, tmp_path: Path) -> None:
    rubric = make_rubric(image="generate")
    await add_bank_item(db_path, "reserve", RESERVE_TEXT)
    generated_dir = tmp_path / "generated"
    pipeline = make_pipeline(
        db_path,
        [rubric],
        MockLlm(),
        images=FallbackImageGenerator(MockImageGenerator([PNG_BYTES]), None),
        generated_dir=generated_dir,
    )
    run = await claim_run(db_path, "good_night", "2026-10-09")

    outcome = await pipeline.fallback_post(rubric, run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.text == RESERVE_TEXT
    assert outcome.image_ref == f"generated:{run.id}.png"
    assert (generated_dir / f"{run.id}.png").exists()


@pytest.mark.asyncio
async def test_image_generate_without_wired_generator_degrades_with_alert(
    db_path: Path,
) -> None:
    rubric = make_rubric(image="generate")
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    pipeline = make_pipeline(db_path, [rubric], MockLlm([GOOD_TEXT, "нет"]))

    outcome = await pipeline.generate(
        rubric, await claim_run(db_path, "good_night", "2026-10-09"), now=NOW
    )

    assert outcome.status == "generated"
    assert outcome.image_ref is None
    assert outcome.alert is not None and "generation failed" in outcome.alert


# --- completion client (real HTTP against a local fake server) ---


class FakeCompletionServer:
    """Local aiohttp server impersonating the OpenAI-compatible API."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.content = GOOD_TEXT
        self.tokens = 42
        self.raw_body: str | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def completions(request: web.Request) -> web.Response:
            self.requests.append(await request.json())
            if self.raw_body is not None:
                return web.Response(
                    status=self.status, text=self.raw_body, content_type="text/plain"
                )
            return web.json_response(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": self.content}}
                    ],
                    "usage": {"total_tokens": self.tokens},
                },
                status=self.status,
            )

        app = web.Application()
        app.router.add_post("/chat/completions", completions)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses
        self._runner = runner
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def completion_server():
    server = FakeCompletionServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def _client(base_url: str) -> LlmCompletionClient:
    return LlmCompletionClient(
        base_url=base_url,
        api_key="test-api-key",
        model="test-model",
        timeout=5,
        max_tokens=500,
        temperature=0.7,
    )


@pytest.mark.asyncio
async def test_completion_client_happy_path(completion_server) -> None:
    server, base_url = completion_server

    result = await _client(base_url).complete("системный промпт", "вопрос")

    assert result.content == GOOD_TEXT
    assert result.tokens == 42
    assert result.model == "test-model"
    messages = server.requests[0]["messages"]
    assert messages[0] == {"role": "system", "content": "системный промпт"}
    assert messages[1] == {"role": "user", "content": "вопрос"}


@pytest.mark.asyncio
async def test_completion_client_billing_error_is_config(completion_server) -> None:
    server, base_url = completion_server
    server.status = 402

    with pytest.raises(LlmCompletionError) as excinfo:
        await _client(base_url).complete("sys", "usr")

    assert excinfo.value.kind == "config"


@pytest.mark.asyncio
async def test_completion_client_bad_payload_is_infra(completion_server) -> None:
    server, base_url = completion_server
    server.raw_body = "<html>gateway error</html>"

    with pytest.raises(LlmCompletionError) as excinfo:
        await _client(base_url).complete("sys", "usr")

    assert excinfo.value.kind == "infra"


# --- full chain with the slot scheduler (plan section 7) ---


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def make_scheduler(
    path: Path,
    config: RubricsConfig,
    pipeline: GenerativePipeline,
    publisher: RecordingPublisher,
    alerter: MockAlerter,
) -> SlotScheduler:
    return SlotScheduler(
        config,
        SlotRunRepository(path),
        RubricStateRepository(path),
        pipeline,
        publisher,
        alerter,
    )


@pytest.mark.asyncio
async def test_scheduler_chain_falls_back_to_bank_reserve(db_path: Path) -> None:
    rubric = make_rubric()  # on_llm_failure=bank
    await add_bank_item(db_path, "reserve", RESERVE_TEXT)
    # the LLM is down: every attempt times out
    llm = MockLlm([TimeoutError("dial timed out")])
    pipeline = make_pipeline(db_path, [rubric], llm)
    publisher = RecordingPublisher()
    alerter = MockAlerter()
    scheduler = make_scheduler(db_path, make_config([rubric]), pipeline, publisher, alerter)

    result = await scheduler.tick(now=_at(DAY, 21, 35))

    assert result.published == ("good_night",)
    run = await SlotRunRepository(db_path).get("good_night", DAY.isoformat())
    assert run is not None
    assert run.status == "published"
    assert run.text == RESERVE_TEXT  # the reserve post, not an LLM post
    assert run.attempts == 3  # 1 call + 2 regenerations
    # the whole slot cost exactly the plan's budget of LLM calls
    assert llm.call_count == 3
    assert llm.call_count <= LLM_CALLS_PER_SLOT
    assert publisher.calls[0]["text"] == RESERVE_TEXT
    assert publisher.calls[0]["image_ref"] == "postcard"
    assert alerter.alerts == []  # graceful degradation is not an anomaly


@pytest.mark.asyncio
async def test_scheduler_skip_policy_fails_slot_with_alert(db_path: Path) -> None:
    rubric = make_rubric(on_llm_failure="skip")
    llm = MockLlm([TimeoutError("dial timed out")])
    pipeline = make_pipeline(db_path, [rubric], llm)
    publisher = RecordingPublisher()
    alerter = MockAlerter()
    scheduler = make_scheduler(db_path, make_config([rubric]), pipeline, publisher, alerter)

    result = await scheduler.tick(now=_at(DAY, 21, 35))

    assert result.failed == ("good_night",)
    assert publisher.calls == []  # nothing published without a bank reserve
    run = await SlotRunRepository(db_path).get("good_night", DAY.isoformat())
    assert run is not None and run.status == "failed"
    assert llm.call_count == 3  # budget respected even on the failure path
    assert alerter.alerts and "good_night" in alerter.alerts[0]


@pytest.mark.asyncio
async def test_scheduler_publishes_checked_generative_post(db_path: Path) -> None:
    rubric = make_rubric(prompt_overlay=OVERLAY)
    await add_bank_item(db_path, "thought", SEED_THOUGHT_A)
    llm = MockLlm([GOOD_TEXT, "нет"])
    pipeline = make_pipeline(db_path, [rubric], llm)
    publisher = RecordingPublisher()
    scheduler = make_scheduler(
        db_path, make_config([rubric]), pipeline, publisher, MockAlerter()
    )

    result = await scheduler.tick(now=_at(DAY, 21, 35))

    assert result.published == ("good_night",)
    run = await SlotRunRepository(db_path).get("good_night", DAY.isoformat())
    assert run is not None and run.text == GOOD_TEXT
    assert llm.call_count == 2  # generation + safety checker
    # the anti-repeat history is in place for tomorrow's slot
    assert await SlotRunRepository(db_path).recent_texts("good_night", 5) == [GOOD_TEXT]
