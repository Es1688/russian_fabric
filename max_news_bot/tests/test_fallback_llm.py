"""LLM completion failover tests (plan §2.3, §2.5, §2.6).

Two local FakeLlmServers impersonate the primary and the fallback
OpenAI-compatible channels: primary ok means the fallback is never hit;
an error of any kind on the primary hands the call to the fallback model;
both failing propagates the fallback's own error as is. Events land in
bot_stats (durable across reconnections) and config errors of any channel
raise the alert hook while infra errors stay silent.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from aiohttp import web

from max_news_bot.tests.mocks import MockAlerter
from max_news_bot.utils.db import Database
from max_news_bot.writers.completion import (
    LlmCompletionClient,
    LlmCompletionError,
)
from max_news_bot.writers.fallback import FallbackCompletion

_API_KEY = "test-api-key"
_PRIMARY_MODEL = "primary-model"
_FALLBACK_MODEL = "fallback-model"
_OUTPUT = "Пост о релизе Python 3.13 и новом JIT-компиляторе. " * 3
_SYSTEM = "Ты генеративный пайплайн рубрик."
_USER = "Напиши пост про релиз Python 3.13."


class FakeLlmServer:
    """Local aiohttp server impersonating an OpenAI-compatible API."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.content = _OUTPUT
        self.tokens = 123
        # when set, returned verbatim instead of a valid JSON payload
        self.raw_body: str | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def completions(request: web.Request) -> web.Response:
            self.requests.append(
                {"headers": dict(request.headers), "json": await request.json()}
            )
            if self.raw_body is not None:
                return web.Response(
                    status=self.status, text=self.raw_body, content_type="text/plain"
                )
            payload = {
                "id": "cmpl-test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": self.tokens},
            }
            return web.json_response(payload, status=self.status)

        app = web.Application()
        app.router.add_post("/chat/completions", completions)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def channels():
    primary = FakeLlmServer()
    primary_url = await primary.start()
    fallback = FakeLlmServer()
    fallback_url = await fallback.start()
    yield (primary, primary_url), (fallback, fallback_url)
    await primary.stop()
    await fallback.stop()


def make_client(base_url: str, model: str) -> LlmCompletionClient:
    return LlmCompletionClient(
        base_url=base_url,
        api_key=_API_KEY,
        model=model,
        timeout=5,
        max_tokens=2000,
        temperature=0.7,
        session=None,
    )


# --- failover scenarios ----------------------------------------------------------


async def test_completion_primary_ok_fallback_not_called(channels) -> None:
    (primary, primary_url), (fallback, fallback_url) = channels
    completion = FallbackCompletion(
        make_client(primary_url, _PRIMARY_MODEL),
        make_client(fallback_url, _FALLBACK_MODEL),
    )

    result = await completion.complete(_SYSTEM, _USER)

    assert result.content == _OUTPUT
    assert result.model == _PRIMARY_MODEL
    assert len(primary.requests) == 1
    assert fallback.requests == []


@pytest.mark.parametrize("scenario", ["500", "401", "garbage"])
async def test_completion_primary_errors_fall_back(channels, scenario: str) -> None:
    (primary, primary_url), (fallback, fallback_url) = channels
    if scenario == "garbage":
        primary.raw_body = "{definitely not json"
    else:
        primary.status = int(scenario)
    completion = FallbackCompletion(
        make_client(primary_url, _PRIMARY_MODEL),
        make_client(fallback_url, _FALLBACK_MODEL),
    )

    result = await completion.complete(_SYSTEM, _USER)

    assert result.content == _OUTPUT
    assert result.model == _FALLBACK_MODEL
    assert len(fallback.requests) == 1


@pytest.mark.parametrize(
    ("primary_status", "fallback_status", "expected_kind"),
    [(500, 401, "config"), (401, 500, "infra")],
)
async def test_completion_both_fail_fallback_error_propagates_as_is(
    channels, primary_status: int, fallback_status: int, expected_kind: str
) -> None:
    (primary, primary_url), (fallback, fallback_url) = channels
    primary.status = primary_status
    fallback.status = fallback_status
    completion = FallbackCompletion(
        make_client(primary_url, _PRIMARY_MODEL),
        make_client(fallback_url, _FALLBACK_MODEL),
    )

    with pytest.raises(LlmCompletionError) as exc_info:
        await completion.complete(_SYSTEM, _USER)

    assert exc_info.value.kind == expected_kind
    assert str(fallback_status) in str(exc_info.value)
    assert len(fallback.requests) == 1


