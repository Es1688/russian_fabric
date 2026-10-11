from __future__ import annotations


class MaxApiError(Exception):
    """MAX Bot API call failed: non-2xx response or unusable body."""

    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(f"status={status} code={code!r} message={message}")
        self.status = status
        self.code = code
        self.message = message


class MaxRateLimitError(MaxApiError):
    """HTTP 429: rate limit hit (2 msg/sec per dialog, 30 rps global)."""


# Media processing on the MAX side is asynchronous: a message sent right
# after the upload can be rejected until the server has processed the file.
# Docs name both spellings (research report section 1.4).
_ATTACHMENT_NOT_READY_CODES = (
    "attachment.not.ready",
    "errors.process.attachment.file.not.processed",
)


def is_attachment_not_ready(exc: Exception) -> bool:
    """True when ``exc`` is the "file still processing" upload error.

    Callers should pause with a growing interval and re-send the same
    message; the token stays valid, no re-upload is needed."""
    if not isinstance(exc, MaxApiError):
        return False
    if exc.code in _ATTACHMENT_NOT_READY_CODES:
        return True
    # the live error shape is unconfirmed (research report section 4):
    # match the wording with dots/underscores normalized to spaces
    haystack = (
        f"{exc.code or ''} {exc.message}".lower().replace(".", " ").replace("_", " ")
    )
    return "not ready" in haystack or "not processed" in haystack


def classify_error(exc: Exception) -> str:
    """Map an exception to "infra" or "content", mirroring publishers/post.py:
    infra failures (network, rate limit, server errors) are retried for free,
    content ones mean the message itself is unusable and burns an attempt."""
    if isinstance(exc, MaxApiError):
        # only a malformed request (400) is the message's own fault; auth,
        # permissions and other 4xx are effectively configuration/infra
        return "content" if exc.status == 400 else "infra"
    # timeouts and any network/unknown failure
    return "infra"
