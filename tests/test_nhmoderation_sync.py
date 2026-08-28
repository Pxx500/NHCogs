import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.storage_loader import load_shared_storage

load_shared_storage()

from NHCogs.nhmoderation.history import NHModerationHistory  # noqa: E402
from NHCogs.nhmoderation.models import BanChartQuery  # noqa: E402
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

    async def test_partial_audit_fetch_keeps_observations_without_advancing_cursor(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            entry = SimpleNamespace(
                id=100,
                target=SimpleNamespace(id=1),
                user=SimpleNamespace(id=55),
                reason=None,
                created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            )

            async def audit_fetcher(
                _guild, *, action, after_id, on_batch
            ):
                del after_id
                if action == "ban":
                    await on_batch([entry])
                    raise RuntimeError("later audit page failed")
                return []

            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=audit_fetcher,
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )

            with self.assertRaisesRegex(RuntimeError, "later audit page failed"):
                await synchronizer.synchronize(
                    SimpleNamespace(id=10), SyncMode.INCREMENTAL
                )

            chart = await history.get_ban_chart(BanChartQuery(guild_id=10))
            state = await history.status(10)
            self.assertEqual(chart.total_count, 1)
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

    async def test_completed_initial_migration_is_a_noop(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            audit_fetcher = mock.AsyncMock(side_effect=[[], []])
            modlog_fetcher = mock.AsyncMock(return_value=[])
            snapshot_fetcher = mock.AsyncMock(
                return_value=[
                    SimpleNamespace(user=SimpleNamespace(id=1), reason=None)
                ]
            )
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=audit_fetcher,
                modlog_fetcher=modlog_fetcher,
                snapshot_fetcher=snapshot_fetcher,
            )
            guild = SimpleNamespace(id=10)

            first = await synchronizer.synchronize(guild, SyncMode.INITIAL)
            second = await synchronizer.synchronize(guild, SyncMode.INITIAL)

            self.assertEqual(first.inserted_observations, 1)
            self.assertEqual(second.inserted_observations, 0)
            self.assertEqual(audit_fetcher.await_count, 2)
            self.assertEqual(modlog_fetcher.await_count, 1)
            self.assertEqual(snapshot_fetcher.await_count, 1)

    async def test_initial_migration_records_possible_historical_gap(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            now = datetime(2026, 8, 28, tzinfo=timezone.utc)
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(side_effect=[[], []]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
                clock=lambda: now,
            )

            await synchronizer.synchronize(
                SimpleNamespace(
                    id=10,
                    created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                ),
                SyncMode.INITIAL,
            )

            state = await history.status(10)
            run = await history.migration_run(10)
            self.assertTrue(state.historical_gap)
            self.assertIsNotNone(run)
            self.assertTrue(json.loads(run.report)["historical_gap"])

    async def test_repeated_repair_preserves_gap_and_one_active_snapshot_action(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "moderation.sqlite"
            history = NHModerationHistory(database_path)
            await history.initialize()
            now = datetime(2026, 8, 28, tzinfo=timezone.utc)
            current_time = now

            def clock():
                return current_time

            snapshot_fetcher = mock.AsyncMock(
                return_value=[SimpleNamespace(user=SimpleNamespace(id=1), reason=None)]
            )
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(return_value=[]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=snapshot_fetcher,
                clock=clock,
            )

            await synchronizer.synchronize(
                SimpleNamespace(
                    id=10,
                    created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                ),
                SyncMode.REPAIR,
            )
            current_time = datetime(2026, 10, 28, tzinfo=timezone.utc)
            await synchronizer.synchronize(
                SimpleNamespace(id=10, created_at=current_time),
                SyncMode.REPAIR,
            )

            state = await history.status(10)
            with closing(sqlite3.connect(database_path)) as connection:
                total_actions, current_actions = connection.execute(
                    """SELECT COUNT(*),
                              SUM(CASE WHEN current_state = 'active' THEN 1 ELSE 0 END)
                       FROM moderation_actions
                       WHERE guild_id = ? AND target_user_id = ?
                         AND action_kind = 'ban'""",
                    (10, 1),
                ).fetchone()
            self.assertTrue(state.historical_gap)
            self.assertEqual(state.last_sync_at, current_time)
            self.assertEqual(total_actions, 1)
            self.assertEqual(current_actions, 1)

    async def test_initial_migration_resumes_after_completed_modlog_step(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            case = SimpleNamespace(
                action_type="ban",
                case_number=5,
                user=1,
                moderator=55,
                reason="valid ban",
                created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
                until=None,
                channel=None,
            )
            modlog_fetcher = mock.AsyncMock(return_value=[case])
            audit_fetcher = mock.AsyncMock(
                side_effect=[RuntimeError("audit unavailable"), [], []]
            )
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=audit_fetcher,
                modlog_fetcher=modlog_fetcher,
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )
            guild = SimpleNamespace(id=10)

            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                await synchronizer.synchronize(guild, SyncMode.INITIAL)

            self.assertEqual((await history.status(10)).migration_state, "running")
            self.assertEqual(
                (await history.get_ban_chart(BanChartQuery(guild_id=10))).total_count,
                1,
            )

            await synchronizer.synchronize(guild, SyncMode.INITIAL)

            self.assertEqual(modlog_fetcher.await_count, 1)
            self.assertEqual((await history.status(10)).migration_state, "complete")

    async def test_resumed_initial_report_includes_observations_before_restart(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "moderation.sqlite"
            now = datetime(2026, 8, 28, tzinfo=timezone.utc)
            history = NHModerationHistory(database_path)
            await history.initialize()
            case = SimpleNamespace(
                action_type="ban",
                case_number=5,
                user=1,
                moderator=55,
                reason="valid ban",
                created_at=now,
                until=None,
                channel=None,
            )
            first = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(
                    side_effect=RuntimeError("audit unavailable")
                ),
                modlog_fetcher=mock.AsyncMock(return_value=[case]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
                clock=lambda: now,
            )
            guild = SimpleNamespace(id=10, created_at=now)

            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                await first.synchronize(guild, SyncMode.INITIAL)

            resumed_history = NHModerationHistory(database_path)
            await resumed_history.initialize()
            resumed = ModerationSynchronizer(
                resumed_history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(return_value=[]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
                clock=lambda: now,
            )
            report = await resumed.synchronize(guild, SyncMode.INITIAL)
            migration = await resumed_history.migration_run(10)

            self.assertEqual(report.inserted_observations, 1)
            self.assertIsNotNone(migration)
            self.assertEqual(json.loads(migration.report)["inserted_observations"], 1)

    async def test_initial_migration_resumes_after_restart_at_each_source(self):
        scenarios = {
            "red_modlog": (2, 2, 1),
            "audit_ban": (1, 3, 1),
            "audit_unban": (1, 3, 1),
            "ban_snapshot": (1, 2, 2),
        }
        for failed_source, expected_counts in scenarios.items():
            with self.subTest(source=failed_source), TemporaryDirectory() as directory:
                database_path = Path(directory) / "moderation.sqlite"
                history = NHModerationHistory(database_path)
                await history.initialize()
                modlog_fetcher = mock.AsyncMock(
                    side_effect=(
                        [RuntimeError("source unavailable"), []]
                        if failed_source == "red_modlog"
                        else None
                    ),
                    return_value=[],
                )
                if failed_source == "audit_ban":
                    audit_side_effect = [RuntimeError("source unavailable"), [], []]
                elif failed_source == "audit_unban":
                    audit_side_effect = [[], RuntimeError("source unavailable"), []]
                else:
                    audit_side_effect = [[], []]
                audit_fetcher = mock.AsyncMock(side_effect=audit_side_effect)
                snapshot_fetcher = mock.AsyncMock(
                    side_effect=(
                        [RuntimeError("source unavailable"), []]
                        if failed_source == "ban_snapshot"
                        else None
                    ),
                    return_value=[],
                )
                synchronizer = ModerationSynchronizer(
                    history,
                    bot_user_id=999,
                    audit_fetcher=audit_fetcher,
                    modlog_fetcher=modlog_fetcher,
                    snapshot_fetcher=snapshot_fetcher,
                )
                guild = SimpleNamespace(id=10)

                with self.assertRaisesRegex(RuntimeError, "source unavailable"):
                    await synchronizer.synchronize(guild, SyncMode.INITIAL)
                self.assertEqual(
                    (await history.status(10)).migration_state,
                    "running",
                )

                resumed_history = NHModerationHistory(database_path)
                await resumed_history.initialize()
                resumed = ModerationSynchronizer(
                    resumed_history,
                    bot_user_id=999,
                    audit_fetcher=audit_fetcher,
                    modlog_fetcher=modlog_fetcher,
                    snapshot_fetcher=snapshot_fetcher,
                )
                await resumed.synchronize(guild, SyncMode.INITIAL)

                self.assertEqual(
                    (await resumed_history.status(10)).migration_state,
                    "complete",
                )
                self.assertEqual(
                    (
                        modlog_fetcher.await_count,
                        audit_fetcher.await_count,
                        snapshot_fetcher.await_count,
                    ),
                    expected_counts,
                )

    async def test_cancelled_initial_migration_remains_resumable(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "moderation.sqlite"
            history = NHModerationHistory(database_path)
            await history.initialize()
            cancelled_modlog = mock.AsyncMock(side_effect=asyncio.CancelledError)
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(return_value=[]),
                modlog_fetcher=cancelled_modlog,
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )
            guild = SimpleNamespace(id=10)

            with self.assertRaises(asyncio.CancelledError):
                await synchronizer.synchronize(guild, SyncMode.INITIAL)
            self.assertEqual((await history.status(10)).migration_state, "running")

            resumed_history = NHModerationHistory(database_path)
            await resumed_history.initialize()
            resumed = ModerationSynchronizer(
                resumed_history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(return_value=[]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )
            await resumed.synchronize(guild, SyncMode.INITIAL)

            self.assertEqual(
                (await resumed_history.status(10)).migration_state,
                "complete",
            )

    async def test_initial_snapshot_retry_reuses_source_identity(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "moderation.sqlite"
            history = NHModerationHistory(database_path)
            await history.initialize()
            complete_step = history.complete_migration_step

            async def interrupt_after_snapshot(guild_id, run_id, step):
                if step == "ban_snapshot":
                    raise RuntimeError("interrupted after snapshot")
                return await complete_step(guild_id, run_id, step)

            history.complete_migration_step = interrupt_after_snapshot
            snapshot_fetcher = mock.AsyncMock(
                return_value=[SimpleNamespace(user=SimpleNamespace(id=1), reason=None)]
            )
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(return_value=[]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=snapshot_fetcher,
            )
            guild = SimpleNamespace(id=10)

            with self.assertRaisesRegex(RuntimeError, "interrupted after snapshot"):
                await synchronizer.synchronize(guild, SyncMode.INITIAL)

            resumed_history = NHModerationHistory(database_path)
            await resumed_history.initialize()
            resumed = ModerationSynchronizer(
                resumed_history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(return_value=[]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=snapshot_fetcher,
            )
            await resumed.synchronize(guild, SyncMode.INITIAL)

            with closing(sqlite3.connect(database_path)) as connection:
                snapshot_count = connection.execute(
                    """SELECT COUNT(*) FROM moderation_observations
                       WHERE guild_id = ? AND source_kind = 'discord_ban_snapshot'
                         AND target_user_id = ?""",
                    (10, 1),
                ).fetchone()[0]
            self.assertEqual(snapshot_count, 1)

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

    async def test_red_audit_reason_credits_human_behind_bot_action(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            entry = SimpleNamespace(
                id=100,
                target=SimpleNamespace(id=1),
                user=SimpleNamespace(id=999),
                reason="Action requested by Moderator (ID 55). Reason: valid ban",
                created_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            )
            synchronizer = ModerationSynchronizer(
                history,
                bot_user_id=999,
                audit_fetcher=mock.AsyncMock(side_effect=[[entry], []]),
                modlog_fetcher=mock.AsyncMock(return_value=[]),
                snapshot_fetcher=mock.AsyncMock(return_value=[]),
            )

            await synchronizer.synchronize(
                SimpleNamespace(id=10), SyncMode.INCREMENTAL
            )

            chart = await history.get_ban_chart(BanChartQuery(guild_id=10))
            self.assertEqual(
                [(row.moderator_user_id, row.count) for row in chart.rows],
                [(55, 1)],
            )

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
