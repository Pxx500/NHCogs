from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "NHCogs" / "nhmisc" / "bot_proxy_store.py"
SPEC = importlib.util.spec_from_file_location("nhmisc_bot_proxy_store_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Bot Proxy store")
bot_proxy_store = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot_proxy_store
SPEC.loader.exec_module(bot_proxy_store)

BotProxyStore = bot_proxy_store.BotProxyStore
ActiveSessionRecord = bot_proxy_store.ActiveSessionRecord
CharacterExists = bot_proxy_store.CharacterExists
ProxySender = bot_proxy_store.ProxySender
StaleCharacterRevision = bot_proxy_store.StaleCharacterRevision


class BotProxyCharacterStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = BotProxyStore(Path(self.tempdir.name) / "bot_proxy.sqlite")
        await self.store.initialize()

    async def test_character_round_trip_is_normalized_and_guild_scoped(self) -> None:
        created = await self.store.create_character(
            guild_id=10,
            preset_name="  Narrator  ",
            display_name="The Narrator",
            avatar_bytes=b"avatar-one",
            avatar_media_type="image/png",
            moderator_id=20,
        )

        self.assertEqual(created.preset_name, "Narrator")
        self.assertEqual(created.avatar_bytes, b"avatar-one")
        self.assertEqual(created.revision, 1)
        self.assertEqual(
            await self.store.get_character(10, "nArRaToR"),
            created,
        )
        self.assertIsNone(await self.store.get_character(11, "Narrator"))

        other_guild = await self.store.create_character(
            guild_id=11,
            preset_name="Narrator",
            display_name="Another Narrator",
            avatar_bytes=None,
            avatar_media_type=None,
            moderator_id=21,
        )
        self.assertEqual(other_guild.guild_id, 11)
        self.assertEqual(
            await self.store.list_character_summaries(10),
            (bot_proxy_store.CharacterPresetSummary("Narrator", "The Narrator"),),
        )

    async def test_duplicate_name_is_rejected_case_insensitively(self) -> None:
        await self.store.create_character(
            guild_id=10,
            preset_name="Narrator",
            display_name="The Narrator",
            avatar_bytes=None,
            avatar_media_type=None,
            moderator_id=20,
        )

        with self.assertRaises(CharacterExists):
            await self.store.create_character(
                guild_id=10,
                preset_name="NARRATOR",
                display_name="Replacement",
                avatar_bytes=None,
                avatar_media_type=None,
                moderator_id=20,
            )

    async def test_user_deletion_erases_owned_avatars_and_redacts_shared_updater(self):
        for guild_id in (10, 11):
            await self.store.create_character(
                guild_id=guild_id, preset_name="Owned", display_name="Owned",
                avatar_bytes=b"private avatar", avatar_media_type="image/png",
                moderator_id=20,
            )
        shared = await self.store.create_character(
            guild_id=10, preset_name="Shared", display_name="Shared",
            avatar_bytes=b"shared avatar", avatar_media_type="image/png",
            moderator_id=21,
        )
        await self.store.update_character(
            guild_id=10, preset_name="Shared", expected_revision=shared.revision,
            new_preset_name="Shared", display_name="Shared",
            avatar_bytes=shared.avatar_bytes, avatar_media_type=shared.avatar_media_type,
            moderator_id=20,
        )
        await self.store.delete_user_data(20)
        await self.store.delete_user_data(20)
        restarted = BotProxyStore(self.store._path)
        await restarted.initialize()
        for guild_id in (10, 11):
            self.assertIsNone(await restarted.get_character(guild_id, "Owned"))
        retained = await restarted.get_character(10, "Shared")
        self.assertEqual(retained.created_by, 21)
        self.assertEqual(retained.updated_by, bot_proxy_store.DELETED_USER_ID)
        self.assertEqual(retained.avatar_bytes, b"shared avatar")
        self.assertNotIn(b"private avatar", self.store._path.read_bytes())

    async def test_update_requires_current_revision(self) -> None:
        created = await self.store.create_character(
            guild_id=10,
            preset_name="Narrator",
            display_name="The Narrator",
            avatar_bytes=b"old-avatar",
            avatar_media_type="image/png",
            moderator_id=20,
        )

        updated = await self.store.update_character(
            guild_id=10,
            preset_name="Narrator",
            expected_revision=created.revision,
            new_preset_name="Guide",
            display_name="The Guide",
            avatar_bytes=b"new-avatar",
            avatar_media_type="image/jpeg",
            moderator_id=21,
        )

        self.assertEqual(updated.preset_name, "Guide")
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.updated_by, 21)
        self.assertIsNone(await self.store.get_character(10, "Narrator"))

        with self.assertRaises(StaleCharacterRevision):
            await self.store.update_character(
                guild_id=10,
                preset_name="Guide",
                expected_revision=created.revision,
                new_preset_name="Guide",
                display_name="Stale edit",
                avatar_bytes=None,
                avatar_media_type=None,
                moderator_id=20,
            )

    async def test_delete_requires_current_revision(self) -> None:
        created = await self.store.create_character(
            guild_id=10,
            preset_name="Narrator",
            display_name="The Narrator",
            avatar_bytes=None,
            avatar_media_type=None,
            moderator_id=20,
        )

        deleted = await self.store.delete_character(
            guild_id=10,
            preset_name="Narrator",
            expected_revision=created.revision,
        )

        self.assertEqual(deleted, created)
        self.assertIsNone(await self.store.get_character(10, "Narrator"))


class BotProxyLifecycleStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = BotProxyStore(Path(self.tempdir.name) / "bot_proxy.sqlite")
        await self.store.initialize()

    async def test_session_and_owned_webhook_round_trip(self) -> None:
        session = ActiveSessionRecord(
            session_id="session-one",
            guild_id=10,
            moderator_id=20,
            launcher_channel_id=30,
            launcher_message_id=40,
            thread_id=50,
            dashboard_message_id=60,
        )

        await self.store.record_active_session(session)
        await self.store.remember_webhook(10, 70, 80)

        self.assertEqual(await self.store.list_active_sessions(), (session,))
        self.assertEqual(await self.store.get_webhook_id(10, 70), 80)
        await self.store.remove_active_session(session.session_id)
        await self.store.forget_webhook(10, 70)
        self.assertEqual(await self.store.list_active_sessions(), ())
        self.assertIsNone(await self.store.get_webhook_id(10, 70))

    async def test_recorded_message_preserves_identity_snapshot_and_sent_event(
        self,
    ) -> None:
        recorded = await self.store.record_message(
            guild_id=10,
            channel_id=30,
            message_id=40,
            moderator_id=20,
            sender=ProxySender.CHARACTER,
            webhook_id=50,
            content="Original content",
            reply_message_id=None,
            character_preset_name="Narrator",
            character_display_name="The Narrator",
            avatar_sha256="abc123",
        )

        self.assertEqual(recorded.content, "Original content")
        self.assertEqual(recorded.character_display_name, "The Narrator")
        self.assertEqual(recorded.revision, 1)
        events = await self.store.list_message_events(10, 30, 40)
        self.assertEqual(
            [(event.action, event.revision, event.content) for event in events],
            [("sent", 1, "Original content")],
        )

    async def test_user_deletion_preserves_message_linkage_and_redacts_all_attribution(self):
        for moderator_id in (20, 21):
            await self.store.record_active_session(ActiveSessionRecord(
                session_id=f"session-{moderator_id}", guild_id=10,
                moderator_id=moderator_id, launcher_channel_id=30,
                launcher_message_id=moderator_id + 100, thread_id=moderator_id + 200,
                dashboard_message_id=moderator_id + 300,
            ))
            await self.store.record_message(
                guild_id=10, channel_id=30, message_id=moderator_id + 400,
                moderator_id=moderator_id, sender=ProxySender.CHARACTER,
                webhook_id=50, content="Server-owned message", reply_message_id=90,
                character_preset_name="Guide", character_display_name="Guide",
                avatar_sha256="digest",
            )
        await self.store.remember_webhook(10, 30, 50)
        with self.store._connection() as connection, connection:
            connection.execute(
                "UPDATE bot_proxy_messages SET edited_by = ?, deleted_by = ?",
                (20, 20),
            )
        await self.store.delete_user_data(20)
        sessions = await self.store.list_active_sessions()
        self.assertEqual([session.moderator_id for session in sessions], [21])
        self.assertEqual(await self.store.get_webhook_id(10, 30), 50)
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM bot_proxy_messages ORDER BY message_id"
            ).fetchall()
        self.assertEqual([row["moderator_id"] for row in rows], [0, 21])
        for row in rows:
            self.assertEqual(row["edited_by"], 0)
            self.assertEqual(row["deleted_by"], 0)
            self.assertEqual(row["content"], "Server-owned message")
            self.assertEqual(row["original_content"], "Server-owned message")
            self.assertEqual(row["reply_message_id"], 90)
            self.assertEqual(row["webhook_id"], 50)
        events = await self.store.list_message_events(10, 30, 420)
        self.assertEqual(events[0].moderator_id, 0)
        self.assertEqual(events[0].content, "Server-owned message")
        other_events = await self.store.list_message_events(10, 30, 421)
        self.assertEqual(other_events[0].moderator_id, 21)

if __name__ == "__main__":
    unittest.main()
