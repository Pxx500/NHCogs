import asyncio
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules
from tests.test_joinwatch_live_state import _cog, _entry, _maps


class JoinWatchCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_cutover_keeps_a_write_from_a_reloaded_cog(self):
        await self._assert_ordered("cutover_guild", "replace_from_config", sqlite=False)

    async def test_cancelled_export_keeps_a_later_write(self):
        await self._assert_ordered("export_live_state", "clear_cutover", sqlite=True)

    async def test_cancelled_restore_keeps_a_later_write(self):
        await self._assert_ordered("restore_live_backup", "clear_cutover", sqlite=True)

    async def test_cancelled_upsert_cannot_recreate_a_deleted_member(self):
        await self._assert_ordered("write_row", "upsert", sqlite=True, later_delete=True)

    async def test_cancelled_delete_cannot_remove_a_new_incident(self):
        await self._assert_ordered("delete_row", "delete", sqlite=True)

    async def _assert_ordered(self, operation, worker_name, *, sqlite, later_delete=False):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            state = hp.joinwatch_state
            cog = _cog(hp, directory, _maps(pending={"20": _entry()}))
            guild = SimpleNamespace(id=100)
            if sqlite:
                await state.cutover_guild(cog, guild.id)
            reloaded = SimpleNamespace(bot=cog.bot, config=cog.config, _case_store=cog._case_store)
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()
            release = threading.Event()
            original = getattr(cog._case_store, worker_name)

            def blocked(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(10):
                    raise RuntimeError("worker was not released")
                return original(*args, **kwargs)

            async def run_operation():
                if operation == "cutover_guild":
                    return await state.cutover_guild(cog, guild.id)
                if operation in ("export_live_state", "restore_live_backup"):
                    return await getattr(state, operation)(cog, guild)
                async with state.member_lock(cog, guild.id, 20):
                    if operation == "delete_row":
                        return await state.delete_row(cog, guild, 20, "pending_role")
                    return await state.write_row(cog, guild, 20, "pending_role", _entry(stage=2))

            async def later_operation():
                async with state.member_lock(reloaded, guild.id, 20):
                    if later_delete:
                        return await state.delete_row(reloaded, guild, 20, "pending_role")
                    return await state.write_row(
                        reloaded, guild, 20, "pending_role", _entry(incident_id="new-incident"),
                    )

            await self._cancel_while_blocked(
                state, cog, reloaded, worker_name, blocked,
                run_operation=run_operation, later_operation=later_operation,
                entered=entered, release=release,
            )
            await state.cutover_guild(reloaded, guild.id)
            retained = await state.read_row(reloaded, guild, 20, "pending_role")
            if later_delete:
                self.assertIsNone(retained)
            else:
                self.assertEqual(retained["incident_id"], "new-incident")

    async def _cancel_while_blocked(
        self, state, cog, reloaded, worker_name, blocked,
        *,
        run_operation, later_operation, entered, release,
    ):
        with mock.patch.object(cog._case_store, worker_name, side_effect=blocked):
            operation = asyncio.create_task(run_operation())
            later = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                operation.cancel()
                await asyncio.sleep(0)
                operation.cancel()
                await asyncio.sleep(0)
                later = asyncio.create_task(later_operation())
                await asyncio.sleep(0)
                self.assertFalse(operation.done())
                self.assertFalse(later.done())
                self.assertIs(state._source_lock(cog), state._source_lock(reloaded))
                self.assertTrue(state._source_lock(reloaded).locked())
            finally:
                release.set()
                tasks = [operation] if later is None else [operation, later]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertIs(results[1], True)

    async def test_cancellation_waits_for_a_failing_worker(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            entered = asyncio.Event()
            release = threading.Event()
            loop = asyncio.get_running_loop()

            def fail():
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(10):
                    raise RuntimeError("worker was not released")
                raise ValueError("disk failure")

            task = asyncio.create_task(hp.joinwatch_state.finish_thread(fail))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
                result = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
