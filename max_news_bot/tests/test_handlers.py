from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from max_news_bot.bot.handlers import BOT_COMMANDS, create_dispatcher
from max_news_bot.config.loader import AppConfig
from max_news_bot.core.factory import ContentFactory
from max_news_bot.core.models import Post
from max_news_bot.core.pipeline import Pipeline, PipelineResult
from max_news_bot.tests.mocks import (
    MockAlerter,
    MockFetcher,
    MockPostPublisher,
    MockPublisher,
    MockWriter,
)
from max_news_bot.utils.db import Database


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


def _make_post(**overrides) -> Post:
    fields: dict = dict(
        id=None,
        news_url="https://example.com/news/1",
        source="Test RSS",
        title="Python release improves compiler speed",
        text=None,
        status="draft",
    )
    fields.update(overrides)
    return Post(**fields)


@pytest.fixture
def handler_setup(app_config: AppConfig, db: Database):
    pipeline = Pipeline(app_config, MockFetcher(), MockPublisher(), db)
    client = RecordingClient()
    handle_update = create_dispatcher(
        app_config,
        pipeline,
        db,
        asyncio.Lock(),
        datetime.now(timezone.utc),
        client=client,
    )
    return SimpleNamespace(
        handle_update=handle_update,
        pipeline=pipeline,
        client=client,
        db=db,
        app_config=app_config,
    )


