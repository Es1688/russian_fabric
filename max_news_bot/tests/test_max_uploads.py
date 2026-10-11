"""MAX media upload tests (plan section 12, research report section 1).

Covers the client extensions (POST /uploads, the CDN multipart upload,
``attachments`` in POST /messages), the token extraction nuance (image
tokens arrive as ``photos.photoIds.token`` only after the upload), and
the two-step ``MaxMediaUploader`` flow against a local mock server.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from max_news_bot.max_api.client import (
    MaxBadResponseError,
    MaxClient,
    extract_upload_token,
)
from max_news_bot.max_api.errors import MaxApiError, is_attachment_not_ready
from max_news_bot.max_api.uploads import MaxMediaUploader

_TOKEN = "test-max-token"
_CHAT_ID = 4242
_MEDIAFILE_TOKEN = "mediafile.token.abc123"


class FakeUploadServer:
    """Local aiohttp server impersonating /uploads, the CDN host and
    POST /messages with attachments."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        # (status, body) the CDN upload fails with instead of succeeding
        self.upload_error: tuple[int, str] | None = None
        # CDN answer body override (token location tests)
        self.upload_payload: dict[str, Any] | None = None
        # /uploads answer body override
        self.uploads_payload: dict[str, Any] | None = None
        self.base_url = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        base = self

        async def post_uploads(request: web.Request) -> web.Response:
            base._record("POST", "/uploads", request)
            assert request.headers.get("Authorization") == _TOKEN, (
                "/uploads must carry the RAW token"
            )
            payload = base.uploads_payload or {
                "url": f"{base.base_url}/cdn/upload"
            }
            return web.json_response(payload)

        async def post_cdn_upload(request: web.Request) -> web.Response:
            # the multipart body is asserted as raw bytes: field name,
            # filename and the file content are all visible there
            base._record("POST", "/cdn/upload", request, raw=await request.read())
            if base.upload_error is not None:
                status, text = base.upload_error
                return web.Response(status=status, text=text)
            payload = base.upload_payload or {
                "photos": {"photoIds": {"token": _MEDIAFILE_TOKEN}}
            }
            return web.json_response(payload)

        async def post_cdn_empty(request: web.Request) -> web.Response:
            # audio-style success: 200 with an empty body
            base._record("POST", "/cdn/empty", request, raw=await request.read())
            return web.Response(status=200, text="")

        async def post_messages(request: web.Request) -> web.Response:
            base._record("POST", "/messages", request, body=await request.json())
            return web.json_response(
                {"message": {"body": {"mid": "mid.700001:1"}}, "success": True}
            )

        app = web.Application()
        app.router.add_post("/uploads", post_uploads)
        app.router.add_post("/cdn/upload", post_cdn_upload)
        app.router.add_post("/cdn/empty", post_cdn_empty)
        app.router.add_post("/messages", post_messages)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert runner.addresses, "test server did not bind"
        port = runner.addresses[0][1]
        self._runner = runner
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    def _record(
        self,
        method: str,
        path: str,
        request: web.Request,
        body: Any = None,
        raw: bytes | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "method": method,
            "path": path,
            "query": dict(request.query),
            "headers": dict(request.headers),
        }
        if body is not None:
            entry["json"] = body
        if raw is not None:
            entry["raw"] = raw
        self.requests.append(entry)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def upload_server():
    server = FakeUploadServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


def make_client(base_url: str) -> MaxClient:
    return MaxClient(token=_TOKEN, base_url=base_url)


def multipart_of(request: dict[str, Any]) -> str:
    """The raw multipart body as latin-1 text (headers inside are ascii)."""
    return request["raw"].decode("latin-1")


# --- POST /uploads ---------------------------------------------------------------


async def test_get_upload_url_sends_type_and_raw_token(upload_server) -> None:
    server, base_url = upload_server

    info = await make_client(base_url).get_upload_url("image")

    request = server.requests[-1]
    assert request["method"] == "POST" and request["path"] == "/uploads"
    assert request["query"] == {"type": "image"}
    assert request["headers"]["Authorization"] == _TOKEN
    assert info == {"url": f"{server.base_url}/cdn/upload"}


async def test_get_upload_url_with_audio_style_token(upload_server) -> None:
    server, base_url = upload_server
    server.uploads_payload = {
        "url": f"{server.base_url}/cdn/upload",
        "token": "early-token",
    }

    info = await make_client(base_url).get_upload_url("audio")

    assert info["token"] == "early-token"


# --- CDN upload ------------------------------------------------------------------


async def test_upload_media_posts_multipart_data_field(upload_server) -> None:
    server, base_url = upload_server

    answer = await make_client(base_url).upload_media(
        f"{server.base_url}/cdn/upload",
        b"\x89PNG-fake-bytes",
        "morning.png",
        "image/png",
    )

    request = server.requests[-1]
    assert request["method"] == "POST" and request["path"] == "/cdn/upload"
    # single multipart field "data" carrying the file
    assert "multipart/form-data" in request["headers"]["Content-Type"]
    body = multipart_of(request)
    assert 'name="data"' in body
    assert 'filename="morning.png"' in body
    assert b"\r\n\r\n\x89PNG-fake-bytes\r\n" in request["raw"]
    # default: no Authorization to the CDN host (audio/video/file examples)
    assert "Authorization" not in request["headers"]
    assert answer == {"photos": {"photoIds": {"token": _MEDIAFILE_TOKEN}}}


