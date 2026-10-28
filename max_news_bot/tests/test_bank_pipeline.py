"""Content bank pipeline tests (plan sections 3 and 6).

Selection with seasonality and event_date, show-counter updates, stock
alerts with the reserve type first, LLM-only-formatting with the strict
validator, and the anti-repeat pair (prompt history + Jaccard dedup).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from max_news_bot.bank.antirepeat import (
    is_similar_to_recent,
    least_recently_shown_first,
    recent_posts_section,
)
from max_news_bot.bank.pipeline import (
    BANK_STOCK_ALERT_DAYS,
    BankPipeline,
    BankSourceError,
    build_system_prompt,
    build_user_prompt,
    low_stock_types,
    parse_bank_source,
    rotation_type_index,
)
from max_news_bot.config.rubrics import RubricConfig, RubricsConfig, TargetConfig
from max_news_bot.tests.conftest import make_llm_channels
from max_news_bot.core.models import ContentBankItem, SlotRun
from max_news_bot.images.base import ImageGenerationError
from max_news_bot.images.fallback import FallbackImageGenerator
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    SlotRunRepository,
)
from max_news_bot.writers.completion import (
    CompletionResult,
    LlmCompletionError,
)

UTC = timezone.utc
PERSONA = "Ты — редактор канала для читателей старшего возраста."
# 07:00 UTC = 10:00 MSK on the same calendar day: October, event 10-15.
NOW = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
MAY_NOW = datetime(2026, 5, 20, 7, 0, tzinfo=UTC)
SLOT_DATE = "2026-10-15"

_RECORD_1 = "27 мая 1703 года Пётр I заложил Петропавловскую крепость."
# Same facts re-worded: numbers and names intact, nothing new ("Этот" only
# opens the sentence).
_GOOD_LLM_OUTPUT = (
    "Этот день в истории: 27 мая 1703 года Пётр I заложил Петропавловскую "
    "крепость."
)


@pytest.fixture
async def repo_path(tmp_path: Path) -> Path:
    path = tmp_path / "max_news_bot.db"
    await Database(path).initialize()
    return path


@pytest.fixture
def bank_repo(repo_path: Path) -> ContentBankRepository:
    return ContentBankRepository(repo_path)


@pytest.fixture
def runs_repo(repo_path: Path) -> SlotRunRepository:
    return SlotRunRepository(repo_path)


@pytest.fixture
def db_repo(repo_path: Path) -> Database:
    return Database(repo_path)


def make_rubric(
    rubric_id: str = "good_morning",
    *,
    kind: str = "bank",
    source: dict | None = None,
    history_n: int = 5,
    similarity_threshold: float = 0.7,
    min_chars: int = 1,
    max_chars: int = 4000,
    prompt_overlay: str | None = None,
    image: str | None = None,
) -> RubricConfig:
    return RubricConfig(
        id=rubric_id,
        enabled=True,
        time="07:00",
        days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
        grace_minutes=60,
        jitter_minutes=0,
        kind=kind,
        target="main",
        source=source,
        topics_by_weekday={},
        prompt_overlay=prompt_overlay,
        min_chars=min_chars,
        max_chars=max_chars,
        image=image,
        history_n=history_n,
        similarity_threshold=similarity_threshold,
        validators=(),
        on_llm_failure="bank",
    )


def make_config(image_style: str = "") -> RubricsConfig:
    return RubricsConfig(
        llm=make_llm_channels(),
        timezone="Europe/Moscow",
        persona=PERSONA,
        image_style=image_style,
        targets={"main": TargetConfig(name="main", channel_id=-1001234567890)},
        daily_cap=6,
        weather_points=(),
        rubrics=(),
    )


def make_run(rubric_id: str, *, attempts: int = 1, slot_date: str = SLOT_DATE) -> SlotRun:
    return SlotRun(
        id=None,
        rubric_id=rubric_id,
        slot_date=slot_date,
        status="pending",
        attempts=attempts,
    )


def bank_item(
    item_type: str,
    text: str,
    *,
    review_status: str = "approved",
    months: tuple[int, ...] = (),
    event_date: str | None = None,
) -> ContentBankItem:
    return ContentBankItem(
        id=None,
        item_type=item_type,
        text=text,
        review_status=review_status,
        months=months,
        event_date=event_date,
    )


async def seed(bank: ContentBankRepository, *items: ContentBankItem) -> None:
    for item in items:
        await bank.add(item)


async def publish_run(
    runs: SlotRunRepository, rubric_id: str, slot_date: str, text: str
) -> None:
    run = await runs.try_claim(rubric_id, slot_date)
    assert run is not None
    await runs.save(replace(run, status="published", text=text))


async def claim_run(
    runs: SlotRunRepository, rubric_id: str, slot_date: str = SLOT_DATE
) -> SlotRun:
    """A claimed run carries the repository id the generated file is
    named after (generated images need a real slot_run_id)."""
    run = await runs.try_claim(rubric_id, slot_date)
    assert run is not None
    return run


class FakeCompletion:
    """LlmCompletion double: scripted outputs / error, every call recorded."""

    def __init__(
        self, outputs: list[str] | None = None, error: LlmCompletionError | None = None
    ) -> None:
        self._outputs = list(outputs or [])
        self._error = error
        self.calls: list[dict[str, str]] = []

    async def complete(self, system: str, user: str) -> CompletionResult:
        self.calls.append({"system": system, "user": user})
        if self._error is not None:
            raise self._error
        assert self._outputs, "no scripted output"
        if len(self._outputs) > 1:
            content = self._outputs.pop(0)
        else:
            content = self._outputs[0]
        return CompletionResult(content=content, tokens=10, model="test-model")


PNG_BYTES = b"\x89PNG\r\n\x1a\n fake generated image bytes"


class MockImageGenerator:
    """ImageGenerator double: scripted bytes / errors, prompts recorded."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> bytes:
        self.prompts.append(prompt)
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        return item


