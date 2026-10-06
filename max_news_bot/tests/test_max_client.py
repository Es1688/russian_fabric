from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from max_news_bot.max_api.client import MaxBadResponseError, MaxClient
from max_news_bot.max_api.errors import MaxApiError, MaxRateLimitError, classify_error

_TOKEN = "test-max-token"
_CHAT_ID = 4242
_MID = 12345


class FakeMaxServer:
    """Local aiohttp server impersonating the MAX Bot API."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        # when set, POST /messages fails with (status, raw body) instead
        self.send_error: tuple[int, str] | None = None
        # overrides the /messages success payload (mid shape tests)
        self.send_payload: dict[str, Any] | None = None
        # overrides the /updates response payload verbatim
        self.updates_payload: dict[str, Any] | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        async def post_messages(request: web.Request) -> web.Response:
            self._record("POST", "/messages", request, body=await request.json())
            if self.send_error is not None:
                status, text = self.send_error
                return web.Response(
                    status=status, text=text, content_type="text/plain"
                )
            payload = self.send_payload or {
                "message": {"body": {"mid": _MID, "seq": 1}},
                "success": True,
            }
            return web.json_response(payload)

        async def delete_messages(request: web.Request) -> web.Response:
            self._record("DELETE", "/messages", request)
            # success with an empty body, as the real API may reply
            return web.Response(status=200)

        async def get_updates(request: web.Request) -> web.Response:
            self._record("GET", "/updates", request)
            if self.updates_payload is not None:
                return web.json_response(self.updates_payload)
            if "marker" not in request.query:
                return web.json_response(
                    {"updates": [{"update_type": "message_created"}], "marker": 9001}
                )
            return web.json_response({"updates": [], "marker": 9002})

        async def get_me(request: web.Request) -> web.Response:
            self._record("GET", "/me", request)
            return web.json_response(
                {"user_id": 777, "name": "Test Bot", "is_bot": True}
            )

        async def patch_commands(request: web.Request) -> web.Response:
            self._record("PATCH", "/me/commands", request, body=await request.json())
            return web.json_response({"success": True})

        app = web.Application()
        app.router.add_post("/messages", post_messages)
        app.router.add_delete("/messages", delete_messages)
        app.router.add_get("/updates", get_updates)
        app.router.add_get("/me", get_me)
        app.router.add_patch("/me/commands", patch_commands)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        return f"http://127.0.0.1:{port}"

    def _record(
        self, method: str, path: str, request: web.Request, body: Any = None
    ) -> None:
        # every MAX call must carry the RAW token, never "Bearer ..."
        assert request.headers.get("Authorization") == _TOKEN
        self.requests.append(
            {
                "method": method,
                "path": path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "json": body,
            }
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def max_server():
    server = FakeMaxServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def make_client(base_url: str, **overrides: Any) -> MaxClient:
    params: dict[str, Any] = {
        "token": _TOKEN,
        "base_url": base_url,
        "session": None,
    }
    params.update(overrides)
    return MaxClient(**params)


async def test_send_message_returns_mid_and_correct_request(max_server) -> None:
    server, base_url = max_server
    text = "Привет, <b>MAX</b>!"

    mid = await make_client(base_url).send_message(_CHAT_ID, text)

    assert mid == _MID
    request = server.requests[-1]
    assert request["method"] == "POST" and request["path"] == "/messages"
    assert request["headers"]["Authorization"] == _TOKEN
    assert request["query"]["chat_id"] == str(_CHAT_ID)
    assert "disable_link_preview" not in request["query"]
    assert request["json"] == {"text": text, "format": "html", "notify": True}


async def test_send_message_format_and_link_preview_params(max_server) -> None:
    server, base_url = max_server

    await make_client(base_url).send_message(
        _CHAT_ID, "plain **max**", fmt="markdown", disable_link_preview=True
    )

    request = server.requests[-1]
    assert request["query"]["disable_link_preview"] == "true"
    assert request["json"]["format"] == "markdown"
    assert request["json"]["text"] == "plain **max**"
    assert request["json"]["notify"] is True


@pytest.mark.parametrize(
    ("payload", "expected_mid"),
    [
        ({"body": {"mid": 777}}, 777),
        ({"message": {"mid": 888}}, 888),
        ({"message": {"body": {"mid": _MID, "seq": 1}}, "success": True}, _MID),
    ],
)
async def test_send_message_mid_shapes(
    max_server, payload: dict[str, Any], expected_mid: int
) -> None:
    server, base_url = max_server
    server.send_payload = payload

    mid = await make_client(base_url).send_message(_CHAT_ID, "x")

    assert mid == expected_mid


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},
        {"message": {"body": {"mid": "12345"}}},
        {"body": {"mid": None}},
    ],
)
async def test_send_message_without_int_mid_raises(max_server, payload) -> None:
    server, base_url = max_server
    server.send_payload = payload

    with pytest.raises(MaxBadResponseError):
        await make_client(base_url).send_message(_CHAT_ID, "x")


async def test_rate_limit_raises_dedicated_error(max_server) -> None:
    server, base_url = max_server
    server.send_error = (
        429,
        '{"code": "too.many.requests", "message": "rate exceeded"}',
    )

    with pytest.raises(MaxRateLimitError) as exc_info:
        await make_client(base_url).send_message(_CHAT_ID, "x")

    assert exc_info.value.status == 429
    assert exc_info.value.code == "too.many.requests"
    assert exc_info.value.message == "rate exceeded"


async def test_bad_request_error_parses_code_and_message(max_server) -> None:
    server, base_url = max_server
    server.send_error = (
        400,
        '{"code": "validation.failed", "message": "text too long"}',
    )

    with pytest.raises(MaxApiError) as exc_info:
        await make_client(base_url).send_message(_CHAT_ID, "x")

    assert not isinstance(exc_info.value, MaxRateLimitError)
    assert exc_info.value.status == 400
    assert exc_info.value.code == "validation.failed"
    assert exc_info.value.message == "text too long"
    text = str(exc_info.value)
    assert "400" in text and "validation.failed" in text and "text too long" in text


async def test_non_json_error_body_keeps_code_none(max_server) -> None:
    server, base_url = max_server
    server.send_error = (502, "gateway exploded")

    with pytest.raises(MaxApiError) as exc_info:
        await make_client(base_url).send_message(_CHAT_ID, "x")

    assert exc_info.value.status == 502
    assert exc_info.value.code is None
    assert exc_info.value.message == "gateway exploded"


async def test_non_json_error_body_truncated_to_200(max_server) -> None:
    server, base_url = max_server
    server.send_error = (500, "E" * 500)

    with pytest.raises(MaxApiError) as exc_info:
        await make_client(base_url).send_message(_CHAT_ID, "x")

    assert len(exc_info.value.message) == 200


async def test_updates_pass_marker_and_join_types(max_server) -> None:
    server, base_url = max_server
    client = make_client(base_url)

    updates, next_marker = await client.get_updates(
        types=["message_created", "bot_started"]
    )

    assert next_marker == 9001
    assert updates == [{"update_type": "message_created"}]
    first = server.requests[-1]
    assert first["method"] == "GET" and first["path"] == "/updates"
    assert "marker" not in first["query"]
    assert first["query"]["types"] == "message_created,bot_started"
    assert first["query"]["timeout"] == "30"
    assert first["query"]["limit"] == "100"

    updates2, next_marker2 = await client.get_updates(
        next_marker, types=["message_created"], timeout=5, limit=50
    )

    second = server.requests[-1]
    assert second["query"]["marker"] == "9001"
    assert second["query"]["types"] == "message_created"
    assert second["query"]["timeout"] == "5"
    assert second["query"]["limit"] == "50"
    assert updates2 == [] and next_marker2 == 9002


async def test_updates_empty_poll_returns_none_marker(max_server) -> None:
    server, base_url = max_server
    server.updates_payload = {"updates": []}

    updates, next_marker = await make_client(base_url).get_updates(marker=5)

    assert updates == []
    assert next_marker is None


async def test_updates_garbage_marker_is_none(max_server) -> None:
    server, base_url = max_server
    server.updates_payload = {"updates": [], "marker": "not-a-cursor"}

    _, next_marker = await make_client(base_url).get_updates(marker=5)

    assert next_marker is None


async def test_delete_message_sends_id_and_tolerates_empty_body(max_server) -> None:
    server, base_url = max_server

    await make_client(base_url).delete_message(_MID)  # must not raise

    request = server.requests[-1]
    assert request["method"] == "DELETE" and request["path"] == "/messages"
    assert request["query"]["message_id"] == str(_MID)


async def test_get_me_returns_dict(max_server) -> None:
    server, base_url = max_server

    me = await make_client(base_url).get_me()

    assert me == {"user_id": 777, "name": "Test Bot", "is_bot": True}
    assert server.requests[-1]["path"] == "/me"


async def test_patch_commands_posts_commands_body(max_server) -> None:
    server, base_url = max_server
    commands = [{"command": "/status", "description": "Show bot status"}]

    await make_client(base_url).patch_commands(commands)  # must not raise

    request = server.requests[-1]
    assert request["method"] == "PATCH" and request["path"] == "/me/commands"
    assert request["json"] == {"commands": commands}


async def test_injected_session_is_not_closed(max_server) -> None:
    server, base_url = max_server
    async with aiohttp.ClientSession() as session:
        client = MaxClient(token=_TOKEN, session=session, base_url=base_url)

        await client.get_me()

        assert not session.closed
    assert server.requests[-1]["path"] == "/me"


@pytest.mark.parametrize(
    "exc",
    [
        MaxRateLimitError(429, "too.many.requests", "rate exceeded"),
        MaxApiError(500, "internal", "boom"),
        MaxApiError(404, "not.found", "no chat"),
        MaxApiError(0, None, "bad body"),
        aiohttp.ClientConnectionError("connection reset"),
        asyncio.TimeoutError(),
        OSError("network down"),
        RuntimeError("whatever"),
    ],
)
def test_infra_errors(exc: Exception) -> None:
    assert classify_error(exc) == "infra"


def test_bad_request_is_content() -> None:
    assert classify_error(MaxApiError(400, "validation.failed", "text too long")) == "content"


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _RecordingSession:
    """Duck-typed session capturing request() kwargs."""

    def __init__(self, status: int = 200, body: str = "{}") -> None:
        self._status = status
        self._body = body
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse(self._status, self._body)


async def test_updates_total_timeout_exceeds_poll_timeout() -> None:
    session = _RecordingSession(body='{"updates": [], "marker": 1}')
    client = MaxClient(token=_TOKEN, session=session)

    await client.get_updates(timeout=30)

    assert session.calls[0]["timeout"].total == 40


async def test_plain_requests_use_constructor_timeout() -> None:
    session = _RecordingSession(body="{}")
    client = MaxClient(token=_TOKEN, session=session, timeout=7)

    await client.get_me()

    assert session.calls[0]["timeout"].total == 7
