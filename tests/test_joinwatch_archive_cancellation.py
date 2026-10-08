"""Cancelled archive writes must settle before optional user context is scrubbed."""

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import mock

import tests.test_joinwatch_verification as verification_fixtures
from tests.harness import _isolated_honeypot_modules


class JoinWatchArchiveCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_config_archive_cannot_restore_removed_user_context(self):
        await self._assert_cancelled_archive(sqlite=False)

    async def test_cancelled_sqlite_archive_cannot_restore_removed_user_context(self):
        await self._assert_cancelled_archive(sqlite=True)

    async def _assert_cancelled_archive(self, *, sqlite):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            runtime = verification_fixtures._runtime(hp)
            state = hp.joinwatch_state
            entry = runtime.raw["joinwatch_pending_roles"]["20"]
            entry["history"] = {
                "captured_at": runtime.now.isoformat(), "history_origin": "enrollment",
                "profile": {"username": "remove-me"}, "activity": {"messages": 5},
            }
            if sqlite:
                self.assertTrue(runtime.cog._case_store.replace_from_config(10, {
                    "pending_role": runtime.raw["joinwatch_pending_roles"],
                    "pending_assignment": {}, "verified": {},
                }))
            entered, release, writer_finished = Event(), Event(), Event()
            original_save = runtime.cog._case_store.save_verification_history

            def blocked_save(record, events):
                entered.set()
                try:
                    if not release.wait(timeout=10):
                        raise AssertionError("test archive writer was not released")
                    original_save(record, events)
                finally:
                    writer_finished.set()

            member_lock = state.member_lock(runtime.cog, 10, 20)

            async def archive_under_member_lock():
                async with member_lock:
                    return await runtime.owner._archive(runtime.member.guild, 20, entry)

            archive = None
            scrub = None
            try:
                with mock.patch.object(runtime.cog._case_store, "save_verification_history",
                                       side_effect=blocked_save):
                    archive = asyncio.create_task(archive_under_member_lock())
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    archive.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(runtime.cog._joinwatch_member_locks[(10, 20)].locked())
                    self.assertFalse(archive.done())
                    scrub = asyncio.create_task(runtime.owner.redact_user_context(20))
                    await asyncio.sleep(0)
                    self.assertFalse(scrub.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await archive
                    await scrub
                await self._assert_context_scrubbed(runtime, state, entry)
            finally:
                release.set()
                pending = tuple(task for task in (archive, scrub) if task is not None)
                await asyncio.gather(*pending, return_exceptions=True)
                if entered.is_set():
                    self.assertTrue(await asyncio.to_thread(writer_finished.wait, 10))
                await runtime.owner.close()

    async def _assert_context_scrubbed(self, runtime, state, entry):
        retained = await state.read_row(runtime.cog, runtime.member.guild, 20, "pending_role")
        self.assertEqual(retained["incident_id"], "incident")
        self.assertEqual(retained["role_id"], 51)
        self.assertEqual(retained["challenge"], entry["challenge"])
        self.assertIsNone(retained["history"]["profile"])
        self.assertIsNone(retained["history"]["activity"])
        history = await runtime.owner.export_history(10)
        self.assertEqual(history["incidents"][0]["user_id"], "20")
        self.assertIsNone(history["incidents"][0]["profile"])
        self.assertIsNone(history["incidents"][0]["activity"])
        self.assertIn(runtime.role, runtime.member.roles)