# --- bot_stats events -----------------------------------------------------------


async def test_completion_events_durable_across_reconnections(
    channels, tmp_path
) -> None:
    (primary, primary_url), (fallback, fallback_url) = channels
    db = Database(tmp_path / "stats.db")
    await db.initialize()
    completion = FallbackCompletion(
        make_client(primary_url, _PRIMARY_MODEL),
        make_client(fallback_url, _FALLBACK_MODEL),
        db=db,
    )

    await completion.complete(_SYSTEM, _USER)  # primary ok
    primary.status = 500
    await completion.complete(_SYSTEM, _USER)  # fallback used
    fallback.status = 401
    with pytest.raises(LlmCompletionError):
        await completion.complete(_SYSTEM, _USER)  # both fail

    # a brand new Database instance over the same file: a re-created
    # connection still sees every committed event
    reopened = Database(tmp_path / "stats.db")
    await reopened.initialize()
    for event in ("llm_primary_ok", "llm_fallback_used", "llm_fallback_error_config"):
        assert await reopened.get_last_event(event) is not None, event
    conn = sqlite3.connect(tmp_path / "stats.db")
    rows = conn.execute(
        "SELECT event, COUNT(*) FROM bot_stats WHERE event LIKE 'llm_%' "
        "GROUP BY event"
    ).fetchall()
    conn.close()
    counts = dict(rows)
    assert counts == {
        "llm_primary_ok": 1,
        "llm_fallback_used": 1,
        "llm_fallback_error_config": 1,
    }
    # the fallback-used event keeps the primary error as context
    last_used = await reopened.get_last_event("llm_fallback_used")
    assert last_used is not None
    assert "500" in (last_used.get("details") or "")


# --- admin alert hook -----------------------------------------------------------


async def test_completion_config_error_alerts_infra_does_not(channels) -> None:
    (primary, primary_url), (fallback, fallback_url) = channels
    alerter = MockAlerter()
    completion = FallbackCompletion(
        make_client(primary_url, _PRIMARY_MODEL),
        make_client(fallback_url, _FALLBACK_MODEL),
        alerter=alerter,
    )

    primary.status = 401
    result = await completion.complete(_SYSTEM, _USER)
    assert result.model == _FALLBACK_MODEL
    assert len(alerter.alerts) == 1
    assert "primary" in alerter.alerts[0]

    primary.status = 500  # infra: no new alert
    result = await completion.complete(_SYSTEM, _USER)
    assert result.model == _FALLBACK_MODEL
    assert len(alerter.alerts) == 1

    # a config error of the OTHER channel alerts separately: the primary
    # stays infra-broken so the call reaches the broken fallback
    fallback.status = 401
    with pytest.raises(LlmCompletionError):
        await completion.complete(_SYSTEM, _USER)
    assert len(alerter.alerts) == 2
    assert "fallback" in alerter.alerts[1]


class FlakyAlerter(MockAlerter):
    """Delivery double failing the first N sends, then recording normally."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self._failures = failures

    async def send_alert(self, text: str) -> None:
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("max is unreachable")
        await super().send_alert(text)


async def test_alert_delivery_failure_does_not_consume_daily_dedup(
    channels,
) -> None:
    (primary, primary_url), (fallback, fallback_url) = channels
    alerter = FlakyAlerter()
    completion = FallbackCompletion(
        make_client(primary_url, _PRIMARY_MODEL),
        make_client(fallback_url, _FALLBACK_MODEL),
        alerter=alerter,
    )

    primary.status = 401
    result = await completion.complete(_SYSTEM, _USER)
    assert result.model == _FALLBACK_MODEL
    assert alerter.alerts == []  # delivery failed, nothing reached admins

    # a failed delivery must not consume the daily dedup: the next config
    # error of the same channel retries and now reaches the admins
    result = await completion.complete(_SYSTEM, _USER)
    assert result.model == _FALLBACK_MODEL
    assert len(alerter.alerts) == 1
    assert "primary" in alerter.alerts[0]

    # once delivered, further same-day errors stay deduped
    result = await completion.complete(_SYSTEM, _USER)
    assert result.model == _FALLBACK_MODEL
    assert len(alerter.alerts) == 1
