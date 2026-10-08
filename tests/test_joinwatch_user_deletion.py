import asyncio
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.harness import _isolated_honeypot_modules
from tests.test_joinwatch_live_state import _cog, _entry, _maps


class JoinWatchUserDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletion_after_export_erases_config_and_inactive_sqlite_rows(self):
        await self._assert_deletion_after_rollback("export_live_state")

    async def test_deletion_after_restore_erases_config_and_inactive_sqlite_rows(self):
        await self._assert_deletion_after_rollback("restore_live_backup")

    async def _assert_deletion_after_rollback(self, operation):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            state = hp.joinwatch_state
            kept = _entry(
                incident_id="kept", enrollment_moderator=30,
                completion_moderator="30", completion_reason="noted",
            )
            removed = _entry(incident_id="removed")
            cog = _cog(hp, directory, _maps(
                pending={"20": kept, "30": removed},
                assignments={"20": kept, "30": removed},
                verified={"30": {"incident_id": "verified"}},
            ))
            guild = SimpleNamespace(id=100)
            cog.bot.guilds = [guild]
            store = cog._case_store
            await state.cutover_guild(cog, guild.id)
            self.assertTrue(await getattr(state, operation)(cog, guild))
            # Older exports can leave these inactive rows on disk across an upgrade.
            for kind in ("pending_role", "pending_assignment"):
                store.upsert(guild.id, 20, kind, kept)
                store.upsert(guild.id, 30, kind, removed)
            store.upsert(guild.id, 30, "verified", {"incident_id": "verified"})
            backup = store.live_backup(guild.id)
            verification = importlib.import_module(f"{hp.__package__}.joinwatch_verification")
            owner = verification.JoinwatchVerification(cog)
            try:
                await owner.delete_user_data(30)
            finally:
                await owner.close()
            self.assertEqual(store.live_backup(guild.id), backup)
            for kind in ("verified", "pending_role", "pending_assignment"):
                self.assertIsNone(await state.read_row(cog, guild, 30, kind))
                self.assertIsNone(store.get(guild.id, 30, kind))
            for kind in ("pending_role", "pending_assignment"):
                self._assert_moderator_erased(await state.read_row(cog, guild, 20, kind))
                self._assert_moderator_erased(store.get(guild.id, 20, kind))
            await asyncio.to_thread(
                store.expire_live_backups, datetime.now(timezone.utc) + timedelta(days=8),
            )
            self.assertIsNone(store.live_backup(guild.id))
            await state.cutover_guild(cog, guild.id)
            renewed_backup = store.live_backup(guild.id)
            for entries in renewed_backup.values():
                self.assertNotIn("30", entries)
            self._assert_moderator_erased(renewed_backup["pending_role"]["20"])
            self.assertIsNone(store.get(guild.id, 30, "pending_role"))
            self._assert_moderator_erased(store.get(guild.id, 20, "pending_role"))

    def _assert_moderator_erased(self, row):
        self.assertEqual(row["incident_id"], "kept")
        self.assertIsNone(row["enrollment_moderator"])
        self.assertIsNone(row["completion_moderator"])
        self.assertIsNone(row["completion_reason"])

    async def test_failed_cutover_cleanup_cannot_keep_inactive_config_user_data(self):
        await self._assert_failed_cleanup_deletion(loaded=True)

    async def test_failed_cutover_cleanup_is_erased_for_an_unloaded_guild(self):
        await self._assert_failed_cleanup_deletion(loaded=False)

    async def _assert_failed_cleanup_deletion(self, *, loaded):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            state = hp.joinwatch_state
            kept = _entry(
                incident_id="kept", enrollment_moderator=30,
                completion_moderator="30", completion_reason="noted",
            )
            cog = _cog(hp, directory, _maps(
                pending={"20": kept, "30": _entry(incident_id="removed")},
                assignments={"20": kept, "30": _entry(incident_id="assignment")},
                verified={"30": {"incident_id": "verified"}},
            ))
            guild = SimpleNamespace(id=100)
            cog.bot.guilds = [guild] if loaded else []
            cog.config.fail_clear = True
            await state.cutover_live_state(cog)
            self.assertEqual(cog._case_store.cutover_source(guild.id), "sqlite")
            advanced = {**kept, "stage": 2}
            cog._case_store.upsert(guild.id, 20, "pending_role", advanced)
            cog.config.fail_clear = False
            backup = cog._case_store.live_backup(guild.id)
            verification = importlib.import_module(f"{hp.__package__}.joinwatch_verification")
            owner = verification.JoinwatchVerification(cog)
            try:
                await owner.delete_user_data(30)
            finally:
                await owner.close()
            self.assertEqual(cog._case_store.rows_for_member(guild.id, 30), {})
            config_maps = cog.config.guilds[guild.id]
            for config_key in state._KIND_CONFIG.values():
                self.assertNotIn("30", config_maps[config_key])
            for kind in ("pending_role", "pending_assignment"):
                self._assert_moderator_erased(config_maps[state._KIND_CONFIG[kind]]["20"])
                self._assert_moderator_erased(cog._case_store.get(guild.id, 20, kind))
            self.assertEqual(cog._case_store.get(guild.id, 20, "pending_role")["stage"], 2)
            self.assertEqual(config_maps["joinwatch_pending_roles"]["20"]["stage"], 1)
            self.assertEqual(cog._case_store.live_backup(guild.id), backup)

    async def test_completed_export_does_not_retain_a_dormant_challenge(self):
        await self._assert_completed_rollback("export_live_state")

    async def test_completed_restore_does_not_retain_a_dormant_challenge(self):
        await self._assert_completed_rollback("restore_live_backup")

    async def test_reload_erases_old_inactive_rows_when_config_is_empty(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            cog = _cog(hp, directory, _maps())
            # An export from an older version can leave rows without a source marker.
            cog._case_store.upsert(100, 30, "pending_role", _entry())
            self.assertIsNone(cog._case_store.cutover_source(100))
            self.assertTrue(await hp.joinwatch_state.cutover_guild(cog, 100))
            self.assertEqual(cog._case_store.rows_for_member(100, 30), {})

    async def _assert_completed_rollback(self, operation):
        for outcome in ("manual", "cancelled"):
            with self.subTest(outcome=outcome):
                await self._complete_rollback(operation, outcome)

    async def _complete_rollback(self, operation, outcome):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            state = hp.joinwatch_state
            cog = _cog(hp, directory, _maps(pending={"30": _entry()}))
            guild = SimpleNamespace(id=100)
            await state.cutover_guild(cog, guild.id)
            await getattr(state, operation)(cog, guild)
            self.assertEqual(cog._case_store.rows_for_member(guild.id, 30), {})
            verification = importlib.import_module(f"{hp.__package__}.joinwatch_verification")
            owner = verification.JoinwatchVerification(cog)
            try:
                async with state.member_lock(cog, guild.id, 30):
                    entry = await state.read_row(cog, guild, 30, "pending_role")
                    self.assertTrue(await owner.finish(guild, 30, entry, outcome))
            finally:
                await owner.close()
            await asyncio.to_thread(
                cog._case_store.expire_live_backups,
                datetime.now(timezone.utc) + timedelta(days=8),
            )
            await state.cutover_guild(cog, guild.id)
            self.assertEqual(await state.rows_for_member(cog, guild, 30), {})
            self.assertEqual(cog._case_store.rows_for_member(guild.id, 30), {})
            self.assertIsNone(cog._case_store.live_backup(guild.id))

    async def test_empty_config_after_deletion_cannot_keep_a_dormant_incident(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
            state = hp.joinwatch_state
            cog = _cog(hp, directory, _maps(pending={"30": _entry()}))
            guild = SimpleNamespace(id=100)
            cog.bot.guilds = [guild]
            await state.cutover_guild(cog, guild.id)
            await state.export_live_state(cog, guild)
            verification = importlib.import_module(f"{hp.__package__}.joinwatch_verification")
            owner = verification.JoinwatchVerification(cog)
            try:
                await owner.delete_user_data(30)
            finally:
                await owner.close()
            await asyncio.to_thread(
                cog._case_store.expire_live_backups,
                datetime.now(timezone.utc) + timedelta(days=8),
            )
            await state.cutover_guild(cog, guild.id)
            self.assertIsNone(cog._case_store.live_backup(guild.id))
            self.assertEqual(cog._case_store.rows_for_member(guild.id, 30), {})
            self.assertEqual(await state.rows_for_member(cog, guild, 30), {})
