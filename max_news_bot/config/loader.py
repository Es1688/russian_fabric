"""Process configuration: env secrets plus the single contour's yaml.

The rubrics contour (``config/rubrics.py`` → ``rubrics.yaml``) is the only
publishing contour; this module only resolves the process-level env
(MAX credentials, admins, log level, database path) and delegates the
contour configuration with all its validations to ``load_rubrics_config``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from max_news_bot.config.rubrics import RubricsConfig, load_rubrics_config


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    channel_id: int  # MAX chat_id is int64
    admin_ids: list[int]
    log_level: str
    data_path: Path
    rubrics: RubricsConfig


def load_config() -> AppConfig:
    load_dotenv()

    bot_token = os.getenv("MAX_BOT_TOKEN", "").strip()
    channel_id_raw = os.getenv("MAX_CHANNEL_ID", "").strip()
    admin_raw = os.getenv("ADMIN_IDS", "").strip()
    log_level = os.getenv("LOG_LEVEL", "INFO").strip()

    if not bot_token or not channel_id_raw:
        raise ValueError("MAX_BOT_TOKEN and MAX_CHANNEL_ID are required")

    try:
        channel_id = int(channel_id_raw)
    except ValueError as exc:
        raise ValueError(
            f"MAX_CHANNEL_ID must be an integer chat id, got: {channel_id_raw!r}"
        ) from exc

    admin_ids = [
        int(value)
        for value in admin_raw.split(",")
        if value.strip().isdigit()
    ]

    rubrics = load_rubrics_config()

    if rubrics.dry_run and not admin_ids:
        raise ValueError(
            "ADMIN_IDS is required when the contour is in dry-run "
            "(previews are delivered to admins); set ADMIN_IDS or "
            "RUBRICS_DRY_RUN=false"
        )

    data_env = os.getenv("DATABASE_PATH", "").strip()
    if data_env:
        data_path = Path(data_env)
    else:
        data_path = Path(__file__).resolve().parents[1] / "data" / "max_news_bot.db"

    return AppConfig(
        bot_token=bot_token,
        channel_id=channel_id,
        admin_ids=admin_ids,
        log_level=log_level,
        data_path=data_path,
        rubrics=rubrics,
    )
