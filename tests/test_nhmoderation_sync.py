import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.storage_loader import load_shared_storage

load_shared_storage()

from NHCogs.nhmoderation.history import NHModerationHistory  # noqa: E402
from NHCogs.nhmoderation.synchronization import (  # noqa: E402
    ModerationSynchronizer,
    SyncMode,
    modlog_observation,
    next_weekly_reconciliation,
)


class ModerationSynchronizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_sync_reuses_committed_cursors(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            audit_fetcher = mock.AsyncMock(
                side_effect=[
                    [SimpleNamespace(id=100, target=SimpleNamespace(id=1), user=SimpleNamespace(id=55), reason=None, created_at=datetime(2026, 8, 28, tzinfo=timezone.utc))],
                    [],
                    [],
                    [],
                ]
            )
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=audit_fetcher,
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )
            guild = SimpleNamespace(id=10)

            await synchronizer.synchronize(guild, SyncMode.INCREMENTAL)
            await synchronizer.synchronize(guild, SyncMode.INCREMENTAL)

            self.assertIsNone(audit_fetcher.await_args_list[0].kwargs["after_id"])
            self.assertEqual(audit_fetcher.await_args_list[2].kwargs["after_id"], 100)

    async def test_failed_fetch_does_not_advance_cursor(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            audit_fetcher = mock.AsyncMock(side_effect=RuntimeError("missing permissions"))
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=audit_fetcher,
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )

            with self.assertRaisesRegex(RuntimeError, "missing permissions"):
                await synchronizer.synchronize(SimpleNamespace(id=10), SyncMode.INCREMENTAL)

            state = await history.status(10)
            self.assertIsNone(state.audit_ban_cursor)
            self.assertIsNone(state.last_sync_at)

    async def test_weekly_sync_uses_overlap_without_snapshot(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            audit_fetcher = mock.AsyncMock(return_value=[])
            snapshot_fetcher = mock.AsyncMock(return_value=[])
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=audit_fetcher,
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=snapshot_fetcher,
                clock=lambda: datetime(2026, 8, 30, 4, 20, tzinfo=timezone.utc),
            )

            await synchronizer.synchronize(SimpleNamespace(id=10), SyncMode.WEEKLY)

            self.assertEqual(audit_fetcher.await_count, 2)
            self.assertIsNotNone(audit_fetcher.await_args_list[0].kwargs["after_id"])
            snapshot_fetcher.assert_not_awaited()

    async def test_repair_reads_snapshot(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            snapshot_fetcher = mock.AsyncMock(return_value=[])
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(return_value=[]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=snapshot_fetcher,
            )
            guild = SimpleNamespace(id=10)

            await synchronizer.synchronize(guild, SyncMode.REPAIR)

            snapshot_fetcher.assert_awaited_once_with(guild)

    async def test_bot_identity_is_resolved_when_sync_runs_after_startup(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            current_bot_id = 0
            entry = SimpleNamespace(
                id=100,
                target=SimpleNamespace(id=1),
                user=SimpleNamespace(id=999),
                reason=None,
                created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            )
            audit_fetcher = mock.AsyncMock(side_effect=[[entry], []])
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=lambda: current_bot_id,
                audit_fetcher=audit_fetcher,
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )
            current_bot_id = 999

            await synchronizer.synchronize(SimpleNamespace(id=10), SyncMode.INCREMENTAL)

            default_chart = await history.get_ban_chart(
                __import__("NHCogs.nhmoderation.models", fromlist=["BanChartQuery"]).BanChartQuery(
                    guild_id=10
                )
            )
            self.assertEqual(default_chart.total_count, 0)

    def test_next_weekly_reconciliation_is_sunday_0420_utc(self):
        now = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)

        self.assertEqual(
            next_weekly_reconciliation(now),
            datetime(2026, 8, 30, 4, 20, tzinfo=timezone.utc),
        )

    def test_red_integer_timestamps_are_normalized_to_utc(self):
        item = modlog_observation(
            10,
            SimpleNamespace(
                action_type="tempban",
                case_number=5,
                user=100,
                moderator=55,
                reason="reason",
                created_at=1_777_000_000,
                until=1_777_086_400,
                channel=20,
            ),
            999,
            datetime(2026, 8, 28, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(item)
        self.assertEqual(item.occurred_at.tzinfo, timezone.utc)
        self.assertEqual(item.expiry_at.tzinfo, timezone.utc)

    def test_red_deleted_user_sentinel_is_not_treated_as_discord_identity(self):
        item = modlog_observation(
            10,
            SimpleNamespace(
                action_type="ban",
                case_number=5,
                user=0xDE1,
                moderator=0xDE1,
                reason=None,
                created_at=1_777_000_000,
                until=None,
                channel=None,
            ),
            999,
            datetime(2026, 8, 28, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(item)
        self.assertIsNone(item.target_user_id)
        self.assertIsNone(item.credited_moderator_hint)
        self.assertIsNone(item.attribution_hint)
