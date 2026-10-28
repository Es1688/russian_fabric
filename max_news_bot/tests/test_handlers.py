from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from max_news_bot.bot.handlers import BOT_COMMANDS, create_dispatcher
from max_news_bot.bot.rubrics_admin import RubricsAdmin
from max_news_bot.bot.slot_scheduler import SlotScheduler
from max_news_bot.config.loader import AppConfig
from max_news_bot.tests.mocks import MockAlerter
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import (
    RubricStateRepository,
    SlotRunRepository,
)
from max_news_bot.tests.test_slot_scheduler import (
    FakePipeline,
    FakeSlotPublisher,
    make_config,
    make_rubric,
)


CHAT_ID = -100200300
NON_ADMIN_ID = 999999


def make_update(
    text: str | None,
    *,
    user_id: int = NON_ADMIN_ID,
    chat_id: int = CHAT_ID,
) -> dict:
    """Hand-made MAX message_created update (defensive-parse target).

    Shape matches the real API (no "update" wrapper — message sits directly
    on the update object, sender/recipient inside message)."""
    return {
        "update_type": "message_created",
        "timestamp": 123,
        "message": {
            "sender": {"user_id": user_id, "name": "Tester"},
            "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
            "body": {"text": text, "mid": "mid.789"},
        },
        "user_locale": "ru",
    }


def make_bot_started_update(*, user_id: int = 42, chat_id: int = CHAT_ID) -> dict:
    # real bot_started carries chat_id and the acting user directly
    return {
        "update_type": "bot_started",
        "timestamp": 123,
        "chat_id": chat_id,
        "user": {"user_id": user_id, "name": "Tester"},
    }


class RecordingClient:
    """MaxClient double: records sends and deletes, can fail deletes."""

    def __init__(self, *, raise_on_delete: Exception | None = None) -> None:
        self.sent: list[tuple[int, str, str]] = []
        self.deleted: list[str] = []
        self._raise_on_delete = raise_on_delete

    async def send_message(
        self, chat_id: int, text: str, *, fmt: str = "html",
        disable_link_preview: bool = False,
    ) -> str:
        self.sent.append((chat_id, text, fmt))
        return f"mid.{len(self.sent)}"

    async def delete_message(self, message_id: str) -> None:
        if self._raise_on_delete is not None:
            raise self._raise_on_delete
        self.deleted.append(message_id)


def _texts(setup: SimpleNamespace) -> list[str]:
    return [text for _, text, _ in setup.client.sent]


async def _invoke(
    setup: SimpleNamespace,
    text: str | None,
    *,
    user_id: int = NON_ADMIN_ID,
    chat_id: int = CHAT_ID,
) -> None:
    await setup.handle_update(
        make_update(text, user_id=user_id, chat_id=chat_id)
    )


def _make_admin(db: Database) -> RubricsAdmin:
    config = make_config(
        [
            make_rubric("good_morning"),
            make_rubric("weather", kind="weather", on_llm_failure="skip"),
        ]
    )
    scheduler = SlotScheduler(
        config,
        SlotRunRepository(db.path),
        RubricStateRepository(db.path),
        FakePipeline(),
        FakeSlotPublisher(),
        MockAlerter(),
    )
    return RubricsAdmin(
        config,
        scheduler,
        SlotRunRepository(db.path),
        RubricStateRepository(db.path),
        db,
    )


@pytest.fixture
def handler_setup(app_config: AppConfig, db: Database, tmp_path: Path):
    client = RecordingClient()
    admin = _make_admin(db)
    handle_update = create_dispatcher(
        app_config,
        db,
        datetime.now(timezone.utc),
        client=client,
        rubrics_admin=admin,
    )
    return SimpleNamespace(
        handle_update=handle_update,
        client=client,
        db=db,
        app_config=app_config,
        admin=admin,
    )


def test_bot_commands_registry_shape() -> None:
    # PATCH /me/commands expects {"commands":[{"name": ..., "description": ...}]}
    # with bare names (no leading slash).
    assert {"name", "description"} == set(BOT_COMMANDS[0].keys())
    assert all(set(entry.keys()) == {"name", "description"} for entry in BOT_COMMANDS)
    assert [entry["name"] for entry in BOT_COMMANDS] == [
        "start",
        "chatid",
        "status",
        "stats",
        "factory",
    ]
    assert not any(entry["name"].startswith("/") for entry in BOT_COMMANDS)


# --- /start and /chatid -------------------------------------------------------


async def test_start_replies_greeting(handler_setup) -> None:
    await _invoke(handler_setup, "/start")

    assert len(handler_setup.client.sent) == 1
    chat_id, text, fmt = handler_setup.client.sent[0]
    assert chat_id == CHAT_ID
    assert "Привет" in text
    assert fmt == "html"


async def test_bot_started_replies_greeting(handler_setup) -> None:
    await handler_setup.handle_update(make_bot_started_update(user_id=42))

    assert len(handler_setup.client.sent) == 1
    assert "Привет" in handler_setup.client.sent[0][1]


async def test_chatid_replies_with_chat_id_and_user_id(handler_setup) -> None:
    await _invoke(handler_setup, "/chatid", chat_id=-777000111, user_id=555000)

    assert len(handler_setup.client.sent) == 1
    chat_id, text, _ = handler_setup.client.sent[0]
    assert chat_id == -777000111
    # both ids, with the config variable each one belongs to
    assert "chat_id: -777000111" in text
    assert "MAX_CHANNEL_ID" in text
    assert "user_id: 555000" in text
    assert "ADMIN_IDS" in text


