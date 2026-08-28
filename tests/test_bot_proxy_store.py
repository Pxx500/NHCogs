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
        self.assertEqual(await self.store.list_characters(10), (created,))

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

if __name__ == "__main__":
    unittest.main()
