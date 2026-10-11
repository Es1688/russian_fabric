from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from max_news_bot.core.models import (
    ContentBankItem,
    FilterLogEntry,
    NewsItem,
    RubricState,
    SlotRun,
)
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    ContentBankRepository,
    DigestItemsRepository,
    FilterLogRepository,
    RubricStateRepository,
    SlotRunRepository,
)


@pytest.fixture
async def repo_path(tmp_path: Path) -> Path:
    path = tmp_path / "max_news_bot.db"
    await Database(path).initialize()
    return path


# --- slot_runs ---


@pytest.mark.asyncio
async def test_try_claim_creates_pending_run(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)

    run = await repo.try_claim("good_morning", "2026-10-09")

    assert run is not None
    assert run.id is not None
    assert run.rubric_id == "good_morning"
    assert run.slot_date == "2026-10-09"
    assert run.status == "pending"
    assert run.attempts == 0
    assert run.is_dry_run is False
    assert run.target is None
    assert run.text is None
    assert run.source_refs == ()
    assert run.created_at is not None
    assert run.updated_at is not None


@pytest.mark.asyncio
async def test_try_claim_second_same_day_returns_none(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)

    first = await repo.try_claim("good_morning", "2026-10-09")
    second = await repo.try_claim("good_morning", "2026-10-09")

    assert first is not None
    assert second is None  # UNIQUE(rubric_id, slot_date)

    other_day = await repo.try_claim("good_morning", "2026-10-10")
    assert other_day is not None