# --- /status /stats -----------------------------------------------------------


async def test_status_command_responds(handler_setup, db: Database) -> None:
    await db.log_event("slot_heartbeat", "quiet")

    await _invoke(handler_setup, "/status")

    texts = _texts(handler_setup)
    assert any("Uptime:" in text for text in texts)
    assert any("Last scheduler heartbeat:" in text for text in texts)
    assert any("Last error:" in text for text in texts)


async def test_stats_command_responds(handler_setup, db: Database) -> None:
    # /stats reads publications from slot_runs and errors from bot_stats
    runs = SlotRunRepository(db.path)
    run = await runs.try_claim(
        "good_morning", "2026-10-28", target="-1001234567890", is_dry_run=False
    )
    await runs.save(_published(run))

    await _invoke(handler_setup, "/stats")

    texts = _texts(handler_setup)
    assert any("Daily stats:" in text for text in texts)
    assert any("publish=1" in text for text in texts)


def _published(run):
    from dataclasses import replace

    return replace(
        run, status="published", published_at=datetime.now(timezone.utc)
    )


# --- /factory -----------------------------------------------------------------


async def test_factory_status_shows_rubrics(handler_setup) -> None:
    await _invoke(handler_setup, "/factory status", user_id=handler_setup.app_config.admin_ids[0])

    texts = _texts(handler_setup)
    assert any("good_morning" in text for text in texts)
    assert not any("Access denied" in text for text in texts)


async def test_factory_bare_command_shows_status(handler_setup) -> None:
    # /factory without arguments answers with the status overview
    await _invoke(handler_setup, "/factory", user_id=handler_setup.app_config.admin_ids[0])

    assert any("good_morning" in text for text in _texts(handler_setup))


async def test_factory_denied_for_non_admin(handler_setup) -> None:
    await _invoke(handler_setup, "/factory")

    assert _texts(handler_setup) == ["Access denied."]


async def test_factory_pause_all_pauses_rubrics(handler_setup) -> None:
    await _invoke(
        handler_setup, "/factory pause all", user_id=handler_setup.app_config.admin_ids[0]
    )

    assert any("paused" in text.lower() for text in _texts(handler_setup))
    states = RubricStateRepository(handler_setup.db.path)
    assert (await states.get("good_morning")).paused is True


async def test_factory_usage_hint_on_unknown_action(handler_setup) -> None:
    await _invoke(
        handler_setup, "/factory frobnicate", user_id=handler_setup.app_config.admin_ids[0]
    )

    hint = _texts(handler_setup)[0]
    assert "Usage: /factory" in hint
    # the hint documents the rubric argument (plan section 11); angle
    # brackets are html-escaped because replies go as fmt=html
    assert "[all|&lt;rubric&gt;]" in hint
    assert "/factory retract slot &lt;run_id&gt;" in hint


async def test_factory_retract_requires_slot_keyword(handler_setup) -> None:
    await _invoke(
        handler_setup,
        "/factory retract 5",
        user_id=handler_setup.app_config.admin_ids[0],
    )

    # the legacy post-id form is gone: only /factory retract slot <run_id>
    assert any(
        "Usage: /factory retract slot" in text for text in _texts(handler_setup)
    )
    assert handler_setup.client.deleted == []


# --- non-commands and malformed updates ---------------------------------------


async def test_unknown_command_ignored_silently(handler_setup) -> None:
    await _invoke(handler_setup, "/unknown")

    assert handler_setup.client.sent == []


async def test_plain_text_ignored_silently(handler_setup) -> None:
    await _invoke(handler_setup, "hello world")
    await _invoke(handler_setup, None)  # message without a text body

    assert handler_setup.client.sent == []


async def test_malformed_updates_do_not_raise(handler_setup) -> None:
    broken_updates: list = [
        {},
        None,
        {"update_type": "message_created"},
        {"message": {}},
        # legacy TamTam-style wrapper: must be treated as unknown shape
        {
            "update_type": "message_created",
            "update": {
                "sender": {"user_id": 42},
                "recipient": {"chat_id": CHAT_ID},
            },
        },
        {"update_type": "message_created", "message": {"recipient": {}}},
        {
            "update_type": "message_created",
            "message": {"recipient": {"chat_id": "abc"}},
        },
        {
            "update_type": "message_created",
            "message": {"recipient": {"chat_id": CHAT_ID}, "body": {}},
        },
        {
            "update_type": "message_created",
            "message": {
                "recipient": {"chat_id": CHAT_ID},
                "sender": {"user_id": True},  # bool must not pass as an id
                "body": {"text": "just chatting"},
            },
        },
        {"update_type": 42, "message": {"recipient": {"chat_id": CHAT_ID}}},
    ]
    for update in broken_updates:
        await handler_setup.handle_update(update)

    assert handler_setup.client.sent == []


# --- error handling (port of the source error_handler.py UX) -------------------


async def test_handler_crash_replies_and_records_error(handler_setup) -> None:
    with patch.object(
        handler_setup.admin,
        "status_text",
        side_effect=RuntimeError("boom"),
    ):
        await _invoke(handler_setup, "/factory status", user_id=handler_setup.app_config.admin_ids[0])

    texts = _texts(handler_setup)
    assert (
        "Internal error occurred. Check /status later for details." in texts
    )

    last_error = await handler_setup.db.get_last_event("error")
    assert last_error is not None
    assert last_error["details"].startswith("unhandled_error=RuntimeError: boom")
