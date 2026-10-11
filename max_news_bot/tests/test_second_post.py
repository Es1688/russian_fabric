"""Second post rubric tests (plan section 2, the 15:30 slot).

The rotation machinery itself lives in the bank pipeline (written by the
morning/night stage); these tests pin the combined contract of the
rubric as shipped:

- the shipped ``second_post`` block of rubrics.yaml matches the plan
  grid (15:30, mon-sat, recipe/advice/question/fact rotation, no image);
- the deterministic per-day type pick is stable across retries and
  covers all four types over a month;
- seasonality applies inside the rotation (an October recipe day never
  offers a May recipe);
- anti-repeat applies inside the rotation: a near-duplicate of a recent
  post is skipped, and a record already shown rotates to the next one;
- an exhausted rotated type fails generation with a content error and
  the plan section 7 chain ends in the reserve post.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from max_news_bot.bank.pipeline import (
    BankPipeline,
    parse_bank_source,
    rotation_type_index,
)
from max_news_bot.config.rubrics import (
    RubricConfig,
    RubricsConfig,
    TargetConfig,
    load_rubrics_config,
)
from max_news_bot.core.models import ContentBankItem, SlotRun
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    SlotRunRepository,
)

UTC = timezone.utc
PERSONA = "Ты — редактор канала для читателей старшего возраста."
# 12:00 UTC = 15:00 MSK of the same calendar day: the slot's own local
# time, so the month filter sees the day the slot belongs to.
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SLOT_DATE = "2026-10-15"

SECOND_POST_TYPES = ("recipe", "advice", "question", "fact")

# Evergreen one-liners per type; long enough for the default min_chars=1.
TEXTS = {
    "recipe": "Яблочный пирог на кефире: быстро и без заморочек.",
    "advice": "Проверьте подпоры под ветви яблонь после сбора урожая.",
    "question": "Какую книгу вы перечитывали больше одного раза?",
    "fact": "Мёд не портится годами при правильном хранении.",
}


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


def make_rubric(
    *,
    source: dict | None = None,
    history_n: int = 7,
    similarity_threshold: float = 0.7,
    min_chars: int = 1,
    max_chars: int = 4000,
) -> RubricConfig:
    return RubricConfig(
        id="second_post",
        enabled=True,
        time="15:30",
        days=("mon", "tue", "wed", "thu", "fri", "sat"),
        grace_minutes=30,
        jitter_minutes=10,
        kind="bank",
        target="main",
        source=source
        or {"bank_types": list(SECOND_POST_TYPES), "rotation": True},
        topics_by_weekday={},
        prompt_overlay=None,
        min_chars=min_chars,
        max_chars=max_chars,
        image=None,
        history_n=history_n,
        similarity_threshold=similarity_threshold,
        validators=(),
        on_llm_failure="bank",
    )


def make_config() -> RubricsConfig:
    return RubricsConfig(
        enabled=True,
        timezone="Europe/Moscow",
        persona=PERSONA,
        image_style="",
        targets={"main": TargetConfig(name="main", channel_id=-1001234567890)},
        daily_cap=6,
        weather_points=(),
        rubrics=(),
    )


def make_run(*, attempts: int = 1, slot_date: str = SLOT_DATE) -> SlotRun:
    return SlotRun(
        id=None,
        rubric_id="second_post",
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
) -> ContentBankItem:
    return ContentBankItem(
        id=None,
        item_type=item_type,
        text=text,
        review_status=review_status,
        months=months,
    )


def make_pipeline(
    bank: ContentBankRepository, runs: SlotRunRepository
) -> BankPipeline:
    return BankPipeline(make_config(), bank, runs)


def rotation_type(slot_date: str) -> str:
    return SECOND_POST_TYPES[
        rotation_type_index("second_post", slot_date, len(SECOND_POST_TYPES))
    ]


def day_with_type(item_type: str) -> date:
    """First October day whose rotation lands on ``item_type``."""
    for day in range(1, 32):
        candidate = date(2026, 10, day)
        if rotation_type(candidate.isoformat()) == item_type:
            return candidate
    raise AssertionError(f"no October day rotates to {item_type}")


async def seed_all_types(bank: ContentBankRepository) -> None:
    for item_type, text in TEXTS.items():
        await bank.add(bank_item(item_type, text))


async def publish_run(
    runs: SlotRunRepository, slot_date: str, text: str
) -> None:
    run = await runs.try_claim("second_post", slot_date)
    assert run is not None
    await runs.save(replace(run, status="published", text=text))


# --- shipped config (plan section 2) -------------------------------------------


def test_shipped_second_post_block_matches_plan_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUBRICS_ENABLED", raising=False)
    monkeypatch.delenv("RUBRICS_CONFIG_PATH", raising=False)
    monkeypatch.setenv("MAX_CHANNEL_ID", "-1001234567890")
    config = load_rubrics_config()

    rubric = next(r for r in config.rubrics if r.id == "second_post")
    assert rubric.time == "15:30"
    assert rubric.days == ("mon", "tue", "wed", "thu", "fri", "sat")
    assert "sun" not in rubric.days
    assert rubric.kind == "bank"
    assert rubric.grace_minutes == 30  # daytime grace (plan section 10)
    assert rubric.image is None  # plan section 2: no picture for this slot
    assert rubric.source is not None
    assert rubric.source["bank_types"] == list(SECOND_POST_TYPES)
    assert rubric.source["rotation"] is True
    assert rubric.history_n == 7  # plan section 3: last 5-7 posts
    assert rubric.similarity_threshold == 0.7  # plan section 3: Jaccard >= 0.7
    assert rubric.on_llm_failure == "bank"  # plan section 7 chain

    # the same block feeds the pipeline without a config error
    source = parse_bank_source(rubric)
    assert source.bank_types == SECOND_POST_TYPES
    assert source.rotation is True


# --- rotation mechanics --------------------------------------------------------


def test_rotation_covers_all_types_over_a_month() -> None:
    indices = {
        rotation_type_index("second_post", f"2026-10-{day:02d}", 4)
        for day in range(1, 32)
    }
    assert indices == {0, 1, 2, 3}


async def test_rotation_type_is_stable_across_retries(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    # The scheduler retries only after a failed attempt, and a failed
    # attempt burns no record; retries must advance the record index
    # inside the day's type, never switch the type. Two first records
    # are too short for min_chars, so attempts 1-2 fail on them.
    item_type = rotation_type(SLOT_DATE)
    others = [t for t in SECOND_POST_TYPES if t != item_type]
    short_ids = [
        await bank_repo.add(bank_item(item_type, "Коротко.")),
        await bank_repo.add(bank_item(item_type, "Тоже коротко.")),
    ]
    good_id = await bank_repo.add(
        bank_item(
            item_type,
            "Развёрнутая запись этого типа, достаточно длинная для лимита.",
        )
    )
    for other in others:
        await bank_repo.add(bank_item(other, TEXTS[other]))
    pipeline = make_pipeline(bank_repo, runs_repo)
    rubric = make_rubric(min_chars=60)

    first = await pipeline.generate(rubric, make_run(attempts=1), now=NOW)
    assert first.status == "failed"
    assert "short" in (first.error or "")
    second = await pipeline.generate(rubric, make_run(attempts=2), now=NOW)
    assert second.status == "failed"
    assert "short" in (second.error or "")

    third = await pipeline.generate(rubric, make_run(attempts=3), now=NOW)
    assert third.status == "generated"
    assert third.source_refs == (f"bank:{item_type}/{good_id}",)
    # the failed attempts burned nothing: the short records are intact
    for short_id in short_ids:
        short = await bank_repo.get(short_id)
        assert short is not None and short.show_count == 0


# --- rotation x seasonality (plan section 6) ------------------------------------


async def test_recipe_day_respects_seasonal_months(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    recipe_day = day_with_type("recipe")
    await bank_repo.add(
        bank_item("recipe", "В мае высаживайте рассаду на грядки.", months=(5,))
    )
    october_id = await bank_repo.add(
        bank_item("recipe", "В октябре укройте розы до морозов.", months=(10,))
    )
    for other in ("advice", "question", "fact"):
        await bank_repo.add(bank_item(other, TEXTS[other]))
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(
        make_rubric(), make_run(slot_date=recipe_day.isoformat()), now=NOW
    )
    assert outcome.status == "generated"
    assert outcome.text == "В октябре укройте розы до морозов."
    assert outcome.source_refs == (f"bank:recipe/{october_id}",)

    # a recipe day with no in-season recipe fails rather than leaking a
    # May recipe in October (plan section 6)
    only_may = date(2026, 11, 1)
    while rotation_type(only_may.isoformat()) != "recipe":
        only_may += timedelta(days=1)
    november = datetime(2026, 11, only_may.day, 12, 0, tzinfo=UTC)
    outcome = await pipeline.generate(
        make_rubric(), make_run(slot_date=only_may.isoformat()), now=november
    )
    assert outcome.status == "failed"
    assert outcome.error_kind == "content"


# --- rotation x anti-repeat (plan section 3) ------------------------------------


async def test_recipe_day_skips_jaccard_duplicates_of_recent_posts(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    recipe_day = day_with_type("recipe")
    near_duplicate = "Яблочный пирог на кефире: быстро и без хлопот"
    await bank_repo.add(bank_item("recipe", near_duplicate))
    different_id = await bank_repo.add(
        bank_item("recipe", "Творожная запеканка с изюмом в духовке.")
    )
    for other in ("advice", "question", "fact"):
        await bank_repo.add(bank_item(other, TEXTS[other]))
    # yesterday's published post is a near-duplicate of the first recipe
    await publish_run(
        runs_repo,
        (recipe_day - timedelta(days=1)).isoformat(),
        "Яблочный пирог на кефире: быстро и без заморочек",
    )
    pipeline = make_pipeline(bank_repo, runs_repo)

    outcome = await pipeline.generate(
        make_rubric(), make_run(slot_date=recipe_day.isoformat()), now=NOW
    )
    assert outcome.status == "generated"
    assert outcome.text == "Творожная запеканка с изюмом в духовке."
    assert outcome.source_refs == (f"bank:recipe/{different_id}",)


async def test_same_type_days_do_not_repeat_the_shown_record(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    first_day = day_with_type("recipe")
    second_day = first_day + timedelta(days=1)
    while rotation_type(second_day.isoformat()) != "recipe":
        second_day += timedelta(days=1)
    first_id = await bank_repo.add(
        bank_item("recipe", "Яблочный пирог на кефире: быстро и без заморочек.")
    )
    second_id = await bank_repo.add(
        bank_item("recipe", "Творожная запеканка с изюмом в духовке.")
    )
    for other in ("advice", "question", "fact"):
        await bank_repo.add(bank_item(other, TEXTS[other]))
    pipeline = make_pipeline(bank_repo, runs_repo)

    first = await pipeline.generate(
        make_rubric(), make_run(slot_date=first_day.isoformat()), now=NOW
    )
    assert first.status == "generated"
    assert first.source_refs == (f"bank:recipe/{first_id}",)

    # the next recipe day takes the least-shown record, not the same one
    second = await pipeline.generate(
        make_rubric(),
        make_run(slot_date=second_day.isoformat()),
        now=datetime(2026, 10, second_day.day, 12, 0, tzinfo=UTC),
    )
    assert second.status == "generated"
    assert second.source_refs == (f"bank:recipe/{second_id}",)
    shown = await bank_repo.get(first_id)
    assert shown is not None and shown.show_count == 1


# --- degradation: exhausted type -> reserve post (plan section 7) ---------------


async def test_exhausted_rotated_type_falls_to_reserve_post(
    bank_repo: ContentBankRepository, runs_repo: SlotRunRepository
) -> None:
    question_day = day_with_type("question")
    # no questions at all; only recipes (not a reserve type) plus the
    # fact reserve the fallback chain draws from
    await bank_repo.add(bank_item("recipe", TEXTS["recipe"]))
    fact_id = await bank_repo.add(bank_item("fact", TEXTS["fact"]))
    pipeline = make_pipeline(bank_repo, runs_repo)
    rubric = make_rubric()

    outcome = await pipeline.generate(
        rubric, make_run(slot_date=question_day.isoformat()), now=NOW
    )
    assert outcome.status == "failed"
    assert outcome.error_kind == "content"
    assert "question" in (outcome.error or "")

    # the scheduler tail of the plan section 7 chain: on_llm_failure=bank
    # (slot_scheduler._execute) calls fallback_post after the attempts
    fallback = await pipeline.fallback_post(
        rubric, make_run(attempts=3, slot_date=question_day.isoformat()), now=NOW
    )
    assert fallback.status == "generated"
    assert fallback.source_refs == (f"bank:fact/{fact_id}",)
