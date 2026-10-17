"""Image generation protocol shared by all providers (plan §3.1)."""

from __future__ import annotations

from typing import Protocol

# Hard cap on decoded image bytes for every provider: downloads are streamed
# and aborted past it so a huge response cannot exhaust process memory.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ImageGenerationError(Exception):
    """Image generation failed; ``kind`` follows the factory taxonomy.

    "config" — wrong key or exhausted balance (HTTP 401/402/403): retrying
    is pointless and the caller must alert; "infra" — network errors, 429,
    5xx, timeouts and malformed responses.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ImageGenerator(Protocol):
    async def generate(self, prompt: str) -> bytes: ...
