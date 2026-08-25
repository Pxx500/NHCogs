from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from typing import Protocol

log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeadlineSource(Protocol):
    async def nearest_deadline(self) -> datetime | None: ...

    async def due_ticket_ids(self, now: datetime) -> Sequence[int]: ...


class DeadlineScheduler:
    def __init__(
        self,
        deadlines: DeadlineSource,
        on_due: Callable[[int], Awaitable[None]],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._deadlines = deadlines
        self._on_due = on_due
        self._clock = clock
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="github-tickets-deadlines")
        self._task.add_done_callback(self._observe_task)

    def wake(self) -> None:
        self._wake_event.set()

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _run(self) -> None:
        while True:
            self._wake_event.clear()
            deadline = await self._deadlines.nearest_deadline()
            if deadline is None:
                await self._wake_event.wait()
                continue

            delay = max(0.0, (deadline - self._clock()).total_seconds())
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                else:
                    continue

            callback_failed = False
            for ticket_id in await self._deadlines.due_ticket_ids(self._clock()):
                try:
                    await self._on_due(ticket_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    callback_failed = True
                    log.exception("GitHub Tickets deadline callback failed for ticket %s", ticket_id)

            if callback_failed:
                await asyncio.sleep(1)

    @staticmethod
    def _observe_task(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.error(
                "GitHub Tickets deadline scheduler failed",
                exc_info=(type(error), error, error.__traceback__),
            )