def make_pipeline(
    bank: ContentBankRepository,
    runs: SlotRunRepository,
    completion: FakeCompletion | None = None,
    *,
    image_style: str = "",
    images: FallbackImageGenerator | None = None,
    generated_dir: Path | None = None,
    db: Database | None = None,
) -> BankPipeline:
    return BankPipeline(
        make_config(image_style),
        bank,
        runs,
        completion,
        images=images,
        generated_dir=generated_dir,
        db=db,
    )


# --- source parsing -----------------------------------------------------------


def test_parse_bank_source_requires_types_for_bank_kind() -> None:
    with pytest.raises(BankSourceError, match="bank_types"):
        parse_bank_source(make_rubric(source={}))


def test_parse_bank_source_rejects_event_types_outside_bank_types() -> None:
    with pytest.raises(BankSourceError, match="event_types"):
        parse_bank_source(
            make_rubric(source={"bank_types": ["fact"], "event_types": ["recipe"]})
        )


def test_parse_bank_source_rejects_non_bool_use_llm() -> None:
    with pytest.raises(BankSourceError, match="use_llm"):
        parse_bank_source(make_rubric(source={"bank_types": ["fact"], "use_llm": "yes"}))


def test_parse_bank_source_tolerates_foreign_keys() -> None:
    # digest rubrics keep feeds_file in the same dict; fallback_post of a
    # non-bank rubric must still parse
    source = parse_bank_source(
        make_rubric(kind="digest", source={"feeds_file": "news_sources.yaml"})
    )
    assert source.bank_types == ()
    assert source.fallback_types == ("fact", "backup")
    assert source.use_llm is False


# --- selection: seasonality, event_date, rotation -----------------------------


async def test_generate_marks_shown_and_returns_raw_text(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["thought"]})
    first = await bank_repo.add(
        bank_item("thought", "Тише едешь — дальше будешь: спешка в деле лишняя.")
    )
    second = await bank_repo.add(bank_item("thought", "Семь раз отмерь, один раз отрежь."))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)

    assert outcome.status == "generated"
    assert outcome.text == "Тише едешь — дальше будешь: спешка в деле лишняя."
    assert outcome.source_refs == (f"bank:thought/{first}",)

    shown = await bank_repo.get(first)
    assert shown is not None and shown.show_count == 1
    assert shown.last_shown_at is not None
    untouched = await bank_repo.get(second)
    assert untouched is not None and untouched.show_count == 0


