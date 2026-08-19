"""Daily public Honeypot statistics publication."""

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules


class _Embed:
    def __init__(self, *, title, color):
        self.title = title
        self.color = color
        self.fields = []

    def add_field(self, *, name, value, inline):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


class DailyStatsPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_becomes_due_at_five_minutes_after_midnight_utc(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                before = datetime(2026, 8, 20, 0, 4, 59, tzinfo=timezone.utc)
                due = datetime(2026, 8, 20, 0, 5, tzinfo=timezone.utc)

                self.assertFalse(honeypot.daily_stats.publication_is_due(before))
                self.assertTrue(honeypot.daily_stats.publication_is_due(due))
                self.assertEqual(
                    honeypot.daily_stats.completed_report_date(due),
                    date(2026, 8, 19),
                )

    async def test_completed_day_is_published_once_with_compact_public_fields(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                store = honeypot.DetectionCaseStore(Path(directory) / "daily.sqlite")
                store.initialize()
                occurred_at = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
                for metric in (
                    "detections",
                    "automated_bans",
                    "manual_bans",
                    "shadowbans",
                    "joinwatch_bans",
                ):
                    store.record_daily_stat(100, occurred_at, metric)
                store.record_daily_stat(100, occurred_at, "detections")

                channel = SimpleNamespace(id=300, send=mock.AsyncMock())
                channel.send.return_value = SimpleNamespace(id=400)
                guild = SimpleNamespace(id=100)
                config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"daily_stats_channel": channel.id})
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(guilds=[guild]),
                    config=SimpleNamespace(guild=mock.Mock(return_value=config)),
                    _case_store=store,
                    _get_text_channel_or_thread=mock.Mock(return_value=channel),
                    _record_operational_failure=mock.AsyncMock(),
                )
                now = datetime(2026, 8, 20, 0, 5, tzinfo=timezone.utc)

                with (
                    mock.patch.object(honeypot.daily_stats.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.daily_stats.discord,
                        "Color",
                        SimpleNamespace(blue=mock.Mock(return_value="blue")),
                    ),
                ):
                    await honeypot.daily_stats.publish_completed_day(cog, now)
                    await honeypot.daily_stats.publish_completed_day(cog, now)

                channel.send.assert_awaited_once()
                embed = channel.send.await_args.kwargs["embed"]
                self.assertEqual(
                    embed.title,
                    "Honeypot daily summary - 2026-08-19 UTC",
                )
                self.assertEqual(
                    [(field.name, field.value, field.inline) for field in embed.fields],
                    [
                        (
                            "Honeypot",
                            "Detections: 2\nAutomated bans: 1\nManual bans: 1",
                            False,
                        ),
                        (
                            "JoinWatch",
                            "Shadowbans: 1\nBans: 1",
                            False,
                        ),
                    ],
                )
                snapshot = store.get_daily_stats(100, date(2026, 8, 19))
                self.assertEqual(snapshot.publication_channel_id, 300)
                self.assertEqual(snapshot.publication_message_id, 400)
                cog._record_operational_failure.assert_not_awaited()

    async def test_one_guild_failure_does_not_block_the_next_zero_summary(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                store = honeypot.DetectionCaseStore(Path(directory) / "daily.sqlite")
                store.initialize()
                store.observe_daily_stats_day(101, date(2026, 8, 19))
                store.observe_daily_stats_day(102, date(2026, 8, 19))
                failing_channel = SimpleNamespace(
                    id=301,
                    send=mock.AsyncMock(
                        side_effect=honeypot.discord.HTTPException("send failed")
                    ),
                )
                working_channel = SimpleNamespace(id=302, send=mock.AsyncMock())
                working_channel.send.return_value = SimpleNamespace(id=402)
                failing_guild = SimpleNamespace(id=101)
                working_guild = SimpleNamespace(id=102)
                channels = {
                    failing_guild.id: failing_channel,
                    working_guild.id: working_channel,
                }

                def guild_config(guild):
                    return SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={"daily_stats_channel": channels[guild.id].id}
                        )
                    )

                cog = SimpleNamespace(
                    bot=SimpleNamespace(guilds=[failing_guild, working_guild]),
                    config=SimpleNamespace(guild=mock.Mock(side_effect=guild_config)),
                    _case_store=store,
                    _get_text_channel_or_thread=mock.Mock(
                        side_effect=lambda guild, _channel_id: channels[guild.id]
                    ),
                    _record_operational_failure=mock.AsyncMock(),
                )
                now = datetime(2026, 8, 20, 0, 5, tzinfo=timezone.utc)

                with (
                    mock.patch.object(honeypot.daily_stats.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.daily_stats.discord,
                        "Color",
                        SimpleNamespace(blue=mock.Mock(return_value="blue")),
                    ),
                ):
                    await honeypot.daily_stats.publish_completed_day(cog, now)

                failing_channel.send.assert_awaited_once()
                working_channel.send.assert_awaited_once()
                working_embed = working_channel.send.await_args.kwargs["embed"]
                self.assertEqual(
                    [field.value for field in working_embed.fields],
                    [
                        "Detections: 0\nAutomated bans: 0\nManual bans: 0",
                        "Shadowbans: 0\nBans: 0",
                    ],
                )
                cog._record_operational_failure.assert_awaited_once_with(
                    failing_guild.id,
                    "daily_stats_publish",
                    mock.ANY,
                )
                snapshot = store.get_daily_stats(
                    working_guild.id,
                    date(2026, 8, 19),
                )
                self.assertEqual(snapshot.publication_message_id, 402)

    async def test_unobserved_completed_day_is_not_published_as_zero_summary(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                store = honeypot.DetectionCaseStore(Path(directory) / "daily.sqlite")
                store.initialize()
                channel = SimpleNamespace(id=300, send=mock.AsyncMock())
                channel.send.return_value = SimpleNamespace(id=400)
                guild = SimpleNamespace(id=100)
                config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"daily_stats_channel": channel.id})
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(guilds=[guild]),
                    config=SimpleNamespace(guild=mock.Mock(return_value=config)),
                    _case_store=store,
                    _get_text_channel_or_thread=mock.Mock(return_value=channel),
                    _record_operational_failure=mock.AsyncMock(),
                )

                with (
                    mock.patch.object(honeypot.daily_stats.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.daily_stats.discord,
                        "Color",
                        SimpleNamespace(blue=mock.Mock(return_value="blue")),
                    ),
                ):
                    await honeypot.daily_stats.publish_completed_day(
                        cog,
                        datetime(2026, 8, 20, 0, 5, tzinfo=timezone.utc),
                    )

                channel.send.assert_not_awaited()
                snapshot = store.get_daily_stats(100, date(2026, 8, 19))
                self.assertFalse(snapshot.observed)
                cog._record_operational_failure.assert_not_awaited()

    async def test_missing_configured_channel_records_operational_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                store = honeypot.DetectionCaseStore(Path(directory) / "daily.sqlite")
                store.initialize()
                store.observe_daily_stats_day(100, date(2026, 8, 19))
                guild = SimpleNamespace(id=100)
                config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"daily_stats_channel": 300})
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(guilds=[guild]),
                    config=SimpleNamespace(guild=mock.Mock(return_value=config)),
                    _case_store=store,
                    _get_text_channel_or_thread=mock.Mock(return_value=None),
                    _record_operational_failure=mock.AsyncMock(),
                )

                await honeypot.daily_stats.publish_completed_day(
                    cog,
                    datetime(2026, 8, 20, 0, 5, tzinfo=timezone.utc),
                )

                cog._record_operational_failure.assert_awaited_once_with(
                    guild.id,
                    "daily_stats_publish",
                    mock.ANY,
                )


if __name__ == "__main__":
    unittest.main()