@pytest.fixture
def factory_setup(app_config: AppConfig, db: Database):
    fetcher = MockFetcher()
    publisher = MockPublisher()
    pipeline = Pipeline(app_config, fetcher, publisher, db)
    writer = MockWriter()
    factory = ContentFactory(
        app_config, fetcher, writer, MockPostPublisher(), MockAlerter(), db
    )
    client = RecordingClient()
    handle_update = create_dispatcher(
        app_config,
        pipeline,
        db,
        asyncio.Lock(),
        datetime.now(timezone.utc),
        factory=factory,
        factory_lock=asyncio.Lock(),
        client=client,
    )
    return SimpleNamespace(
        handle_update=handle_update,
        pipeline=pipeline,
        factory=factory,
        writer=writer,
        client=client,
        db=db,
        app_config=app_config,
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
        "sources",
        "stats",
        "news",
        "factory",
        "posts",
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


async def test_chatid_replies_with_current_chat_id(handler_setup) -> None:
    await _invoke(handler_setup, "/chatid", chat_id=-777000111)

    assert len(handler_setup.client.sent) == 1
    chat_id, text, _ = handler_setup.client.sent[0]
    assert chat_id == -777000111
    assert str(-777000111) in text


# --- /news --------------------------------------------------------------------


async def test_news_allowed_for_admin(handler_setup) -> None:
    with patch.object(
        handler_setup.pipeline,
        "run",
        new=AsyncMock(
            return_value=PipelineResult(collected=1, filtered=1, published=1, skipped=0)
        ),
    ) as mock_run:
        await _invoke(handler_setup, "/news", user_id=handler_setup.app_config.admin_ids[0])

    mock_run.assert_awaited_once()
    texts = _texts(handler_setup)
    assert "Running pipeline..." in texts
    assert "Pipeline done: collected=1 published=1" in texts
    assert not any("Access denied" in text for text in texts)


async def test_news_denied_for_non_admin(handler_setup) -> None:
    with patch.object(
        handler_setup.pipeline, "run", new=AsyncMock()
    ) as mock_run:
        await _invoke(handler_setup, "/news")

    mock_run.assert_not_awaited()
    assert _texts(handler_setup) == ["Access denied."]


# --- /status /sources /stats --------------------------------------------------


async def test_status_command_responds(handler_setup, db: Database) -> None:
    await db.log_event("run", "collected=1")

    await _invoke(handler_setup, "/status")

    texts = _texts(handler_setup)
    assert any("Uptime:" in text for text in texts)
    assert any("Last run:" in text for text in texts)
    assert any("Last error:" in text for text in texts)


async def test_sources_command_lists_sources(handler_setup) -> None:
    await _invoke(handler_setup, "/sources")

    texts = _texts(handler_setup)
    assert any("Test RSS" in text and "enabled" in text for text in texts)
    assert any("Disabled RSS" in text and "disabled" in text for text in texts)


async def test_stats_command_responds(handler_setup, db: Database) -> None:
    await db.log_event("publish", "published=1")

    await _invoke(handler_setup, "/stats")

    assert any("Daily stats:" in text for text in _texts(handler_setup))


# --- /factory -----------------------------------------------------------------


async def test_factory_run_allowed_for_admin(factory_setup) -> None:
    await _invoke(
        factory_setup, "/factory run", user_id=factory_setup.app_config.admin_ids[0]
    )

    texts = _texts(factory_setup)
    assert not any("Access denied" in text for text in texts)
    assert "Running factory cycle..." in texts
    assert any("Factory cycle done:" in text for text in texts)


async def test_factory_bare_command_still_runs_factory_cycle(factory_setup) -> None:
    # backward compatibility: /factory without arguments equals /factory run
    await _invoke(factory_setup, "/factory", user_id=factory_setup.app_config.admin_ids[0])

    texts = _texts(factory_setup)
    assert "Running factory cycle..." in texts
    assert any("Factory cycle done:" in text for text in texts)


async def test_factory_run_reports_disabled_when_no_factory(handler_setup) -> None:
    await _invoke(handler_setup, "/factory run", user_id=handler_setup.app_config.admin_ids[0])

    assert any("Factory is disabled" in text for text in _texts(handler_setup))


async def test_factory_denied_for_non_admin(factory_setup) -> None:
    await _invoke(factory_setup, "/factory")

    assert _texts(factory_setup) == ["Access denied."]
    assert factory_setup.writer.calls == []


async def test_factory_pause_sets_flag(factory_setup) -> None:
    await _invoke(
        factory_setup, "/factory pause", user_id=factory_setup.app_config.admin_ids[0]
    )

    assert any("paused" in text.lower() for text in _texts(factory_setup))
    assert await factory_setup.db.get_state_flag("factory") is False


async def test_factory_resume_clears_both_pauses(factory_setup) -> None:
    db = factory_setup.db
    await db.set_state_flag("factory", False)
    await db.set_state_flag("factory_autopause", False)

    await _invoke(
        factory_setup, "/factory resume", user_id=factory_setup.app_config.admin_ids[0]
    )

    assert await db.get_state_flag("factory") is True
    assert await db.get_state_flag("factory_autopause") is True
    assert any("resumed" in text.lower() for text in _texts(factory_setup))


async def test_factory_status_shows_state(factory_setup) -> None:
    await factory_setup.db.set_state_flag("factory", False)
    await factory_setup.db.set_state_flag("factory_autopause", False)
    await factory_setup.db.log_event("factory_alert", "kind=config error=llm http 401")

    await _invoke(
        factory_setup, "/factory status", user_id=factory_setup.app_config.admin_ids[0]
    )

    texts = _texts(factory_setup)
    assert any("enabled" in text for text in texts)
    assert any("Paused (manual): yes" in text for text in texts)
    assert any("Auto-paused: yes" in text for text in texts)
    assert any("llm http 401" in text for text in texts)


async def test_factory_usage_hint_on_unknown_action(factory_setup) -> None:
    await _invoke(
        factory_setup, "/factory frobnicate", user_id=factory_setup.app_config.admin_ids[0]
    )

    hint = _texts(factory_setup)[0]
    assert "Usage: /factory" in hint
    # the hint documents the rubric argument (plan section 11); angle
    # brackets are html-escaped because replies go as fmt=html
    assert "[all|&lt;rubric&gt;]" in hint
    assert "/factory retract slot &lt;run_id&gt;" in hint


async def test_factory_rubric_argument_answers_disabled_without_contour(
    factory_setup,
) -> None:
    await _invoke(
        factory_setup,
        "/factory pause weather",
        user_id=factory_setup.app_config.admin_ids[0],
    )

    assert _texts(factory_setup) == [
        "Rubrics contour is disabled (check RUBRICS_ENABLED)."
    ]


async def test_factory_retract_deletes_channel_message(factory_setup) -> None:
    post_id = await factory_setup.db.save_attempt(_make_post(status="draft"))
    await factory_setup.db.mark_post_published(post_id, "mid.555")

    await _invoke(
        factory_setup,
        f"/factory retract {post_id}",
        user_id=factory_setup.app_config.admin_ids[0],
    )

    # MaxClient.delete_message takes the message id only (no chat_id)
    assert factory_setup.client.deleted == ["mid.555"]
    assert any("retracted" in text for text in _texts(factory_setup))
    event = await factory_setup.db.get_last_event("factory_retract")
    assert event is not None
    assert f"id={post_id}" in event["details"]


async def test_factory_retract_refuses_previewed_post(factory_setup) -> None:
    post_id = await factory_setup.db.save_attempt(_make_post(status="previewed"))

    await _invoke(
        factory_setup,
        f"/factory retract {post_id}",
        user_id=factory_setup.app_config.admin_ids[0],
    )

    assert factory_setup.client.deleted == []
    assert any("Cannot retract" in text for text in _texts(factory_setup))


async def test_factory_retract_unknown_post(factory_setup) -> None:
    await _invoke(
        factory_setup,
        "/factory retract 12345",
        user_id=factory_setup.app_config.admin_ids[0],
    )

    assert factory_setup.client.deleted == []
    assert any("not found" in text for text in _texts(factory_setup))


async def test_factory_retract_reports_api_failure(factory_setup) -> None:
    factory_setup.client._raise_on_delete = RuntimeError("max is down")
    post_id = await factory_setup.db.save_attempt(_make_post(status="draft"))
    await factory_setup.db.mark_post_published(post_id, "mid.556")

    await _invoke(
        factory_setup,
        f"/factory retract {post_id}",
        user_id=factory_setup.app_config.admin_ids[0],
    )

    assert any("Retract failed" in text for text in _texts(factory_setup))
    event = await factory_setup.db.get_last_event("factory_retract")
    assert event is None


# --- /posts -------------------------------------------------------------------


async def test_posts_lists_recent_posts(factory_setup) -> None:
    await factory_setup.db.save_attempt(
        _make_post(status="published", news_url="https://example.com/a")
    )
    await factory_setup.db.save_attempt(
        _make_post(
            status="failed",
            news_url="https://example.com/b",
            error="llm http 429",
        )
    )

    await _invoke(factory_setup, "/posts", user_id=factory_setup.app_config.admin_ids[0])

    texts = _texts(factory_setup)
    assert any("published" in text for text in texts)
    assert any("failed" in text for text in texts)
    assert any("llm http 429" in text for text in texts)


async def test_posts_denied_for_non_admin(factory_setup) -> None:
    await _invoke(factory_setup, "/posts")

    assert _texts(factory_setup) == ["Access denied."]


async def test_posts_empty_answers_politely(factory_setup) -> None:
    await _invoke(factory_setup, "/posts", user_id=factory_setup.app_config.admin_ids[0])

    assert any("No posts yet" in text for text in _texts(factory_setup))


async def test_posts_escapes_html_in_dynamic_values(factory_setup) -> None:
    await factory_setup.db.save_attempt(
        _make_post(
            status="failed",
            news_url="https://example.com/x",
            title="Python <b>3.13</b> & more",
            error=None,
        )
    )

    await _invoke(factory_setup, "/posts", user_id=factory_setup.app_config.admin_ids[0])

    text = _texts(factory_setup)[0]
    assert "Python &lt;b&gt;3.13&lt;/b&gt; &amp; more" in text
    assert "<b>3.13</b>" not in text


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
        handler_setup.pipeline,
        "run",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await _invoke(handler_setup, "/news", user_id=handler_setup.app_config.admin_ids[0])

    texts = _texts(handler_setup)
    assert "Running pipeline..." in texts
    assert (
        "Internal error occurred. Check /status later for details." in texts
    )

    last_error = await handler_setup.db.get_last_event("error")
    assert last_error is not None
    assert last_error["details"].startswith("unhandled_error=RuntimeError: boom")