async def test_generate_respects_seasonal_months(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["advice"]})
    await seed(
        bank_repo,
        bank_item("advice", "В мае высаживайте рассаду на грядки.", months=(5,)),
        bank_item("advice", "В октябре укройте розы до морозов.", months=(10,)),
        bank_item("advice", "Проверьте срок годности домашних запасов."),
    )
    pipeline = make_pipeline(bank_repo, runs_repo)

    october = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert october.text == "В октябре укройте розы до морозов."

    # in May the may-advice ranks ahead of the evergreen one (id order)
    may = await pipeline.generate(rubric, make_run(rubric.id, slot_date="2026-05-20"), now=MAY_NOW)
    assert may.text == "В мае высаживайте рассаду на грядки."


async def test_generate_event_date_facts(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(
        source={"bank_types": ["fact"], "event_types": ["fact"]},
    )
    await seed(
        bank_repo,
        bank_item(
            "fact",
            "27 мая 1703 года основана Петропавловская крепость.",
            event_date="05-27",
        ),
        bank_item(
            "fact",
            "15 октября 1964 года Хрущёв отправлен на пенсию.",
            event_date="10-15",
        ),
    )
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "generated"
    assert "15 октября" in outcome.text

    # a day with no matching fact fails: the chain falls back to a reserve post
    no_match = await pipeline.generate(
        rubric, make_run(rubric.id, slot_date="2026-10-16"),
        now=NOW + timedelta(days=1),
    )
    assert no_match.status == "failed"
    assert no_match.error_kind == "content"


async def test_generate_rotation_picks_one_type_per_day(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(
        rubric_id="second_post",
        source={
            "bank_types": ["recipe", "advice", "question", "fact"],
            "rotation": True,
        },
    )
    texts = {
        "recipe": "Яблочный пирог на кефире: быстро и без заморочек.",
        "advice": "Проверьте подпоры под ветви яблонь после сбора урожая.",
        "question": "Какую книгу вы перечитывали больше одного раза?",
        "fact": "Мёд не портится годами при правильном хранении.",
    }
    for item_type, text in texts.items():
        await bank_repo.add(bank_item(item_type, text))
    pipeline = make_pipeline(bank_repo, runs_repo)

    # two dates whose deterministic rotation indices differ
    dates = [date(2026, 10, day) for day in range(1, 32)]
    day_a, day_b = dates[0], next(
        d
        for d in dates[1:]
        if rotation_type_index(rubric.id, d.isoformat(), 4)
        != rotation_type_index(rubric.id, dates[0].isoformat(), 4)
    )
    for day in (day_a, day_b):
        expected_type = rubric.source["bank_types"][
            rotation_type_index(rubric.id, day.isoformat(), 4)
        ]
        outcome = await pipeline.generate(
            rubric, make_run(rubric.id, slot_date=day.isoformat()), now=NOW
        )
        assert outcome.status == "generated"
        assert outcome.text == texts[expected_type]
        assert outcome.source_refs[0].startswith(f"bank:{expected_type}/")


async def test_generate_least_shown_first(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["phrase"]})
    often = await bank_repo.add(bank_item("phrase", "Доброе утро, и пусть день будет добрым."))
    fresh = await bank_repo.add(bank_item("phrase", "Пусть утро начнётся с тёплой чашки чая."))
    # often has been shown twice already
    await bank_repo.mark_shown(often)
    await bank_repo.mark_shown(often)
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.text == "Пусть утро начнётся с тёплой чашки чая."
    assert outcome.source_refs == (f"bank:phrase/{fresh}",)


async def test_generate_skips_unapproved_items(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["fact"]})
    await seed(bank_repo, bank_item("fact", "Черновой факт.", review_status="pending"))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "failed"


# --- anti-repeat (plan section 3) ----------------------------------------------


def test_recent_posts_section_lists_history_in_order() -> None:
    section = recent_posts_section(["Первый пост.", "Второй пост."])
    assert section.splitlines()[0].startswith("Недавние посты рубрики")
    assert "1. Первый пост." in section
    assert "2. Второй пост." in section
    assert recent_posts_section([]) == ""


def test_is_similar_uses_rubric_threshold() -> None:
    history = "Полейте рассаду тёплой водой и подкормите зольным настоем утром"
    near_duplicate = "Полейте рассаду тёплой водой и подкормите зольным настоем вечером"
    different = "Проверьте подпоры под ветви яблонь после сбора урожая"
    assert is_similar_to_recent(near_duplicate, [history], threshold=0.7)
    assert not is_similar_to_recent(different, [history], threshold=0.7)


