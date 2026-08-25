from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "NHCogs"
PACKAGE_PATH = ROOT / PACKAGE_NAME
GITHUBTICKETS_PACKAGE_NAME = f"{PACKAGE_NAME}.githubtickets"
GITHUBTICKETS_PACKAGE_PATH = PACKAGE_PATH / "githubtickets"


def _load_scheduler_module():
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package

    githubtickets_package = sys.modules.get(GITHUBTICKETS_PACKAGE_NAME)
    if githubtickets_package is None:
        githubtickets_package = types.ModuleType(GITHUBTICKETS_PACKAGE_NAME)
        githubtickets_package.__path__ = [str(GITHUBTICKETS_PACKAGE_PATH)]
        sys.modules[GITHUBTICKETS_PACKAGE_NAME] = githubtickets_package

    try:
        return importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.scheduler")
    except (ImportError, ModuleNotFoundError):
        return None


scheduler_module = _load_scheduler_module()


class FakeDeadlineSource:
    def __init__(self, deadlines: dict[int, datetime]) -> None:
        self.deadlines = deadlines
        self.nearest_queried = asyncio.Event()
        self.nearest_query_count = 0

    async def nearest_deadline(self):
        self.nearest_query_count += 1
        self.nearest_queried.set()
        return min(self.deadlines.values(), default=None)

    async def due_ticket_ids(self, now):
        return tuple(ticket_id for ticket_id, deadline in self.deadlines.items() if deadline <= now)


@unittest.skipIf(scheduler_module is None, "GitHub Tickets scheduler is not implemented yet")
class DeadlineSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_earlier_deadline_interrupts_current_sleep(self):
        source = FakeDeadlineSource({1: datetime.now(timezone.utc) + timedelta(minutes=5)})
        processed = asyncio.Event()

        async def on_due(ticket_id):
            source.deadlines.pop(ticket_id)
            processed.set()

        scheduler = scheduler_module.DeadlineScheduler(source, on_due)
        scheduler.start()
        try:
            await asyncio.wait_for(source.nearest_queried.wait(), timeout=0.5)
            source.deadlines[1] = datetime.now(timezone.utc)
            scheduler.wake()
            await asyncio.wait_for(processed.wait(), timeout=0.5)
        finally:
            await scheduler.close()

    async def test_restart_reads_overdue_and_future_deadlines_from_the_same_source(self):
        now = datetime.now(timezone.utc)
        source = FakeDeadlineSource({1: now - timedelta(seconds=1), 2: now + timedelta(seconds=0.05)})
        processed = []
        first_processed = asyncio.Event()
        second_processed = asyncio.Event()

        async def on_due(ticket_id):
            processed.append(ticket_id)
            source.deadlines.pop(ticket_id)
            (first_processed if ticket_id == 1 else second_processed).set()

        first_scheduler = scheduler_module.DeadlineScheduler(source, on_due)
        first_scheduler.start()
        try:
            await asyncio.wait_for(first_processed.wait(), timeout=0.5)
        finally:
            await first_scheduler.close()

        second_scheduler = scheduler_module.DeadlineScheduler(source, on_due)
        second_scheduler.start()
        try:
            await asyncio.wait_for(second_processed.wait(), timeout=0.5)
        finally:
            await second_scheduler.close()

        self.assertEqual(processed, [1, 2])

    async def test_one_failing_due_callback_does_not_stop_later_tickets(self):
        overdue = datetime.now(timezone.utc) - timedelta(seconds=1)
        source = FakeDeadlineSource({1: overdue, 2: overdue})
        calls = []
        later_processed = asyncio.Event()

        async def on_due(ticket_id):
            calls.append(ticket_id)
            if ticket_id == 1:
                raise RuntimeError("controlled callback failure")
            source.deadlines.clear()
            later_processed.set()

        scheduler = scheduler_module.DeadlineScheduler(source, on_due)
        with self.assertLogs(scheduler_module.log, level="ERROR"):
            scheduler.start()
            try:
                await asyncio.wait_for(later_processed.wait(), timeout=0.5)
            finally:
                await scheduler.close()

        self.assertEqual(calls, [1, 2])

    async def test_start_is_idempotent_and_close_is_clean(self):
        source = FakeDeadlineSource({})

        async def on_due(_ticket_id):
            self.fail("no ticket was due")

        scheduler = scheduler_module.DeadlineScheduler(source, on_due)
        scheduler.start()
        scheduler.start()
        await asyncio.wait_for(source.nearest_queried.wait(), timeout=0.5)

        self.assertEqual(source.nearest_query_count, 1)
        await scheduler.close()
        await scheduler.close()

    async def test_background_query_exception_is_observed(self):
        queried = asyncio.Event()

        class FailingSource:
            async def nearest_deadline(self):
                queried.set()
                raise RuntimeError("controlled query failure")

            async def due_ticket_ids(self, _now):
                return ()

        async def on_due(_ticket_id):
            self.fail("no ticket was due")

        scheduler = scheduler_module.DeadlineScheduler(FailingSource(), on_due)
        with self.assertLogs(scheduler_module.log, level="ERROR") as captured:
            scheduler.start()
            await asyncio.wait_for(queried.wait(), timeout=0.5)
            await asyncio.sleep(0)
            await scheduler.close()

        self.assertTrue(any("deadline scheduler failed" in message for message in captured.output))
