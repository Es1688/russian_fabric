"""MaxSlotPublisher tests (plan sections 10 and 12).

End-to-end against a local mock MAX server: the postcard flow
(/uploads -> CDN multipart upload -> /messages with the attachment
token), degradation rules (a broken card goes text-only, a transport
failure fails the publish as infra) and the two retry dimensions
(rate limit, attachment.not.ready).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from max_news_bot.core.models import ContentBankItem
from max_news_bot.max_api.client import MaxClient
from max_news_bot.publishers.slot import MaxSlotPublisher
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import ContentBankRepository

_TOKEN = "test-max-token"
_CHANNEL_ID = -1001234567890
_ADMINS = [111, 222]
_MEDIAFILE_TOKEN = "mediafile.token.abc123"
_IMAGE_BYTES = b"fake-jpeg-bytes"


class FakeSlotServer:
    """Local aiohttp server: /uploads, the CDN host and /messages."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        # scripted /messages failures, popped per request: (status, body)
        self.send_errors: list[tuple[int, str]] = []
        # (status, body) the CDN upload fails with instead of succeeding
        self.upload_error: tuple[int, str] | None = None
        self.base_url = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        base = self

        async def post_uploads(request: web.Request) -> web.Response:
            base._record("POST", "/uploads", request)
            return web.json_response({"url": f"{base.base_url}/cdn/upload"})

        async def post_cdn_upload(request: web.Request) -> web.Response:
            base._record("POST", "/cdn/upload", request, raw=await request.read())
            if base.upload_error is not None:
                status, text = base.upload_error
                return web.Response(status=status, text=text)
            return web.json_response(
                {"photos": {"photoIds": {"token": _MEDIAFILE_TOKEN}}}
            )

        async def post_messages(request: web.Request) -> web.Response:
            base._record("POST", "/messages", request, body=await request.json())
            if base.send_errors:
                status, text = base.send_errors.pop(0)
                return web.Response(status=status, text=text)
            return web.json_response(
                {"message": {"body": {"mid": "mid.700001:1"}}, "success": True}
            )

        app = web.Application()
        app.router.add_post("/uploads", post_uploads)
        app.router.add_post("/cdn/upload", post_cdn_upload)
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

    def messages_requests(self) -> list[dict[str, Any]]:
        return [entry for entry in self.requests if entry["path"] == "/messages"]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def slot_server():
    server = FakeSlotServer()
    base_url = await server.start()
    yield server, base_url
    await server.stop()


@pytest.fixture
async def bank(tmp_path: Path) -> ContentBankRepository:
    db_path = tmp_path / "max_news_bot.db"
    await Database(db_path).initialize()
    return ContentBankRepository(db_path)


@pytest.fixture
def postcards_dir(tmp_path: Path) -> Path:
    cards = tmp_path / "postcards"
    cards.mkdir()
    (cards / "morning.jpg").write_bytes(_IMAGE_BYTES)
    return cards


def make_publisher(
    base_url: str, bank: ContentBankRepository, postcards_dir: Path
) -> MaxSlotPublisher:
    publisher = MaxSlotPublisher(
        MaxClient(token=_TOKEN, base_url=base_url),
        list(_ADMINS),
        bank=bank,
        postcards_dir=postcards_dir,
    )
    # no real waiting in tests (rate-limit and not-ready pauses)
    publisher.SEND_PAUSE_SECONDS = 0.0
    publisher.ATTACHMENT_PAUSE_SECONDS = 0.0
    return publisher


def postcard_item(
    image: str | None = "morning.jpg", show_count: int = 0
) -> ContentBankItem:
    metadata = {"image": image} if image else {}
    return ContentBankItem(
        id=None,
        item_type="postcard",
        text="Доброе утро! Пусть день будет тёплым.",
        metadata=metadata,
        review_status="approved",
        show_count=show_count,
    )


# --- happy path -------------------------------------------------------------------


async def test_publish_with_postcard_full_flow(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    card_id = await bank.add(postcard_item())
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро! Пусть день будет тёплым.",
        chat_id=_CHANNEL_ID,
        image_ref=f"bank:postcard/{card_id}",
    )

    assert result.success and result.message_id == "mid.700001:1"
    uploads, cdn, message = server.requests[-3:]
    assert uploads["path"] == "/uploads" and uploads["query"] == {"type": "image"}
    assert uploads["headers"]["Authorization"] == _TOKEN
    assert cdn["path"] == "/cdn/upload"
    assert b"\r\n\r\n" + _IMAGE_BYTES + b"\r\n" in cdn["raw"]
    assert message["json"]["attachments"] == [
        {"type": "image", "payload": {"token": _MEDIAFILE_TOKEN}}
    ]
    assert message["query"]["chat_id"] == str(_CHANNEL_ID)
    # the concrete ref was marked shown by the pipeline, not here
    item = await bank.get(card_id)
    assert item is not None and item.show_count == 0


async def test_publish_without_image_ref_sends_plain_text(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Спокойной ночи.", chat_id=_CHANNEL_ID, image_ref=None
    )

    assert result.success
    # only the message call happened: no /uploads round trip
    assert [entry["path"] for entry in server.requests] == ["/messages"]
    assert "attachments" not in server.requests[-1]["json"]


