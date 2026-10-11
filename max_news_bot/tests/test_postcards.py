"""Postcard bank policy tests (plan section 12).

Selection (least shown, image-carrying records only), image_ref
resolution (concrete ``bank:<type>/<id>`` vs a bare type name), and the
image file loading rules (extension whitelist, no path traversal).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from max_news_bot.bank.postcards import (
    IMAGE_METADATA_KEY,
    PostcardError,
    has_image,
    is_concrete_ref,
    load_postcard_bytes,
    postcard_ref,
    resolve_postcard,
    select_postcard,
)
from max_news_bot.core.models import ContentBankItem
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import ContentBankRepository


@pytest.fixture
async def bank(tmp_path: Path) -> ContentBankRepository:
    path = tmp_path / "max_news_bot.db"
    await Database(path).initialize()
    return ContentBankRepository(path)


def card(
    text: str,
    *,
    image: str | None = "cards/morning.jpg",
    show_count: int = 0,
    months: tuple[int, ...] = (),
    review_status: str = "approved",
) -> ContentBankItem:
    metadata = {IMAGE_METADATA_KEY: image} if image else {}
    return ContentBankItem(
        id=None,
        item_type="postcard",
        text=text,
        metadata=metadata,
        review_status=review_status,
        show_count=show_count,
        months=months,
    )


async def add(bank: ContentBankRepository, item: ContentBankItem) -> int:
    return await bank.add(item)


# --- has_image / postcard_ref / is_concrete_ref -------------------------------------


def test_has_image_detects_metadata_reference() -> None:
    assert has_image(card("x"))
    assert not has_image(card("x", image=None))
    assert not has_image(card("x", image="   "))


def test_postcard_ref_format_and_unclaimed_error() -> None:
    assert postcard_ref(ContentBankItem(id=7, item_type="postcard", text="x")) == (
        "bank:postcard/7"
    )
    with pytest.raises(PostcardError):
        postcard_ref(card("x"))  # id=None: not from the repository yet


def test_is_concrete_ref() -> None:
    assert is_concrete_ref("bank:postcard/12")
    assert not is_concrete_ref("postcard")
    assert not is_concrete_ref("bank:postcard/not-a-number")


# --- selection ----------------------------------------------------------------------


async def test_select_postcard_picks_least_shown_with_image(
    bank: ContentBankRepository,
) -> None:
    first = await add(bank, card("Первая открытка", show_count=0))
    await add(bank, card("Вторая открытка", show_count=5))
    await add(bank, card("Третья без картинки", image=None))

    item = await select_postcard(bank, "postcard")

    assert item is not None and item.id == first


async def test_select_postcard_none_when_stock_empty(
    bank: ContentBankRepository,
) -> None:
    await add(bank, card("Только без картинки", image=None))

    assert await select_postcard(bank, "postcard") is None
    assert await select_postcard(bank, "unknown_type") is None


async def test_select_postcard_honors_seasonality(
    bank: ContentBankRepository,
) -> None:
    await add(bank, card("Зимняя открытка", months=(12, 1)))
    summer = await add(bank, card("Летняя открытка", months=(6, 7)))

    in_july = await select_postcard(bank, "postcard", month=7)

    assert in_july is not None and in_july.id == summer
    # no month given: seasonal cards must not be filtered away
    any_time = await select_postcard(bank, "postcard")
    assert any_time is not None and any_time.id is not None


async def test_select_postcard_skips_unreviewed(bank: ContentBankRepository) -> None:
    await add(bank, card("Черновик", review_status="pending"))

    assert await select_postcard(bank, "postcard") is None


# --- image_ref resolution --------------------------------------------------------------


async def test_resolve_postcard_concrete_ref_loads_record(
    bank: ContentBankRepository,
) -> None:
    card_id = await add(bank, card("Утренняя открытка"))

    item = await resolve_postcard(bank, f"bank:postcard/{card_id}")

    assert item is not None and item.id == card_id


async def test_resolve_postcard_concrete_ref_verifies_type(
    bank: ContentBankRepository,
) -> None:
    phrase_id = await add(bank, card("Фраза без картинки", image=None))

    assert await resolve_postcard(bank, f"bank:postcard/{phrase_id}") is None
    assert await resolve_postcard(bank, "bank:postcard/99999") is None
    assert await resolve_postcard(bank, "bank:fact/1") is None


async def test_resolve_postcard_bare_name_selects_least_shown(
    bank: ContentBankRepository,
) -> None:
    first = await add(bank, card("Первая", show_count=0))
    await add(bank, card("Вторая", show_count=3))

    item = await resolve_postcard(bank, "postcard")

    assert item is not None and item.id == first


# --- image file loading ------------------------------------------------------------------


async def test_load_postcard_bytes_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "morning.jpg").write_bytes(b"jpeg-bytes")

    data, filename, content_type = await load_postcard_bytes(
        ContentBankItem(
            id=1,
            item_type="postcard",
            text="x",
            metadata={IMAGE_METADATA_KEY: "cards/morning.jpg"},
        ),
        tmp_path,
    )

    assert data == b"jpeg-bytes"
    assert filename == "morning.jpg"
    assert content_type == "image/jpeg"


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("card.PNG", "image/png"),
        ("card.gif", "image/gif"),
        ("card.tiff", "image/tiff"),
        ("card.bmp", "image/bmp"),
        ("card.heic", "image/heic"),
    ],
)
async def test_load_postcard_bytes_content_types(
    tmp_path: Path, suffix: str, expected: str
) -> None:
    path = tmp_path / suffix
    path.write_bytes(b"x")

    *_, content_type = await load_postcard_bytes(
        ContentBankItem(
            id=1,
            item_type="postcard",
            text="x",
            metadata={IMAGE_METADATA_KEY: suffix},
        ),
        tmp_path,
    )

    assert content_type == expected


@pytest.mark.parametrize(
    "image_ref_value",
    ["../secrets.png", "/etc/passwd.png", "cards/../../escape.jpg"],
)
async def test_load_postcard_bytes_rejects_escape(
    tmp_path: Path, image_ref_value: str
) -> None:
    item = ContentBankItem(
        id=1,
        item_type="postcard",
        text="x",
        metadata={IMAGE_METADATA_KEY: image_ref_value},
    )

    with pytest.raises(PostcardError, match="escapes"):
        await load_postcard_bytes(item, tmp_path)


async def test_load_postcard_bytes_rejects_unknown_extension(
    tmp_path: Path,
) -> None:
    (tmp_path / "card.exe").write_bytes(b"x")

    with pytest.raises(PostcardError, match="not a MAX image type"):
        await load_postcard_bytes(
            ContentBankItem(
                id=1,
                item_type="postcard",
                text="x",
                metadata={IMAGE_METADATA_KEY: "card.exe"},
            ),
            tmp_path,
        )


async def test_load_postcard_bytes_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PostcardError, match="cannot read"):
        await load_postcard_bytes(
            ContentBankItem(
                id=1,
                item_type="postcard",
                text="x",
                metadata={IMAGE_METADATA_KEY: "cards/absent.jpg"},
            ),
            tmp_path,
        )


async def test_load_postcard_bytes_without_reference(tmp_path: Path) -> None:
    with pytest.raises(PostcardError, match="no image reference"):
        await load_postcard_bytes(
            ContentBankItem(id=1, item_type="postcard", text="x", metadata={}),
            tmp_path,
        )
