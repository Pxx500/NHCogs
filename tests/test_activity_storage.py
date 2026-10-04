import importlib.util
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

MODULE_PATH = Path(__file__).parents[1] / "NHCogs" / "nhmisc" / "activity_storage.py"
SPEC = importlib.util.spec_from_file_location("nhmisc_activity_storage_test", MODULE_PATH)
activity_storage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = activity_storage
SPEC.loader.exec_module(activity_storage)


class ActivityStoreLeaderboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_activity_summary_does_not_infer_zero_from_guild_history(self):
        with TemporaryDirectory() as directory:
            store = activity_storage.ActivityStore(Path(directory) / "activity.sqlite3")
            await store.initialize()
            await store.record_message(
                guild_id=1,
                date_utc=date(2026, 7, 25),
                hour_utc=12,
                user_id=42,
                channel_id=100,
                thread_id=None,
                now_utc=datetime(2026, 7, 25, 12, tzinfo=timezone.utc),
            )
            await store.close_stale_days(
                guild_id=1, current_date_utc=date(2026, 7, 26), member_count=100,
            )
            await store.prune_detail_rows_older_than(1, date(2026, 7, 26))
            for guild_id, user_id in ((1, 42), (1, 99), (2, 42)):
                with self.subTest(guild_id=guild_id, user_id=user_id):
                    summary = await store.get_member_activity_summary(
                        guild_id=guild_id,
                        user_id=user_id,
                        start_date_utc=date(2026, 7, 25),
                        end_date_utc=date(2026, 7, 27),
                    )
                    self.assertEqual(summary, {
                        "messages": None,
                        "active_days": None,
                        "distinct_channels": None,
                        "availability": "no_data",
                        "coverage": "unknown",
                    })

    async def test_member_activity_summary_counts_retained_days_and_parent_channels(self):
        with TemporaryDirectory() as directory:
            store = activity_storage.ActivityStore(Path(directory) / "activity.sqlite3")
            await store.initialize()
            for guild_id, user_id, day, channel_id, thread_id in (
                (1, 42, 24, 100, None),
                (1, 42, 25, 100, None),
                (1, 42, 25, 100, 101),
                (1, 42, 27, 200, None),
                (1, 42, 28, 300, None),
                (1, 99, 26, 300, None),
                (2, 42, 26, 300, None),
            ):
                await store.record_message(
                    guild_id=guild_id,
                    date_utc=date(2026, 7, day),
                    hour_utc=12,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    now_utc=datetime(2026, 7, day, 12, tzinfo=timezone.utc),
                )
            await store.close_stale_days(
                guild_id=1,
                current_date_utc=date(2026, 7, 29),
                member_count=100,
            )

            summary = await store.get_member_activity_summary(
                guild_id=1,
                user_id=42,
                start_date_utc=date(2026, 7, 25),
                end_date_utc=date(2026, 7, 27),
            )

        self.assertEqual(summary, {
            "messages": 3,
            "active_days": 2,
            "distinct_channels": 2,
            "availability": "observed",
            "coverage": "unknown",
        })

    async def test_guild_user_counts_aggregate_date_range_and_apply_limit(self):
        with TemporaryDirectory() as directory:
            store = activity_storage.ActivityStore(Path(directory) / "activity.sqlite3")
            await store.initialize()

            async def record(
                *,
                guild_id: int,
                day: date,
                user_id: int,
                channel_id: int,
                count: int,
            ) -> None:
                for _ in range(count):
                    await store.record_message(
                        guild_id=guild_id,
                        date_utc=day,
                        hour_utc=12,
                        user_id=user_id,
                        channel_id=channel_id,
                        thread_id=None,
                        now_utc=datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
                    )

            await record(
                guild_id=1,
                day=date(2026, 7, 26),
                user_id=20,
                channel_id=100,
                count=2,
            )
            await record(
                guild_id=1,
                day=date(2026, 7, 27),
                user_id=20,
                channel_id=200,
                count=2,
            )
            await record(
                guild_id=1,
                day=date(2026, 7, 27),
                user_id=10,
                channel_id=100,
                count=3,
            )
            await record(
                guild_id=1,
                day=date(2026, 7, 27),
                user_id=30,
                channel_id=200,
                count=3,
            )
            await record(
                guild_id=1,
                day=date(2026, 7, 25),
                user_id=40,
                channel_id=100,
                count=10,
            )
            await record(
                guild_id=2,
                day=date(2026, 7, 27),
                user_id=50,
                channel_id=100,
                count=20,
            )

            rows = await store.get_guild_user_counts(
                guild_id=1,
                end_date_utc=date(2026, 7, 27),
                days=2,
                limit=2,
            )

        self.assertEqual(
            [(row.user_id, row.message_count) for row in rows],
            [(20, 4), (10, 3)],
        )

    async def test_guild_user_counts_include_closed_days(self):
        with TemporaryDirectory() as directory:
            store = activity_storage.ActivityStore(Path(directory) / "activity.sqlite3")
            await store.initialize()

            for _ in range(100):
                await store.record_message(
                    guild_id=1,
                    date_utc=date(2026, 7, 26),
                    hour_utc=12,
                    user_id=10,
                    channel_id=100,
                    thread_id=None,
                    now_utc=datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
                )
            await store.close_stale_days(
                guild_id=1,
                current_date_utc=date(2026, 7, 27),
                member_count=1_000,
            )
            for _ in range(5):
                await store.record_message(
                    guild_id=1,
                    date_utc=date(2026, 7, 27),
                    hour_utc=12,
                    user_id=20,
                    channel_id=100,
                    thread_id=None,
                    now_utc=datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
                )

            rows = await store.get_guild_user_counts(
                guild_id=1,
                end_date_utc=date(2026, 7, 27),
                days=2,
                limit=20,
            )

        self.assertEqual(
            [(row.user_id, row.message_count) for row in rows],
            [(10, 100), (20, 5)],
        )

    async def test_delete_user_everywhere_removes_personal_activity_details(self):
        with TemporaryDirectory() as directory:
            store = activity_storage.ActivityStore(Path(directory) / "activity.sqlite3")
            await store.initialize()
            day = date(2026, 8, 4)
            now = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
            for user_id in (42, 42, 99):
                await store.record_message(
                    guild_id=1,
                    date_utc=day,
                    hour_utc=12,
                    user_id=user_id,
                    channel_id=100,
                    thread_id=None,
                    now_utc=now,
                )

            await store.delete_user_everywhere(42)

            rows = await store.get_guild_user_counts(
                guild_id=1,
                end_date_utc=day,
                days=1,
                limit=20,
            )
            deleted_distribution = await store.get_user_channel_distribution(
                guild_id=1,
                user_id=42,
                end_date_utc=day,
                days=1,
            )
            remaining_distribution = await store.get_user_channel_distribution(
                guild_id=1,
                user_id=99,
                end_date_utc=day,
                days=1,
            )

        self.assertEqual(
            [(row.user_id, row.message_count) for row in rows],
            [(99, 1)],
        )
        self.assertEqual(deleted_distribution.total_messages, 0)
        self.assertEqual(deleted_distribution.top_locations, [])
        self.assertEqual(remaining_distribution.total_messages, 1)


if __name__ == "__main__":
    unittest.main()