@pytest.mark.asyncio
async def test_slot_run_round_trip(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)
    claimed = await repo.try_claim(
        "night",
        "2026-10-09",
        target="-1001234567890",
        is_dry_run=True,
    )
    assert claimed is not None

    published = replace(
        claimed,
        status="published",
        attempts=2,
        text="Спокойной ночи. Берегите себя.",
        image_ref="postcards/night_07.png",
        source_refs=("bank:thought/42",),
        max_message_id="mid.4242",
        published_at=datetime(2026, 10, 9, 18, 30, tzinfo=timezone.utc),
    )
    await repo.save(published)

    loaded = await repo.get("night", "2026-10-09")
    assert loaded is not None
    assert loaded == replace(
        published,
        # updated_at is stamped by the storage on save
        updated_at=loaded.updated_at,
    )
    assert loaded.updated_at is not None
    # published_at survives the UTC round trip
    assert loaded.published_at == datetime(
        2026, 10, 9, 18, 30, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_slot_run_skipped_and_failed_round_trip(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)

    skipped = await repo.try_claim("weather", "2026-10-09")
    assert skipped is not None
    await repo.save(replace(skipped, status="skipped", skip_reason="missed_grace"))
    loaded = await repo.get("weather", "2026-10-09")
    assert loaded is not None
    assert loaded.status == "skipped"
    assert loaded.skip_reason == "missed_grace"

    failed = await repo.try_claim("topic", "2026-10-09")
    assert failed is not None
    await repo.save(
        replace(failed, status="failed", attempts=3, error="E" * 500)
    )
    loaded = await repo.get("topic", "2026-10-09")
    assert loaded is not None
    assert loaded.status == "failed"
    assert loaded.attempts == 3
    assert loaded.error is not None
    assert len(loaded.error) == 300  # truncated like posts.error


@pytest.mark.asyncio
async def test_recent_texts_last_published_oldest_first(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)

    async def publish(rubric_id: str, slot_date: str, text: str | None) -> None:
        run = await repo.try_claim(rubric_id, slot_date)
        assert run is not None
        await repo.save(replace(run, status="published", text=text))

    await publish("good_night", "2026-10-08", "Первый вечерний пост.")
    await publish("good_night", "2026-10-10", "Самый свежий пост.")
    await publish("good_night", "2026-10-09", "Второй вечерний пост.")
    # other rubrics, rows without text and non-published rows must not leak in
    await publish("good_morning", "2026-10-10", "Чужая рубрика.")
    await publish("good_night", "2026-10-07", None)
    failed = await repo.try_claim("good_night", "2026-10-11")
    await repo.save(replace(failed, status="failed", text="Не вышло."))

    texts = await repo.recent_texts("good_night", 5)

    assert texts == [
        "Первый вечерний пост.",
        "Второй вечерний пост.",
        "Самый свежий пост.",
    ]
    assert await repo.recent_texts("good_night", 2) == [
        "Второй вечерний пост.",
        "Самый свежий пост.",
    ]


@pytest.mark.asyncio
async def test_slot_run_source_refs_stored_as_json(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)
    claimed = await repo.try_claim("digest", "2026-10-09")
    assert claimed is not None

    await repo.save(
        replace(claimed, status="generated", source_refs=("https://a", "https://b"))
    )

    with sqlite3.connect(repo_path) as conn:
        stored = conn.execute(
            "SELECT source_refs FROM slot_runs WHERE id = ?", (claimed.id,)
        ).fetchone()[0]
    assert json.loads(stored) == ["https://a", "https://b"]

    loaded = await repo.get("digest", "2026-10-09")
    assert loaded is not None
    assert loaded.source_refs == ("https://a", "https://b")

    # empty refs round-trip to an empty tuple
    await repo.save(replace(claimed, source_refs=()))
    loaded = await repo.get("digest", "2026-10-09")
    assert loaded is not None
    assert loaded.source_refs == ()


@pytest.mark.asyncio
async def test_save_requires_claimed_run(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)
    orphan = SlotRun(
        id=None, rubric_id="orphan", slot_date="2026-10-09", status="pending"
    )
    with pytest.raises(ValueError, match="try_claim"):
        await repo.save(orphan)


@pytest.mark.asyncio
async def test_find_by_status_newest_first(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)
    for day in ("2026-10-07", "2026-10-08", "2026-10-09"):
        claimed = await repo.try_claim("digest", day)
        assert claimed is not None
        await repo.save(replace(claimed, status="generated"))
    await repo.try_claim("weather", "2026-10-09")

    runs = await repo.find_by_status("generated")

    assert [run.slot_date for run in runs] == [
        "2026-10-09",
        "2026-10-08",
        "2026-10-07",
    ]
    limited = await repo.find_by_status("generated", limit=2)
    assert len(limited) == 2


@pytest.mark.asyncio
async def test_count_for_date_with_statuses(repo_path: Path) -> None:
    # the scheduler's daily_cap counts runs that did or may still produce
    # a post; skipped/failed published nothing and stay free
    repo = SlotRunRepository(repo_path)
    cap_statuses = ("pending", "generated", "publishing", "published")
    for rubric_id, status in (
        ("morning", "published"),
        ("weather", "skipped"),
        ("topic", "failed"),
        ("night", "pending"),
    ):
        claimed = await repo.try_claim(rubric_id, "2026-10-09")
        assert claimed is not None
        await repo.save(replace(claimed, status=status))
    other_day = await repo.try_claim("morning", "2026-10-10")
    assert other_day is not None
    await repo.save(replace(other_day, status="published"))

    assert await repo.count_for_date("2026-10-09") == 4
    assert await repo.count_for_date("2026-10-09", statuses=cap_statuses) == 2
    assert await repo.count_for_date("2026-10-11") == 0


@pytest.mark.asyncio
async def test_count_for_date_dry_run_filter(repo_path: Path) -> None:
    # daily_cap limits channel posts (plan section 2): dry-run preview runs
    # (shakedown, plan section 14) go to the admins and must not consume it
    repo = SlotRunRepository(repo_path)
    for rubric_id in ("morning", "weather", "topic", "night"):
        claimed = await repo.try_claim(rubric_id, "2026-10-09", is_dry_run=True)
        assert claimed is not None
        await repo.save(replace(claimed, status="published"))
    live = await repo.try_claim("good_news", "2026-10-09")
    assert live is not None
    await repo.save(replace(live, status="published"))

    assert await repo.count_for_date("2026-10-09") == 5
    assert await repo.count_for_date("2026-10-09", dry_run=False) == 1
    assert await repo.count_for_date("2026-10-09", dry_run=True) == 4


# --- digest_items ---


@pytest.mark.asyncio
async def test_digest_items_round_trip(repo_path: Path) -> None:
    runs = SlotRunRepository(repo_path)
    repo = DigestItemsRepository(repo_path)
    claimed = await runs.try_claim("digest", "2026-10-09")
    assert claimed is not None

    items = [
        NewsItem(
            title="Museum night", url="https://example.com/1", source="Culture"
        ),
        NewsItem(
            title="Rare birds", url="https://example.com/2", source="Nature"
        ),
    ]
    await repo.add(claimed.id, items)

    stored = await repo.list_for_run(claimed.id)
    assert len(stored) == 2
    assert [item.position for item in stored] == [0, 1]
    assert [item.url for item in stored] == [
        "https://example.com/1",
        "https://example.com/2",
    ]
    assert stored[0].slot_run_id == claimed.id
    assert stored[0].title == "Museum night"
    assert stored[0].source == "Culture"
    assert stored[0].created_at is not None

    # re-adding the same issue is idempotent (UNIQUE(slot_run_id, url))
    await repo.add(claimed.id, items)
    assert len(await repo.list_for_run(claimed.id)) == 2

    # another run keeps its own items
    other = await runs.try_claim("digest", "2026-10-10")
    assert other is not None
    assert await repo.list_for_run(other.id) == []


# --- rubric_state ---


@pytest.mark.asyncio
async def test_rubric_state_defaults(repo_path: Path) -> None:
    repo = RubricStateRepository(repo_path)

    state = await repo.get("unknown_rubric")

    assert state == RubricState(rubric_id="unknown_rubric")
    assert state.paused is False
    assert state.target is None


@pytest.mark.asyncio
async def test_rubric_state_survives_new_instance(repo_path: Path) -> None:
    repo = RubricStateRepository(repo_path)
    await repo.set_paused("weather", True)
    await repo.set_target("weather", "-100999000111")

    # a "restart": fresh repository instance over the same file
    state = await RubricStateRepository(repo_path).get("weather")

    assert state.paused is True
    assert state.target == "-100999000111"
    assert state.updated_at is not None

    # other rubrics are untouched
    other = await RubricStateRepository(repo_path).get("good_morning")
    assert other.paused is False


@pytest.mark.asyncio
async def test_rubric_state_pause_and_target_updates(repo_path: Path) -> None:
    repo = RubricStateRepository(repo_path)

    await repo.set_paused("digest", True)
    await repo.set_target("digest", "-100111000222")
    state = await repo.get("digest")
    assert state.paused is True

    await repo.set_paused("digest", False)
    state = await repo.get("digest")
    assert state.paused is False
    assert state.target == "-100111000222"  # target survives unpausing

    await repo.set_target("digest", None)
    state = await repo.get("digest")
    assert state.target is None


# --- content_bank ---


@pytest.mark.asyncio
async def test_content_bank_round_trip(repo_path: Path) -> None:
    repo = ContentBankRepository(repo_path)
    added = await repo.add(
        ContentBankItem(
            id=None,
            item_type="recipe",
            text="Яблочный пирог на кефире",
            metadata={"author": "bank-2026-10", "lines": 6},
            months=(9, 10, 11),
            event_date=None,
        )
    )

    loaded = await repo.get(added)
    assert loaded is not None
    assert loaded.id == added
    assert loaded.item_type == "recipe"
    assert loaded.text == "Яблочный пирог на кефире"
    assert loaded.metadata == {"author": "bank-2026-10", "lines": 6}
    assert loaded.months == (9, 10, 11)
    assert loaded.event_date is None
    assert loaded.review_status == "pending"
    assert loaded.show_count == 0
    assert loaded.last_shown_at is None
    assert loaded.created_at is not None


@pytest.mark.asyncio
async def test_content_bank_fact_with_event_date_round_trip(
    repo_path: Path,
) -> None:
    repo = ContentBankRepository(repo_path)
    added = await repo.add(
        ContentBankItem(
            id=None,
            item_type="fact",
            text="В 1712 году Пётр I перенёс столицу в Санкт-Петербург.",
            event_date="05-27",
        )
    )

    loaded = await repo.get(added)
    assert loaded is not None
    assert loaded.event_date == "05-27"
    assert loaded.months == ()  # no seasonality
    assert loaded.metadata == {}


@pytest.mark.asyncio
async def test_content_bank_duplicate_ignored(repo_path: Path) -> None:
    repo = ContentBankRepository(repo_path)
    item = ContentBankItem(
        id=None, item_type="thought", text="Тише едешь — дальше будешь."
    )

    first = await repo.add(item)
    second = await repo.add(item)

    assert first == second
    assert await repo.count_available("thought") == 0  # still pending review


@pytest.mark.asyncio
async def test_content_bank_list_available_filters(repo_path: Path) -> None:
    repo = ContentBankRepository(repo_path)

    # seasonal recipe: summer only
    summer = ContentBankItem(
        id=None,
        item_type="advice",
        text="В мае высаживайте рассаду.",
        review_status="approved",
        months=(5, 6),
    )
    # evergreen advice: any month
    evergreen = ContentBankItem(
        id=None,
        item_type="advice",
        text="Проверьте срок годности лекарств.",
        review_status="approved",
    )
    # "this day" fact
    fact = ContentBankItem(
        id=None,
        item_type="fact",
        text="День рождения Пушкина.",
        review_status="approved",
        event_date="06-06",
    )
    # not reviewed yet: must never be listed
    pending = ContentBankItem(
        id=None, item_type="advice", text="Черновой совет.", review_status="pending"
    )
    for item in (summer, evergreen, fact, pending):
        await repo.add(item)

    # seasonality: months is NULL (any) or contains the requested month
    assert [item.text for item in await repo.list_available("advice", month=5)] == [
        "В мае высаживайте рассаду.",
        "Проверьте срок годности лекарств.",
    ]
    assert [
        item.text for item in await repo.list_available("advice", month=10)
    ] == ["Проверьте срок годности лекарств."]
    assert [item.text for item in await repo.list_available("advice")] == [
        "В мае высаживайте рассаду.",
        "Проверьте срок годности лекарств.",
    ]

    # event_date matches exactly
    assert [
        item.text for item in await repo.list_available("fact", event_date="06-06")
    ] == ["День рождения Пушкина."]
    assert await repo.list_available("fact", event_date="01-01") == []

    assert await repo.count_available("advice") == 2


@pytest.mark.asyncio
async def test_content_bank_mark_shown(repo_path: Path) -> None:
    repo = ContentBankRepository(repo_path)
    added = await repo.add(
        ContentBankItem(
            id=None,
            item_type="postcard",
            text="Открытка: доброй ночи",
            review_status="approved",
        )
    )

    await repo.mark_shown(added)
    await repo.mark_shown(added)

    loaded = await repo.get(added)
    assert loaded is not None
    assert loaded.show_count == 2
    assert loaded.last_shown_at is not None


@pytest.mark.asyncio
async def test_content_bank_review_status_checked(repo_path: Path) -> None:
    repo = ContentBankRepository(repo_path)
    added = await repo.add(ContentBankItem(id=None, item_type="fact", text="F"))

    await repo.set_review_status(added, "approved")
    loaded = await repo.get(added)
    assert loaded is not None
    assert loaded.review_status == "approved"

    with sqlite3.connect(repo_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE content_bank SET review_status = 'vibes' WHERE id = ?",
                (added,),
            )


# --- filter_log ---


@pytest.mark.asyncio
async def test_filter_log_round_trip(repo_path: Path) -> None:
    repo = FilterLogRepository(repo_path)
    await repo.log(
        FilterLogEntry(
            id=None,
            stage="llm_classifier",
            decision="rejected",
            url="https://example.com/news",
            title="Something grim",
            reason="negative",
            model="yandexgpt-5-lite",
            response="негативная",
        )
    )
    await repo.log(
        FilterLogEntry(id=None, stage="stoplist", decision="passed")
    )

    entries = await repo.recent(10)

    assert len(entries) == 2
    # newest first
    assert entries[0].stage == "stoplist"
    assert entries[0].decision == "passed"
    assert entries[0].created_at is not None

    first = entries[1]
    assert first.stage == "llm_classifier"
    assert first.decision == "rejected"
    assert first.url == "https://example.com/news"
    assert first.title == "Something grim"
    assert first.reason == "negative"
    assert first.model == "yandexgpt-5-lite"
    assert first.response == "негативная"


# --- admin/summary queries (plan section 11) ---


@pytest.mark.asyncio
async def test_slot_run_list_for_date(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)
    for rubric_id in ("good_morning", "weather"):
        claimed = await repo.try_claim(rubric_id, "2026-10-09")
        assert claimed is not None
    other_day = await repo.try_claim("good_morning", "2026-10-10")
    assert other_day is not None

    runs = await repo.list_for_date("2026-10-09")

    assert [run.rubric_id for run in runs] == ["good_morning", "weather"]
    assert all(run.slot_date == "2026-10-09" for run in runs)


@pytest.mark.asyncio
async def test_slot_run_get_by_id(repo_path: Path) -> None:
    repo = SlotRunRepository(repo_path)
    claimed = await repo.try_claim("good_morning", "2026-10-09")
    assert claimed is not None and claimed.id is not None

    loaded = await repo.get_by_id(claimed.id)
    assert loaded == claimed

    assert await repo.get_by_id(99999) is None


@pytest.mark.asyncio
async def test_content_bank_count_available_by_type(repo_path: Path) -> None:
    repo = ContentBankRepository(repo_path)
    for index in range(3):
        await repo.add(
            ContentBankItem(
                id=None,
                item_type="thought",
                text=f"thought {index}",
                review_status="approved",
            )
        )
    await repo.add(
        ContentBankItem(
            id=None, item_type="fact", text="fact pending review"
        )
    )

    counts = await repo.count_available_by_type()

    assert counts == {"thought": 3}  # pending review is not a reserve


@pytest.mark.asyncio
async def test_filter_log_count_between(repo_path: Path) -> None:
    repo = FilterLogRepository(repo_path)
    with sqlite3.connect(repo_path) as conn:
        conn.executemany(
            "INSERT INTO filter_log (stage, decision, created_at) "
            "VALUES ('stoplist', 'passed', ?)",
            [("2026-10-09 10:00:00",), ("2026-10-09 20:59:59",)],
        )
        conn.execute(
            "INSERT INTO filter_log (stage, decision, created_at) "
            "VALUES ('stoplist', 'passed', '2026-10-09 21:00:00')"
        )  # exactly the end bound: excluded (half-open interval)
        conn.commit()

    start = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)  # 00:00 MSK
    end = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)

    assert await repo.count_between(start, end) == 2
