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
