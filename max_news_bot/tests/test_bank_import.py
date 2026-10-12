"""Tests for the operator bank import tool (scripts/bank_import.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from max_news_bot.scripts import bank_import
from max_news_bot.utils.db import Database
from max_news_bot.utils.repositories import ContentBankRepository


def write_seed(path: Path, items: list[dict]) -> Path:
    path.write_text(
        yaml.safe_dump({"items": items}, allow_unicode=True), encoding="utf-8"
    )
    return path


async def call(*argv: str) -> int:
    """Run the tool against parsed args (main() wraps run in asyncio.run)."""
    return await bank_import.run(bank_import.parse_args(list(argv)))


async def make_db(tmp_path: Path) -> Path:
    database = Database(tmp_path / "max_news_bot.db")
    await database.initialize()
    return database.path


@pytest.mark.asyncio
async def test_import_inserts_approved_items(tmp_path, capsys):
    db_path = await make_db(tmp_path)
    seed = write_seed(
        tmp_path / "seed.yaml",
        [
            {
                "type": "phrase",
                "text": "Доброе утро! Пусть день будет добрым.",
                "months": [1, 2, 12],
            },
            {"type": "fact", "text": "Факт дня без сезонности."},
        ],
    )

    code = await call("--file", str(seed), "--db", str(db_path))

    assert code == 0
    repo = ContentBankRepository(db_path)
    stock = await repo.count_available_by_type()
    assert stock == {"phrase": 1, "fact": 1}
    out = capsys.readouterr().out
    assert "2 inserted, 0 skipped, 2 total" in out

    # months survive as a seasonality tuple, status as approved
    items = await repo.list_available("phrase", month=None, event_date=None)
    assert items[0].months == (1, 2, 12)
    assert items[0].review_status == "approved"


@pytest.mark.asyncio
async def test_reimport_is_idempotent(tmp_path, capsys):
    db_path = await make_db(tmp_path)
    seed = write_seed(
        tmp_path / "seed.yaml",
        [{"type": "thought", "text": "Мысль вечера."}],
    )
    assert await call("--file", str(seed), "--db", str(db_path)) == 0
    capsys.readouterr()

    code = await call("--file", str(seed), "--db", str(db_path))

    assert code == 0
    out = capsys.readouterr().out
    assert "0 inserted, 1 skipped, 1 total" in out
    repo = ContentBankRepository(db_path)
    assert await repo.count_available_by_type() == {"thought": 1}


@pytest.mark.asyncio
async def test_event_date_stored(tmp_path):
    db_path = await make_db(tmp_path)
    seed = write_seed(
        tmp_path / "seed.yaml",
        [
            {"type": "event", "text": "В этот день...", "event_date": "05-09"},
            {"type": "event", "text": "Неверная дата", "event_date": "13-40"},
        ],
    )

    code = await call("--file", str(seed), "--db", str(db_path))

    assert code == 1
    repo = ContentBankRepository(db_path)
    # all-or-nothing: the valid record did not enter either
    assert await repo.count_available_by_type() == {}


def test_invalid_records_reported_without_import(tmp_path, capsys):
    seed = write_seed(
        tmp_path / "seed.yaml",
        [
            {"type": "Bad Type", "text": "..."},
            {"type": "fact", "text": ""},
            {"type": "fact", "text": "ok", "unknown_key": 1},
            {"type": "fact", "text": "ok2", "months": [0, 13]},
            "just a string",
        ],
    )

    code = bank_import.main(["--file", str(seed), "--db", str(tmp_path / "x.db")])

    assert code == 1
    err = capsys.readouterr().err
    assert "items[0]" in err
    assert "items[1]" in err
    assert "unknown keys" in err
    assert "months" in err
    assert "items[4]" in err


@pytest.mark.asyncio
async def test_postcard_requires_image_file(tmp_path):
    db_path = await make_db(tmp_path)
    (tmp_path / "postcards").mkdir()
    (tmp_path / "postcards" / "morning.jpg").write_bytes(b"jpg")
    seed = write_seed(
        tmp_path / "seed.yaml",
        [
            {
                "type": "postcard",
                "text": "Открытие с картинкой",
                "metadata": {"image": "morning.jpg"},
            },
            {
                "type": "postcard",
                "text": "Открытие без файла",
                "metadata": {"image": "missing.jpg"},
            },
        ],
    )

    code = await call("--file", str(seed), "--db", str(db_path))

    assert code == 1
    repo = ContentBankRepository(db_path)
    assert await repo.count_available_by_type() == {}


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(tmp_path, capsys):
    db_path = await make_db(tmp_path)
    seed = write_seed(
        tmp_path / "seed.yaml",
        [{"type": "phrase", "text": "Только проверка."}],
    )

    code = await call(
        "--file", str(seed), "--db", str(db_path), "--dry-run"
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "would insert [approved]" in out
    assert "1 inserted, 0 skipped, 1 total" in out
    repo = ContentBankRepository(db_path)
    assert await repo.count_available_by_type() == {}


@pytest.mark.asyncio
async def test_pending_flag_stages_for_review(tmp_path):
    db_path = await make_db(tmp_path)
    seed = write_seed(
        tmp_path / "seed.yaml",
        [{"type": "phrase", "text": "На рассмотрение."}],
    )

    code = await call(
        "--file", str(seed), "--db", str(db_path), "--pending"
    )

    assert code == 0
    repo = ContentBankRepository(db_path)
    # pending items are not part of the approved stock the grid draws from
    assert await repo.count_available_by_type() == {}
    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT review_status FROM content_bank WHERE item_type = 'phrase'"
        ).fetchone()[0]
    assert status == "pending"


@pytest.mark.asyncio
async def test_stock_report_flags_low_types(tmp_path, capsys):
    db_path = await make_db(tmp_path)
    seed = write_seed(
        tmp_path / "seed.yaml",
        [{"type": "phrase", "text": "Единственная фраза."}],
    )

    await call("--file", str(seed), "--db", str(db_path))

    out = capsys.readouterr().out
    assert "phrase: 1" in out
    assert "LOW" in out


def test_default_db_path_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "custom.db"))
    assert bank_import.default_db_path() == tmp_path / "custom.db"
    monkeypatch.delenv("DATABASE_PATH")
    expected = (
        Path(bank_import.__file__).parent.parent / "data" / "max_news_bot.db"
    )
    assert bank_import.default_db_path() == expected