def test_least_recently_shown_first_orders_by_counters() -> None:
    shown_once = ContentBankItem(
        id=1, item_type="fact", text="a", show_count=1,
        last_shown_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    never = ContentBankItem(id=2, item_type="fact", text="b")
    shown_twice_old = ContentBankItem(
        id=3, item_type="fact", text="c", show_count=1,
        last_shown_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    ordered = least_recently_shown_first([shown_once, never, shown_twice_old])
    assert [item.id for item in ordered] == [2, 3, 1]


async def test_generate_skips_jaccard_duplicates_of_recent_posts(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["advice"]})
    await seed(
        bank_repo,
        bank_item(
            "advice",
            "Полейте рассаду тёплой водой и подкормите зольным настоем вечером",
        ),
        bank_item("advice", "Проверьте подпоры под ветви яблонь после сбора урожая"),
    )
    await publish_run(
        runs_repo,
        rubric.id,
        "2026-10-14",
        "Полейте рассаду тёплой водой и подкормите зольным настоем утром",
    )
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "generated"
    assert outcome.text == "Проверьте подпоры под ветви яблонь после сбора урожая"


# --- LLM only formats (plan section 6) ------------------------------------------


async def test_generate_llm_formats_record_with_history_in_prompt(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(
        source={"bank_types": ["fact"], "use_llm": True},
        prompt_overlay="Заверши пост коротким напутствием.",
    )
    await seed(bank_repo, bank_item("fact", _RECORD_1))
    await publish_run(runs_repo, rubric.id, "2026-10-14", "Вчерашний пост рубрики.")
    completion = FakeCompletion(outputs=[_GOOD_LLM_OUTPUT])
    pipeline = make_pipeline(bank_repo, runs_repo, completion)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)

    assert outcome.status == "generated"
    assert outcome.text == _GOOD_LLM_OUTPUT
    assert len(completion.calls) == 1
    system, user = completion.calls[0]["system"], completion.calls[0]["user"]
    assert PERSONA in system
    assert "Заверши пост коротким напутствием." in system
    assert "<record>\n" + _RECORD_1 + "\n</record>" in user
    assert "Вчерашний пост рубрики." in user  # anti-repeat history (plan §3)


async def test_generate_llm_rejected_output_burns_no_item_then_next_candidate(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["fact"], "use_llm": True})
    first = await bank_repo.add(bank_item("fact", _RECORD_1))
    second = await bank_repo.add(
        bank_item("fact", "В 1860 году открылась Петербурская консерватория.")
    )
    bad_output = _GOOD_LLM_OUTPUT + " Сейчас идёт 323-й год."  # new number
    good_output = (
        "Этот день в истории: в 1860 году открылась Петербурская консерватория."
    )
    completion = FakeCompletion(outputs=[bad_output, good_output])
    pipeline = make_pipeline(bank_repo, runs_repo, completion)

    first_attempt = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert first_attempt.status == "failed"
    assert first_attempt.error_kind == "content"
    assert "323" in (first_attempt.error or "")
    untouched = await bank_repo.get(first)
    assert untouched is not None and untouched.show_count == 0

    # the scheduler bumps attempts before each retry -> next candidate
    retry = await pipeline.generate(rubric, make_run(rubric.id, attempts=2), now=NOW)
    assert retry.status == "generated"
    assert retry.text == good_output
    spent = await bank_repo.get(second)
    assert spent is not None and spent.show_count == 1


async def test_generate_llm_infra_and_config_errors(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["fact"], "use_llm": True})
    await seed(bank_repo, bank_item("fact", _RECORD_1))

    infra = make_pipeline(
        bank_repo, runs_repo, FakeCompletion(error=LlmCompletionError("infra", "llm http 500"))
    )
    outcome = await infra.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "failed"
    assert outcome.error_kind == "infra"

    config = make_pipeline(
        bank_repo,
        runs_repo,
        FakeCompletion(error=LlmCompletionError("config", "llm http 402")),
    )
    outcome = await config.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "failed"
    assert outcome.error_kind == "config"


async def test_generate_use_llm_without_completion_is_config_error(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["fact"], "use_llm": True})
    await seed(bank_repo, bank_item("fact", _RECORD_1))
    pipeline = make_pipeline(bank_repo, runs_repo)  # no completion wired

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "failed"
    assert outcome.error_kind == "config"


