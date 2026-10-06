from __future__ import annotations

from typing import Protocol


class Alerter(Protocol):
    async def send_alert(self, text: str) -> None: ...

    async def send_recovery(self, text: str) -> None: ...