async def test_upload_media_send_auth_flag_adds_header(upload_server) -> None:
    server, base_url = upload_server

    await make_client(base_url).upload_media(
        f"{server.base_url}/cdn/upload",
        b"jpg-bytes",
        "card.jpg",
        "image/jpeg",
        send_auth=True,
    )

    request = server.requests[-1]
    assert request["headers"].get("Authorization") == _TOKEN


async def test_upload_media_empty_body_returns_empty_dict(upload_server) -> None:
    server, base_url = upload_server

    answer = await make_client(base_url).upload_media(
        f"{server.base_url}/cdn/empty", b"x", "a.mp3", "audio/mpeg"
    )

    assert answer == {}


async def test_upload_media_non_json_body_raises(upload_server) -> None:
    server, base_url = upload_server
    server.upload_error = (200, "not-json-at-all")

    with pytest.raises(MaxBadResponseError, match="not JSON"):
        await make_client(base_url).upload_media(
            f"{server.base_url}/cdn/upload", b"x", "a.png", "image/png"
        )


async def test_upload_media_error_status_raises_typed(upload_server) -> None:
    server, base_url = upload_server
    server.upload_error = (
        400,
        '{"code": "forbidden.extension", "message": "File extension is forbidden"}',
    )

    with pytest.raises(MaxApiError) as exc_info:
        await make_client(base_url).upload_media(
            f"{server.base_url}/cdn/upload",
            b"x",
            "a.exe",
            "application/octet-stream",
        )

    assert exc_info.value.status == 400
    assert exc_info.value.code == "forbidden.extension"


# --- token extraction -------------------------------------------------------------


def test_extract_upload_token_from_top_level() -> None:
    assert extract_upload_token({"url": "u", "token": "tok"}) == "tok"


def test_extract_upload_token_from_photos_photo_ids() -> None:
    assert extract_upload_token({"photos": {"photoIds": {"token": "tok"}}}) == "tok"


@pytest.mark.parametrize(
    "data", [{}, {"url": "u"}, {"token": ""}, {"photos": {"photoIds": {}}}]
)
def test_extract_upload_token_missing_raises(data: dict) -> None:
    with pytest.raises(MaxBadResponseError, match="no attachment token"):
        extract_upload_token(data)


# --- attachments in POST /messages --------------------------------------------------


async def test_send_message_with_attachments_body(upload_server) -> None:
    server, base_url = upload_server
    attachments = [{"type": "image", "payload": {"token": _MEDIAFILE_TOKEN}}]

    mid = await make_client(base_url).send_message(
        _CHAT_ID, "Доброе утро!", attachments=attachments
    )

    request = server.requests[-1]
    assert request["path"] == "/messages"
    assert request["json"]["text"] == "Доброе утро!"
    assert request["json"]["attachments"] == attachments
    assert mid == "mid.700001:1"


async def test_send_message_without_attachments_keeps_body(upload_server) -> None:
    server, base_url = upload_server

    await make_client(base_url).send_message(_CHAT_ID, "x")

    assert "attachments" not in server.requests[-1]["json"]


# --- attachment.not.ready classification ---------------------------------------------


def test_is_attachment_not_ready_by_code() -> None:
    assert is_attachment_not_ready(
        MaxApiError(400, "errors.process.attachment.file.not.processed", "wait")
    )
    assert is_attachment_not_ready(MaxApiError(400, "attachment.not.ready", "wait"))


def test_is_attachment_not_ready_by_message_substring() -> None:
    assert is_attachment_not_ready(MaxApiError(400, None, "attachment is not ready yet"))


def test_is_attachment_not_ready_negative() -> None:
    assert not is_attachment_not_ready(MaxApiError(400, "validation.failed", "x"))
    assert not is_attachment_not_ready(MaxApiError(500, None, "boom"))
    assert not is_attachment_not_ready(ValueError("not a max error"))


# --- MaxMediaUploader: the full two-step flow -----------------------------------------


async def test_uploader_image_flow_returns_token(upload_server) -> None:
    server, base_url = upload_server
    uploader = MaxMediaUploader(make_client(base_url))

    token = await uploader.upload_image(b"image-bytes", "night.jpg", "image/jpeg")

    assert token == _MEDIAFILE_TOKEN
    uploads_request, cdn_request = server.requests[-2:]
    assert uploads_request["path"] == "/uploads"
    assert uploads_request["query"] == {"type": "image"}
    assert cdn_request["path"] == "/cdn/upload"
    # the doc example for image keeps Authorization on the CDN host
    assert cdn_request["headers"].get("Authorization") == _TOKEN
    assert b"\r\n\r\nimage-bytes\r\n" in cdn_request["raw"]


async def test_uploader_finds_early_token_too(upload_server) -> None:
    server, base_url = upload_server
    # audio/video shape: the token already in the /uploads answer
    server.uploads_payload = {
        "url": f"{server.base_url}/cdn/upload",
        "token": "early-token",
    }

    token = await MaxMediaUploader(make_client(base_url)).upload_image(
        b"x", "a.png", "image/png"
    )

    assert token == "early-token"


async def test_uploader_without_url_raises_bad_response(upload_server) -> None:
    server, base_url = upload_server
    server.uploads_payload = {"token": "but-no-url"}

    with pytest.raises(MaxBadResponseError, match="no upload url"):
        await MaxMediaUploader(make_client(base_url)).upload_image(
            b"x", "a.png", "image/png"
        )


async def test_uploader_cdn_failure_propagates(upload_server) -> None:
    server, base_url = upload_server
    server.upload_error = (502, "cdn exploded")

    with pytest.raises(MaxApiError) as exc_info:
        await MaxMediaUploader(make_client(base_url)).upload_image(
            b"x", "a.png", "image/png"
        )

    assert exc_info.value.status == 502