async def test_generate_length_rules_apply_to_bank_text(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["phrase"]}, min_chars=100)
    item_id = await bank_repo.add(bank_item("phrase", "Слишком коротко."))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "failed"
    assert outcome.error_kind == "content"
    assert "short" in (outcome.error or "")
    untouched = await bank_repo.get(item_id)
    assert untouched is not None and untouched.show_count == 0


# --- fallback: reserve posts (plan section 7) -----------------------------------


async def test_fallback_post_uses_reserve_types_without_season_filter(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(kind="generative", source={"bank_types": ["thought"]})
    may_fact = await bank_repo.add(
        bank_item("fact", "В мае зацветает сирень под окном.", months=(5,))
    )
    await seed(bank_repo, bank_item("recipe", "Не резервный тип."))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.fallback_post(rubric, make_run(rubric.id, attempts=3), now=NOW)

    # October, but a reserve post must work on any day (plan section 7)
    assert outcome.status == "generated"
    assert outcome.source_refs == (f"bank:fact/{may_fact}",)
    assert outcome.text == "В мае зацветает сирень под окном."


async def test_fallback_post_prefers_backup_when_no_facts(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["fact"]})
    backup = await bank_repo.add(
        bank_item(
            "backup",
            "Тёплый резервный пост: хорошая книга и чашка чая сделают вечер.",
        )
    )
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.fallback_post(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "generated"
    assert outcome.source_refs == (f"bank:backup/{backup}",)


async def test_fallback_post_empty_reserve_fails(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["fact"], "fallback_types": ["backup"]})
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.fallback_post(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "failed"
    assert "reserve bank empty" in (outcome.error or "")


async def test_generate_empty_bank_fails(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["thought"]})
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "failed"
    assert outcome.error_kind == "content"


# --- postcards: image_ref of the slot run (plan section 12) ---------------------


def card_item(
    text: str, image: str | None = "morning.jpg", show_count: int = 0
) -> ContentBankItem:
    return ContentBankItem(
        id=None,
        item_type="postcard",
        text=text,
        metadata={"image": image} if image else {},
        review_status="approved",
        show_count=show_count,
    )


async def test_generate_without_rubric_image_has_no_image_ref(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["postcard"]})
    card_id = await bank_repo.add(card_item("Доброе утро! Солнечный день."))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)

    assert outcome.status == "generated"
    assert outcome.image_ref is None
    assert outcome.source_refs == (f"bank:postcard/{card_id}",)


async def test_postcard_text_item_is_its_own_image_ref(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    # the selected record carries an image: text and card come from it
    rubric = make_rubric(source={"bank_types": ["postcard"]}, image="postcard")
    card_id = await bank_repo.add(card_item("Доброе утро! Пусть день будет тёплым."))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)

    assert outcome.status == "generated"
    assert outcome.image_ref == f"bank:postcard/{card_id}"
    # one record = one show bump, not two
    item = await bank_repo.get(card_id)
    assert item is not None and item.show_count == 1


async def test_phrase_text_gets_a_postcard_from_the_named_type(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    # morning grid: bank_types [phrase, postcard], the phrase has no image
    rubric = make_rubric(
        source={"bank_types": ["phrase", "postcard"]}, image="postcard"
    )
    await bank_repo.add(bank_item("phrase", "Утро добрым делает не кофе, а покой."))
    picked = await bank_repo.add(card_item("Утренняя открытка", show_count=0))
    skipped = await bank_repo.add(card_item("Запасная открытка", show_count=2))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)

    assert outcome.status == "generated"
    # least-shown card of the rubric.image type, rotated by show counter
    assert outcome.image_ref == f"bank:postcard/{picked}"
    card = await bank_repo.get(picked)
    assert card is not None and card.show_count == 1
    reserve = await bank_repo.get(skipped)
    assert reserve is not None and reserve.show_count == 2


async def test_empty_postcard_stock_degrades_to_text_only(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["phrase"]}, image="postcard")
    await bank_repo.add(bank_item("phrase", "Тише едешь — дальше будешь: спешка лишняя."))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)

    # a missing card must not fail the 07:00 slot
    assert outcome.status == "generated"
    assert outcome.image_ref is None
    assert outcome.text is not None


# --- generated images: image: generate (dual-channel plan §3.3) -----------------


