import json
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.test_chatchart import load_nhmisc_module

nhmisc = load_nhmisc_module()


class MemberActivitySummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_uses_configured_utc_retention_without_a_report_channel(self):
        with TemporaryDirectory() as directory:
            cog = nhmisc.NHMisc.__new__(nhmisc.NHMisc)
            cog._activity_store = nhmisc.ActivityStore(Path(directory) / "activity.sqlite3")
            await cog._activity_store.initialize()
            config = types.SimpleNamespace(
                activity_detail_retention_days=mock.AsyncMock(return_value=2),
                activity_channel=mock.AsyncMock(return_value=None),
            )
            cog.config = types.SimpleNamespace(guild_from_id=lambda guild_id: config)
            for day in (24, 25, 26):
                await cog._activity_store.record_message(
                    guild_id=1,
                    date_utc=date(2026, 7, day),
                    hour_utc=12,
                    user_id=42,
                    channel_id=100,
                    thread_id=None,
                    now_utc=datetime(2026, 7, day, 12, tzinfo=timezone.utc),
                )
            summary = await cog.get_member_activity_summary(
                1, 42, now=datetime(2026, 7, 27, 1, tzinfo=timezone(timedelta(hours=2))),
            )

        self.assertEqual(json.loads(json.dumps(summary)), {
            "messages": 2,
            "active_days": 2,
            "distinct_channels": 1,
            "availability": "observed",
            "coverage": "unknown",
            "window_start": "2026-07-25T00:00:00+00:00",
            "window_end": "2026-07-26T23:00:00+00:00",
            "captured_at": "2026-07-26T23:00:00+00:00",
        })


if __name__ == "__main__":
    unittest.main()
