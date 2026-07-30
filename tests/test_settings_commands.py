"""Guild setting and joinwatch command flows.

Each of these classes owns a single command surface and one or two tests;
they are grouped here rather than given a module apiece.
"""

import logging
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules


class ImageScanSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_threshold_defaults_in_public_threshold_query(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "imagescan_detector_threshold": "invalid",
                            }
                        )
                    )
                )
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={
                        "configured_threshold": 20,
                        "effective_threshold": 20,
                    }
                )
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=100),
                    send=mock.AsyncMock(),
                )

                await cog.imagescan_detector_threshold(ctx)

                ctx.send.assert_awaited_once_with(
                    "Threshold: 20 effective 20"
                )


class PurgeMaintenanceSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_prunes_registry_at_fourteen_days(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._message_registry.initialize()
                await cog._message_registry.observe(
                    honeypot.MessageRecord(
                        message_id=400,
                        guild_id=100,
                        channel_id=300,
                        author_id=200,
                        created_at=datetime.now(timezone.utc) - timedelta(days=15),
                        pinned=False,
                        author_kind="member",
                        fingerprint="fingerprint",
                    )
                )

                await cog.purge_cache_cleanup_loop.function(cog)

                self.assertEqual(
                    await cog._message_registry.recent_by_author(100, 200),
                    (),
                )


class DiagnosticSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_dry_run_defaults_in_owner_config_output(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "dry_run": "false",
                            }
                        )
                    )
                )
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=100),
                    send=mock.AsyncMock(),
                )

                await cog.config_honeypot(ctx)

                output = ctx.send.await_args.args[0]
                self.assertIn("Dry run: disabled", output)


class SettingCommandSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_backward_window_defaults_in_owner_query(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "purge_backward_seconds": "999",
                            }
                        )
                    )
                )
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=100),
                    send=mock.AsyncMock(),
                )

                with self.assertLogs("red.Honeypot", level=logging.WARNING):
                    await cog.purge_backward(ctx)

                ctx.send.assert_awaited_once_with(
                    "Backward purge window: 60s"
                )


class JoinwatchSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_disabled_setting_does_not_publish_join_alert(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                config = {
                    "joinwatch_enabled": "false",
                    "joinwatch_channel": 300,
                    "joinwatch_min_age_hours": 24,
                    "joinwatch_auto_role_enabled": False,
                    "joinwatch_alert_enabled": True,
                }
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(return_value=config)
                    )
                )
                alert_channel = SimpleNamespace(send=mock.AsyncMock())
                cog._get_text_channel_or_thread = mock.Mock(
                    return_value=alert_channel
                )
                cog._increment_stat = mock.AsyncMock()
                honeypot.joinwatch._store_joinwatch_pending_role_alert = mock.AsyncMock()
                guild = SimpleNamespace(id=100)
                member = SimpleNamespace(
                    id=200,
                    guild=guild,
                    bot=False,
                    created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    joined_at=datetime.now(timezone.utc),
                    display_name="New Member",
                    display_avatar=None,
                    mention="<@200>",
                    roles=[],
                )
                embed = SimpleNamespace(
                    set_author=mock.Mock(),
                    set_thumbnail=mock.Mock(),
                    add_field=mock.Mock(),
                )

                with mock.patch.object(
                    honeypot.discord,
                    "Embed",
                    return_value=embed,
                ), mock.patch.object(
                    honeypot.discord,
                    "Color",
                    SimpleNamespace(orange=mock.Mock(return_value=None)),
                ):
                    await cog.on_member_join(member)

                alert_channel.send.assert_not_awaited()


class JoinwatchCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_age_enforces_practical_upper_boundary(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                setting = SimpleNamespace(set=mock.AsyncMock())
                cog = object.__new__(honeypot.Honeypot)
                cog.config = SimpleNamespace(
                    guild=mock.Mock(
                        return_value=SimpleNamespace(joinwatch_min_age_hours=setting)
                    )
                )
                ctx = SimpleNamespace(guild=object(), send=mock.AsyncMock())

                await honeypot.Honeypot.max_age.callback(cog, ctx, 1_000_000)

                setting.set.assert_awaited_once_with(1_000_000)

                setting.set.reset_mock()
                ctx.send.reset_mock()

                await honeypot.Honeypot.max_age.callback(cog, ctx, 1_000_001)

                setting.set.assert_not_awaited()
                ctx.send.assert_awaited_once_with(
                    "Hours must be between 1 and 1000000."
                )

    async def test_max_age_rejects_zero_at_lower_boundary(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                setting = SimpleNamespace(set=mock.AsyncMock())
                cog = object.__new__(honeypot.Honeypot)
                cog.config = SimpleNamespace(
                    guild=mock.Mock(
                        return_value=SimpleNamespace(joinwatch_min_age_hours=setting)
                    )
                )
                ctx = SimpleNamespace(guild=object(), send=mock.AsyncMock())

                await honeypot.Honeypot.max_age.callback(cog, ctx, 0)

                setting.set.assert_not_awaited()
                ctx.send.assert_awaited_once_with(
                    "Hours must be between 1 and 1000000."
                )
