"""File-based kill switch.

Presence of `/var/lib/bot/KILL` (override path with --kill-file) flips
the bot to HALTED within 5 seconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.logging_setup import log
from src.state.db import Database
from src.state.models import SystemStatusEnum

FlattenCallback = Callable[[str], Awaitable[None]]


class KillSwitch:
    def __init__(
        self,
        db: Database,
        path: str = "/var/lib/bot/KILL",
        on_flatten: FlattenCallback | None = None,
        poll_seconds: int = 5,
    ):
        self.db = db
        self.path = Path(path)
        self.on_flatten = on_flatten
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            if self.path.exists():
                log.error("killswitch.triggered", path=str(self.path))
                await self.db.set_status(
                    SystemStatusEnum.HALTED, reason=f"killswitch file: {self.path}"
                )
                if self.on_flatten:
                    await self.on_flatten("killswitch file")
                return  # one-shot
            await asyncio.sleep(self.poll_seconds)