async def test_image_generate_writes_file_and_generated_ref(
    bank_repo: ContentBankRepository,
    runs_repo: SlotRunRepository,
    db_repo: Database,
    tmp_path: Path,
) -> None:
    rubric = make_rubric(source={"bank_types": ["phrase"]}, image="generate")
    await seed(bank_repo, bank_item("phrase", "Тише едешь — дальше будешь."))
    generated_dir = tmp_path / "generated"
    generator = MockImageGenerator([PNG_BYTES])
    pipeline = make_pipeline(
        bank_repo,
        runs_repo,
        images=FallbackImageGenerator(generator, None),
        generated_dir=generated_dir,
        db=db_repo,
    )
    run = await claim_run(runs_repo, rubric.id)

    outcome = await pipeline.generate(rubric, run, now=NOW)

    assert outcome.status == "generated"
    assert outcome.image_ref == f"generated:{run.id}.png"
    # the thin bank's stock alert may fire, but never an image failure
    assert outcome.alert is None or "generation failed" not in outcome.alert
    assert (generated_dir / f"{run.id}.png").read_bytes() == PNG_BYTES
    event = await db_repo.get_last_event("image_fal_ok")
    assert event is not None
    assert f"run={run.id}" in event["details"]
    assert await db_repo.get_last_event("image_none") is None


async def test_image_generation_failure_degrades_to_text_only(
    bank_repo: ContentBankRepository,
    runs_repo: SlotRunRepository,
    db_repo: Database,
    tmp_path: Path,
) -> None:
    # fal fails with no fallback wired: text-only post + alert + event
    rubric = make_rubric(source={"bank_types": ["phrase"]}, image="generate")
    await seed(bank_repo, bank_item("phrase", "Тише едешь — дальше будешь."))
    generated_dir = tmp_path / "generated"
    generator = MockImageGenerator([ImageGenerationError("infra", "fal http 500")])
    pipeline = make_pipeline(
        bank_repo,
        runs_repo,
        images=FallbackImageGenerator(generator, None),
        generated_dir=generated_dir,
        db=db_repo,
    )
    run = await claim_run(runs_repo, rubric.id)

    outcome = await pipeline.generate(rubric, run, now=NOW)

    # a broken picture must not fail the 07:00 slot
    assert outcome.status == "generated"
    assert outcome.text is not None
    assert outcome.image_ref is None
    assert outcome.alert is not None and "generation failed" in outcome.alert
    assert not generated_dir.exists()
    event = await db_repo.get_last_event("image_none")
    assert event is not None and "kind=infra" in event["details"]


# --- stock alerts (plan section 6) ----------------------------------------------


def test_low_stock_types_prioritizes_reserve() -> None:
    counts = {"thought": 40, "fact": 10, "backup": 29}
    assert low_stock_types(counts) == ("backup", "fact")
    assert low_stock_types({"thought": 40, "backup": BANK_STOCK_ALERT_DAYS}) == ()


async def test_generate_alerts_when_stock_below_watermark(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    rubric = make_rubric(source={"bank_types": ["thought"]})
    # thoughts and facts are fine, the shared reserve is not
    await seed(
        bank_repo,
        *[bank_item("thought", f"Мысль номер {number}." ) for number in range(40)],
        *[bank_item("fact", f"Факт номер {number}.") for number in range(35)],
        *[bank_item("backup", f"Резерв номер {number}.") for number in range(29)],
    )
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(rubric, make_run(rubric.id), now=NOW)
    assert outcome.status == "generated"
    assert outcome.alert is not None
    assert "backup=29" in outcome.alert
    assert "thought" not in outcome.alert  # above the watermark, not reported
    assert str(BANK_STOCK_ALERT_DAYS) in outcome.alert


# --- prompt building --------------------------------------------------------------


def test_build_system_prompt_joins_persona_rules_overlay() -> None:
    system = build_system_prompt(PERSONA, "Дополнительное указание.")
    assert system.startswith(PERSONA)
    assert "Правила оформления записи банка" in system
    assert system.endswith("Дополнительное указание.")
    assert build_system_prompt("", None) == build_system_prompt(" ", None)


def test_build_user_prompt_wraps_record_and_appends_history() -> None:
    user = build_user_prompt("Текст записи.", ["Прошлый пост."])
    assert "<record>\nТекст записи.\n</record>" in user
    assert "Прошлый пост." in user
    assert "Недавние посты" not in build_user_prompt("Текст записи.", [])
