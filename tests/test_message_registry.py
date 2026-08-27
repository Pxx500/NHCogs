import importlib
import inspect
import sqlite3
import sys
import types
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.storage_loader import load_shared_storage

PACKAGE_NAME = "_honeypot_message_registry_tests"
PACKAGE_PATH = Path(__file__).resolve().parents[1] / "NHCogs" / "honeypot"


def load_message_registry_module():
    load_shared_storage()
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package
    try:
        return importlib.import_module(f"{PACKAGE_NAME}.message_registry")
    except ModuleNotFoundError as error:
        if error.name == f"{PACKAGE_NAME}.message_registry":
            raise AssertionError("the message registry interface is missing") from error
        raise


class MessageRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "message_registry.sqlite"

    def registry(self):
        module = load_message_registry_module()
        return module.MessageRegistry(self.database_path)

    def record(
        self,
        message_id,
        *,
        guild_id=10,
        channel_id=20,
        author_id=30,
        minute=0,
        pinned=False,
        fingerprint=None,
        created_at=None,
    ):
        module = load_message_registry_module()
        return module.MessageRecord(
            message_id=message_id,
            guild_id=guild_id,
            channel_id=channel_id,
            author_id=author_id,
            created_at=created_at or datetime(2026, 7, 30, 8, minute, tzinfo=timezone.utc),
            pinned=pinned,
            author_kind="member",
            fingerprint=fingerprint,
        )

    async def test_initialize_creates_versioned_registry_with_required_indexes(self):
        registry = self.registry()

        await registry.initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        self.assertEqual(version, 1)
        self.assertIn("idx_observed_messages_channel", indexes)
        self.assertIn("idx_observed_messages_author", indexes)

    async def test_duplicate_observation_persists_once_across_registry_restart(self):
        module = load_message_registry_module()
        registry = module.MessageRegistry(self.database_path)
        await registry.initialize()
        record = module.MessageRecord(
            message_id=900,
            guild_id=10,
            channel_id=20,
            author_id=30,
            created_at=datetime(2026, 7, 30, 8, 15, tzinfo=timezone.utc),
            pinned=False,
            author_kind="member",
            fingerprint="fingerprint",
        )

        await registry.observe(record)
        await registry.observe(record)
        restarted = module.MessageRegistry(self.database_path)

        persisted = await restarted.recent_by_author(10, 30)

        self.assertEqual(persisted, (record,))

    async def test_channel_selection_is_scoped_bounded_and_skips_pins(self):
        registry = self.registry()
        await registry.initialize()
        records = (
            self.record(100, minute=1),
            self.record(200, minute=2, pinned=True),
            self.record(300, minute=3),
            self.record(400, minute=4),
            self.record(500, channel_id=99, minute=5),
            self.record(600, guild_id=11, minute=6),
        )
        for record in records:
            await registry.observe(record)

        selected = await registry.recent_in_channel(
            10,
            20,
            limit=2,
            before_message_id=400,
        )

        self.assertEqual(selected, (records[2], records[0]))

    async def test_channel_range_uses_snowflake_bounds_retention_and_pin_state(self):
        registry = self.registry()
        await registry.initialize()
        self.assertIn(
            "after_message_id",
            inspect.signature(registry.recent_in_channel).parameters,
        )
        records = (
            self.record(100, minute=1),
            self.record(200, minute=2),
            self.record(300, minute=3, pinned=True),
            self.record(400, minute=4),
            self.record(500, minute=5),
            self.record(350, channel_id=21, minute=3),
        )
        for record in records:
            await registry.observe(record)
        cutoff = datetime(2026, 7, 30, 8, 1, 30, tzinfo=timezone.utc)

        selected = await registry.recent_in_channel(
            10,
            20,
            limit=1001,
            after_message_id=100,
            before_message_id=500,
            since_utc=cutoff,
        )
        boundary = await registry.get_in_channel(
            10,
            20,
            200,
            since_utc=cutoff,
        )
        expired_boundary = await registry.get_in_channel(
            10,
            20,
            100,
            since_utc=cutoff,
        )

        self.assertEqual(selected, (records[3], records[1]))
        self.assertEqual(boundary, records[1])
        self.assertIsNone(expired_boundary)

    async def test_author_selection_supports_window_exclusion_limit_and_pins(self):
        registry = self.registry()
        await registry.initialize()
        records = (
            self.record(100, minute=1),
            self.record(200, channel_id=21, minute=2),
            self.record(300, channel_id=22, minute=3, pinned=True),
            self.record(400, channel_id=23, minute=4),
            self.record(500, author_id=31, minute=5),
        )
        for record in records:
            await registry.observe(record)

        selected = await registry.recent_by_author(
            10,
            30,
            limit=2,
            since_utc=datetime(2026, 7, 30, 8, 1, 30, tzinfo=timezone.utc),
            exclude_message_id=400,
        )

        self.assertEqual(selected, (records[1],))

    async def test_matching_channel_count_counts_distinct_channels_in_window(self):
        registry = self.registry()
        await registry.initialize()
        records = (
            self.record(100, channel_id=20, minute=1, fingerprint="same"),
            self.record(200, channel_id=20, minute=2, fingerprint="same"),
            self.record(300, channel_id=21, minute=3, fingerprint="same"),
            self.record(400, channel_id=22, minute=4, fingerprint="other"),
            self.record(500, channel_id=23, author_id=31, minute=5, fingerprint="same"),
        )
        for record in records:
            await registry.observe(record)

        count = await registry.matching_channel_count(
            10,
            30,
            "same",
            since_utc=datetime(2026, 7, 30, 8, 1, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(count, 2)

    async def test_pin_updates_and_forgetting_remove_only_requested_records(self):
        registry = self.registry()
        await registry.initialize()
        records = (
            self.record(100),
            self.record(200, channel_id=21),
            self.record(300, guild_id=11, channel_id=22),
            self.record(400, guild_id=11, channel_id=23, author_id=31),
            self.record(500, guild_id=12, channel_id=24, author_id=32),
        )
        for record in records:
            await registry.observe(record)

        await registry.set_pinned(100, True)
        self.assertEqual(await registry.recent_by_author(10, 30), (records[1],))
        await registry.set_pinned(100, False)
        await registry.forget(200)
        await registry.forget_many((100,))
        await registry.forget_channel(11, 22)
        await registry.forget_user(31)
        await registry.forget_guild(12)

        self.assertEqual(await registry.recent_by_author(10, 30), ())
        self.assertEqual(await registry.recent_by_author(11, 30), ())
        self.assertEqual(await registry.recent_by_author(11, 31), ())
        self.assertEqual(await registry.recent_by_author(12, 32), ())

    async def test_prune_removes_records_before_cutoff_and_reports_count(self):
        registry = self.registry()
        await registry.initialize()
        old = self.record(
            100,
            created_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
        boundary = self.record(
            200,
            created_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )
        for record in (old, boundary):
            await registry.observe(record)

        removed = await registry.prune(datetime(2026, 7, 16, tzinfo=timezone.utc))

        self.assertEqual(removed, 1)
        self.assertEqual(await registry.recent_by_author(10, 30), (boundary,))

    async def test_initialize_rejects_schema_from_newer_software(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA user_version = 2")

        with self.assertRaisesRegex(ValueError, "newer than supported"):
            await self.registry().initialize()


if __name__ == "__main__":
    unittest.main()
