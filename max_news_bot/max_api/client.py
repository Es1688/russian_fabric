from __future__ import annotations

import json
from typing import Any

import aiohttp

from max_news_bot.max_api.errors import MaxApiError, MaxRateLimitError
from max_news_bot.utils.logging import get_logger


logger = get_logger(__name__)

_MAX_ERROR_BODY_LEN = 200

_DEFAULT_BASE_URL = "https://platform-api2.max.ru"


class MaxClient:
    """Thin async client for the MAX Bot API (https://dev.max.ru).

    The API rejects message texts over 4000 chars; the limit is enforced by
    publishers/validators, not by this client.
    """

    def __init__(
        self,
        token: str,
        session: aiohttp.ClientSession | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = 30,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        # Shared process-wide session wins; otherwise one per call.
        self._session = session
        self._timeout = timeout

    async def get_me(self) -> dict:
        """GET /me: bot profile as a raw dict."""
        return _parse_object(await self._request("GET", "/me"), "/me")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        fmt: str = "html",
        disable_link_preview: bool = False,
    ) -> str:
        """POST /messages; returns the created message id (mid).

        mid is a string like "mid.<id>", not an int."""
        params: dict[str, Any] = {"chat_id": chat_id}
        if disable_link_preview:
            params["disable_link_preview"] = "true"
        body = await self._request(
            "POST",
            "/messages",
            params=params,
            json_body={"text": text, "format": fmt, "notify": True},
        )
        return _extract_mid(body)

    async def delete_message(self, message_id: str) -> None:
        """DELETE /messages; any 2xx (empty body included) is success."""
        await self._request(
            "DELETE", "/messages", params={"message_id": message_id}
        )

    async def get_updates(
        self,
        marker: int | None = None,
        *,
        timeout: int = 30,
        limit: int = 100,
        types: list[str] | None = None,
    ) -> tuple[list[dict], int | None]:
        """GET /updates long poll; returns (updates, next_marker).

        next_marker is None on empty polls (no cursor advanced) — the caller
        re-polls without a marker or keeps its last stored one."""
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        # the server holds the connection for up to `timeout` seconds: the
        # client total must exceed it or every slow poll dies as a timeout
        body = await self._request(
            "GET", "/updates", params=params, total_timeout=timeout + 10
        )
        data = _parse_object(body, "/updates")
        updates = data.get("updates")
        if not isinstance(updates, list):
            raise MaxBadResponseError('"updates" is missing or not a list')
        next_marker = data.get("marker")
        if isinstance(next_marker, bool) or not isinstance(next_marker, int):
            next_marker = None
        return updates, next_marker

    async def patch_commands(self, commands: list[dict]) -> None:
        """PATCH /me/commands: register the bot command list (best-effort)."""
        await self._request(
            "PATCH", "/me/commands", json_body={"commands": commands}
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        total_timeout: int | None = None,
    ) -> str:
        """One API call; returns the raw response body text.

        Raises MaxApiError/MaxRateLimitError on non-2xx; network and timeout
        exceptions propagate as-is (classify_error maps them later)."""
        # MAX auth is the RAW access token — NO "Bearer" prefix, unlike
        # OpenAI-style APIs; a prefixed header is rejected with 401
        headers = {"Authorization": self._token}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        logger.debug("max_api_request method=%s path=%s", method, path)
        client_timeout = aiohttp.ClientTimeout(
            total=self._timeout if total_timeout is None else total_timeout
        )
        url = f"{self._base_url}{path}"
        if self._session is not None:
            return await _send(
                self._session, method, url, params, json_body, headers, client_timeout
            )
        async with aiohttp.ClientSession() as session:
            return await _send(
                session, method, url, params, json_body, headers, client_timeout
            )


class MaxBadResponseError(MaxApiError):
    """2xx response whose body does not match the expected shape.

    status=0: there is no real HTTP status to report."""

    def __init__(self, message: str) -> None:
        super().__init__(status=0, code=None, message=message)


async def _send(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: aiohttp.ClientTimeout,
) -> str:
    async with session.request(
        method, url, params=params, json=json_body, headers=headers, timeout=timeout
    ) as response:
        body = await response.text()
        if not response.ok:
            raise _error_response(response.status, body)
        return body


def _parse_object(body: str, what: str) -> dict[str, Any]:
    """Parse a JSON object body; anything else is a bad response."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise MaxBadResponseError(f"{what} response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MaxBadResponseError(f"{what} response is not an object")
    return data


def _extract_mid(body: str) -> str:
    """Pull the message id out of a /messages response.

    The canonical shape is message.body.mid; older/simplified shapes are
    kept as fallbacks so an API tweak degrades to one typed error. mid is
    canonically a string like "mid.<id>" ((mid.)?[a-zA-Z0-9_-]+); a numeric
    mid from a simplified response is coerced to str. The sibling seq field
    is an int counter, NOT an id; it must never be picked here."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise MaxBadResponseError(f"/messages response is not JSON: {exc}") from exc
    for candidate in (
        _dig(data, "message", "body", "mid"),
        _dig(data, "body", "mid"),
        _dig(data, "message", "mid"),
    ):
        if isinstance(candidate, str):
            return candidate
        # bool is an int subclass: a stray true must not pass as an id
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return str(candidate)
    raise MaxBadResponseError("/messages response has no message id")


def _dig(data: Any, *keys: str) -> Any:
    """Walk nested dicts; None on any missing/blocked level."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _error_response(status: int, body: str) -> MaxApiError:
    """Build the typed error for a non-2xx response; body may be non-JSON."""
    code: str | None = None
    message = body[:_MAX_ERROR_BODY_LEN]
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("code"), str):
            code = data["code"]
        if isinstance(data.get("message"), str):
            message = data["message"]
    error_cls = MaxRateLimitError if status == 429 else MaxApiError
    return error_cls(status, code, message)
