from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from max_news_bot.bot import polling
from max_news_bot.bot.polling import polling_loop
from max_news_bot.utils.db import Database


def make_update(mid: int = 1) -> dict:
    return {
        "update_type": "message_created",
        "timestamp": 123,
        "marker": 456,
        "update": {
            "sender": {"user_id": 42, "name": "Tester"},
            "recipient": {"chat_id": -100200300, "chat_type": "dialog"},
            "message": {"body": {"text": "/status", "mid": mid}, "timestamp": 123},
        },
    }


class ScriptedClient:
    """MaxClient double: plays scripted get_updates actions in order, then
    parks in a long sleep so the loop under test waits (and cancellation
    lands on a real await)."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[dict] = []
        self.parked = asyncio.Event()

    async def get_updates(
        self, marker: int | None = None, *, timeout: int = 30,
        limit: int = 100, types: list[str] | None = None,
    ):
        self.calls.append(
            {"marker": marker, "timeout": timeout, "limit": limit, "types": types}
        )
        if self._script:
            action = self._script.pop(0)
        else:
            self.parked.set()
            await asyncio.sleep(30)
            return [], None
        if isinstance(action, Exception):
            raise action
        return action


async def _run_until_parked(client: ScriptedClient, handle_update, db: Database):
    task = asyncio.create_task(polling_loop(client, handle_update, db))
    await asyncio.wait_for(client.parked.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task


async def _make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "polling.db")
    await db.initialize()
    return db


async def test_polling_handles_updates_advances_marker_and_survives_errors(
    tmp_path, monkeypatch, caplog
) -> None:
    # shrink the network-retry sleep so the error path is instant
    monkeypatch.setattr(polling, "_RETRY_DELAY_SECONDS", 0)
    db = await _make_db(tmp_path)

    update = make_update()
    handled: list[dict] = []

    async def handle_update(received: dict) -> None:
        handled.append(received)
        raise ValueError("boom")

    client = ScriptedClient(
        [
            ([update], 100),  # one update, marker advances to 100
            ([], None),  # empty poll: marker must stay 100
            RuntimeError("network down"),  # get_updates failure: log + retry
        ]
    )

    with caplog.at_level(logging.ERROR, logger="max_news_bot.bot.polling"):
        task = await _run_until_parked(client, handle_update, db)

    assert task.cancelled()
    assert handled == [update]

    # marker: first poll without one, None marker and the error did not
    # advance it, all later polls reuse 100
    markers = [call["marker"] for call in client.calls]
    assert markers[0] is None
    assert all(marker == 100 for marker in markers[1:])

    # long-poll subscription parameters forwarded as required
    assert client.calls[0]["types"] == ["message_created", "bot_started"]
    assert client.calls[0]["timeout"] == 30

    # handler crash recorded exactly like the source error handler did
    last_error = await db.get_last_event("error")
    assert last_error is not None
    assert last_error["details"] == "unhandled_error=ValueError: boom"

    messages = [record.getMessage() for record in caplog.records]
    assert any("unhandled_error error=ValueError: boom" in m for m in messages)
    assert any(
        "polling_get_updates_failed error=RuntimeError: network down" in m
        for m in messages
    )


async def test_polling_get_updates_failure_logs_exception_type(
    tmp_path, monkeypatch, caplog
) -> None:
    # TimeoutError str() is empty; the log must still name the exception type
    monkeypatch.setattr(polling, "_RETRY_DELAY_SECONDS", 0)
    db = await _make_db(tmp_path)

    async def handle_update(update: dict) -> None:
        return None

    client = ScriptedClient([asyncio.TimeoutError()])

    with caplog.at_level(logging.ERROR, logger="max_news_bot.bot.polling"):
        await _run_until_parked(client, handle_update, db)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "polling_get_updates_failed error=TimeoutError" in m for m in messages
    )


async def test_polling_reuses_marker_when_api_returns_none(tmp_path) -> None:
    db = await _make_db(tmp_path)

    async def handle_update(update: dict) -> None:
        return None

    client = ScriptedClient([([], None), ([], 7)])

    await _run_until_parked(client, handle_update, db)

    markers = [call["marker"] for call in client.calls]
    assert markers == [None, None, 7]


async def test_polling_handles_every_update_in_batch(tmp_path) -> None:
    db = await _make_db(tmp_path)
    handled: list[dict] = []

    async def handle_update(update: dict) -> None:
        handled.append(update)

    first, second = make_update(mid=1), make_update(mid=2)
    client = ScriptedClient([([first, second], None)])

    await _run_until_parked(client, handle_update, db)

    assert handled == [first, second]


async def test_polling_cancellation_propagates(tmp_path) -> None:
    db = await _make_db(tmp_path)

    async def handle_update(update: dict) -> None:
        return None

    client = ScriptedClient([])

    task = asyncio.create_task(polling_loop(client, handle_update, db))
    await asyncio.wait_for(client.parked.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # CancelledError propagated out of polling_loop, not swallowed
    assert task.cancelled()
