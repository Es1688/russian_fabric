"""Media upload orchestration for the MAX Bot API (plan section 12).

Two-step scheme of the platform (research report section 1.1):
``POST /uploads?type=image`` returns a CDN upload url, the file is POSTed
there as multipart/form-data with a single ``data`` field, and the
attachment token extracted from the responses is attached to the message.
``MaxMediaUploader`` composes the three client calls; retry policy for
``attachment.not.ready`` (the server processes uploads asynchronously)
belongs to the publisher that sends the message.
"""

from __future__ import annotations

from typing import Any

from max_news_bot.max_api.client import (
    MaxBadResponseError,
    MaxClient,
    extract_upload_token,
)
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

UPLOAD_TYPE_IMAGE = "image"
# Images are allowed up to 50 MB / 7680x7680 px; the default client timeout
# (30 s) is too tight for a big upload on a slow uplink.
IMAGE_UPLOAD_TIMEOUT_SECONDS = 120


class MaxMediaUploader:
    """Uploads one media file and returns its attachment token."""

    def __init__(self, client: MaxClient) -> None:
        self._client = client

    async def upload_image(
        self, data: bytes, filename: str, content_type: str
    ) -> str:
        """Upload image bytes; returns the mediafile token for /messages.

        The /uploads answer for images carries only the url, the token
        appears in the upload response (``photos.photoIds.token``); for
        audio/video the token is already in the /uploads answer - the
        merged dict is checked in both places. The doc example for image
        keeps the Authorization header on the CDN host, so it is sent."""
        info = await self._client.get_upload_url(UPLOAD_TYPE_IMAGE)
        upload_url = _upload_url_from_info(info)
        logger.debug(
            "media_upload_start bytes=%s filename=%s", len(data), filename
        )
        uploaded = await self._client.upload_media(
            upload_url,
            data,
            filename,
            content_type,
            send_auth=True,
            total_timeout=IMAGE_UPLOAD_TIMEOUT_SECONDS,
        )
        token = extract_upload_token(_merged(info, uploaded))
        logger.debug("media_upload_done token_len=%s", len(token))
        return token


def _upload_url_from_info(info: dict[str, Any]) -> str:
    url = info.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    # the client's _parse_object already rejected non-object bodies; a
    # missing/relative url means the API shape changed - fail typed, not
    # with a confusing request error later
    raise MaxBadResponseError(f"/uploads response has no upload url: {info!r}")


def _merged(*parts: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge upload responses; later keys win (upload > /uploads)."""
    merged: dict[str, Any] = {}
    for part in parts:
        merged.update(part)
    return merged