async def test_publish_sanitizes_text(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    publisher = make_publisher(base_url, bank, postcards_dir)

    await publisher.publish_slot(
        "При температуре < 5 градусов", chat_id=_CHANNEL_ID
    )

    assert server.requests[-1]["json"]["text"] == (
        "При температуре &lt; 5 градусов"
    )


async def test_bare_type_ref_selects_least_shown_and_rotates(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    (postcards_dir / "night.jpg").write_bytes(b"night-bytes")
    first = await bank.add(postcard_item("morning.jpg", show_count=0))
    await bank.add(postcard_item("night.jpg", show_count=4))
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброй ночи.", chat_id=_CHANNEL_ID, image_ref="postcard"
    )

    assert result.success
    assert server.requests[-1]["json"]["attachments"][0]["payload"]["token"] == (
        _MEDIAFILE_TOKEN
    )
    # the publisher-selected card is spent, the other one is not
    selected = await bank.get(first)
    assert selected is not None and selected.show_count == 1


# --- degradation -------------------------------------------------------------------


async def test_unresolved_image_ref_degrades_to_text_only(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро.", chat_id=_CHANNEL_ID, image_ref="bank:postcard/99999"
    )

    assert result.success
    assert [entry["path"] for entry in server.requests] == ["/messages"]
    assert "attachments" not in server.requests[-1]["json"]


async def test_missing_card_file_degrades_to_text_only(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    # the record points at a file that is not on disk
    card_id = await bank.add(postcard_item(image="absent.jpg"))
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро.", chat_id=_CHANNEL_ID, image_ref=f"bank:postcard/{card_id}"
    )

    assert result.success
    assert [entry["path"] for entry in server.requests] == ["/messages"]
    assert "attachments" not in server.requests[-1]["json"]


async def test_upload_transport_failure_is_infra_without_message(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    server.upload_error = (502, "cdn exploded")
    card_id = await bank.add(postcard_item())
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро.", chat_id=_CHANNEL_ID, image_ref=f"bank:postcard/{card_id}"
    )

    assert not result.success
    assert result.error_kind == "infra"
    # never silently send the post without its card on a transport failure
    assert server.messages_requests() == []


async def test_empty_text_is_a_content_error(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot("   ", chat_id=_CHANNEL_ID)

    assert not result.success
    assert result.error_kind == "content"
    assert server.requests == []


# --- retries ------------------------------------------------------------------------


async def test_attachment_not_ready_is_retried_with_same_token(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    server.send_errors = [
        (
            400,
            '{"code": "errors.process.attachment.file.not.processed", '
            '"message": "attachment.not.ready"}',
        )
    ]
    card_id = await bank.add(postcard_item())
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро.", chat_id=_CHANNEL_ID, image_ref=f"bank:postcard/{card_id}"
    )

    assert result.success
    attempts = server.messages_requests()
    assert len(attempts) == 2
    # the SAME token is re-sent, no second upload happened
    assert attempts[0]["json"]["attachments"] == attempts[1]["json"]["attachments"]
    upload_calls = [
        entry for entry in server.requests if entry["path"] == "/uploads"
    ]
    assert len(upload_calls) == 1


async def test_attachment_not_ready_exhaustion_fails(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    server.send_errors = [
        (
            400,
            '{"code": "errors.process.attachment.file.not.processed", '
            '"message": "still processing"}',
        )
    ] * 10
    card_id = await bank.add(postcard_item())
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро.", chat_id=_CHANNEL_ID, image_ref=f"bank:postcard/{card_id}"
    )

    assert not result.success
    assert len(server.messages_requests()) == 4  # 1 send + 3 not-ready retries


async def test_rate_limit_is_retried(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    server.send_errors = [
        (429, '{"code": "too.many.requests", "message": "rate exceeded"}')
    ]
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot("Вечерний пост.", chat_id=_CHANNEL_ID)

    assert result.success and result.message_id == "mid.700001:1"
    assert len(server.messages_requests()) == 2


# --- dry-run previews -----------------------------------------------------------------


async def test_dry_run_previews_to_admins_with_card(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    card_id = await bank.add(postcard_item())
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро.",
        chat_id=_CHANNEL_ID,
        image_ref=f"bank:postcard/{card_id}",
        to_admins=True,
    )

    assert result.success and result.message_id is None
    attempts = server.messages_requests()
    # admin_ids hold user_id: DMs are addressed by user_id, no chat_id
    assert [entry["query"]["user_id"] for entry in attempts] == [
        str(admin) for admin in _ADMINS
    ]
    for entry in attempts:
        assert "chat_id" not in entry["query"]
        assert entry["json"]["text"].startswith("[DRY-RUN] ")
        assert entry["json"]["attachments"][0]["payload"]["token"] == (
            _MEDIAFILE_TOKEN
        )
    # one upload serves both admin previews
    upload_calls = [
        entry for entry in server.requests if entry["path"] == "/uploads"
    ]
    assert len(upload_calls) == 1


async def test_dry_run_all_admins_failing_is_infra(
    slot_server, bank: ContentBankRepository, postcards_dir: Path
) -> None:
    server, base_url = slot_server
    server.send_errors = [(403, '{"code": "forbidden", "message": "no dm"}')] * 5
    publisher = make_publisher(base_url, bank, postcards_dir)

    result = await publisher.publish_slot(
        "Доброе утро.", chat_id=_CHANNEL_ID, to_admins=True
    )

    assert not result.success and result.error_kind == "infra"
    assert "no admin preview delivered" in (result.error or "")
