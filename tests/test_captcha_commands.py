"""CAPTCHA configuration and moderator command contracts."""

import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support
from tests.test_daily_stats import _Embed
from tests.test_settings_commands import _OverviewEmbed, _ScalarSetting


class CaptchaCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_enable_admits_existing_timers_instead_of_startup_restoration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                configured = _ScalarSetting(False)
                guild = SimpleNamespace(id=10)
                owner = SimpleNamespace(check_configuration=mock.AsyncMock(), enable_existing=mock.AsyncMock(return_value=40), restore=mock.AsyncMock())
                cog = SimpleNamespace(_joinwatch_verification=owner, _channel_is_private=lambda *args: True, config=SimpleNamespace(guild=lambda target: SimpleNamespace(joinwatch_captcha_enabled=configured)))
                ctx = SimpleNamespace(guild=guild, channel=object(), send=mock.AsyncMock())
                await honeypot.joinwatch_commands.captcha_toggle(cog, ctx, True)
                self.assertIs(configured.value, True)
                owner.enable_existing.assert_awaited_once_with(guild)
                owner.restore.assert_not_awaited()
                self.assertIn("40", ctx.send.await_args.args[0])

    async def test_group_limits_update_only_the_two_configured_budgets(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._channel_is_private = mock.Mock(return_value=True)
                guild = SimpleNamespace(id=10)
                ctx = SimpleNamespace(guild=guild, channel=object(), send=mock.AsyncMock())
                await cog.joinwatch_groups_limits(ctx, 80, 8)
                self.assertEqual(cog.config._guilds[10], {"joinwatch_groups_max_active": 80, "joinwatch_groups_per_minute": 8})
                self.assertIn("No accounts were enrolled", ctx.send.await_args.args[0])

    async def test_doctor_denies_public_channel_before_reading_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._channel_is_private = mock.Mock(return_value=False)
                cog.config = SimpleNamespace(guild=mock.Mock(side_effect=AssertionError("Protected config read")))
                ctx = SimpleNamespace(guild=SimpleNamespace(id=1), channel=object(), send=mock.AsyncMock())
                await cog.honeypot_doctor(ctx)
                cog.config.guild.assert_not_called()
                self.assertIn("private", ctx.send.await_args.args[0].lower())

    def test_installation_keeps_new_sources_off_and_panel_unpublished(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                defaults = cog.config.defaults
                self.assertIs(defaults.get("joinwatch_captcha_enabled"), False)
                self.assertIs(defaults.get("joinwatch_groups_enabled"), False)
                self.assertIsNone(defaults.get("captcha_panel_message_id"))
                self.assertEqual(defaults["joinwatch_min_age_hours"], 24)

    async def test_status_refuses_public_channel_before_reading_state(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                commands = importlib.import_module("NHCogs.honeypot.captcha_commands")
                owner = SimpleNamespace(inspect=mock.AsyncMock())
                cog = SimpleNamespace(_joinwatch_verification=owner, _channel_is_private=lambda *args: False)
                ctx = SimpleNamespace(guild=SimpleNamespace(id=1), channel=object(), send=mock.AsyncMock())
                await commands.status(cog, ctx, SimpleNamespace(id=2))
                owner.inspect.assert_not_awaited()
                self.assertIn("private", ctx.send.await_args.args[0].lower())

    async def test_sample_stats_can_be_sent_in_public_without_reading_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = SimpleNamespace(guild=mock.Mock(side_effect=AssertionError("Protected config read")))
                ctx = SimpleNamespace(send=mock.AsyncMock())
                with mock.patch.object(honeypot.discord, "Embed", _Embed), mock.patch.object(honeypot.discord, "Color", SimpleNamespace(blue=lambda: 1)):
                    await cog.honeypot_stats_preview(ctx)
                embed = ctx.send.await_args.kwargs['embed']
                self.assertEqual(embed.title, "Daily summary preview (sample data)")
                self.assertIn("Extra party guests", embed.fields[1].value)
                self.assertIn(": 40", embed.fields[1].value)
                self.assertFalse(ctx.send.await_args.kwargs['allowed_mentions'].users)

    async def test_criteria_confirmation_checks_owner_and_current_permissions(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                owner = SimpleNamespace(confirm_criteria=mock.AsyncMock())
                cog = SimpleNamespace(_joinwatch_groups=owner, _channel_is_private=lambda *args: True)
                view = honeypot.joinwatch_commands.CriteriaConfirmationView(cog, {}, 20)
                click = SimpleNamespace(user=SimpleNamespace(id=21, guild_permissions=SimpleNamespace(manage_messages=True)), permissions=SimpleNamespace(manage_messages=True), guild=SimpleNamespace(id=10), channel=object(), response=SimpleNamespace(defer=mock.AsyncMock()), edit_original_response=mock.AsyncMock(), message=SimpleNamespace(edit=mock.AsyncMock()))
                await view.children[0].callback(click)
                owner.confirm_criteria.assert_not_awaited()
                click.user.id = 20
                click.permissions.manage_messages = False
                await view.children[0].callback(click)
                owner.confirm_criteria.assert_not_awaited()
                click.permissions.manage_messages = True
                await view.children[0].callback(click)
                owner.confirm_criteria.assert_awaited_once_with(click.guild, {}, 20, True)

    async def test_wave_controls_follow_channel_grants_and_denials_not_guild_permissions(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                for guild_allowed, channel_allowed in ((False, True), (True, False)):
                    with self.subTest(guild_allowed=guild_allowed, channel_allowed=channel_allowed):
                        record = {"id": "wave", "moderator_id": 20, "status": "paused", "criteria": {"minimum_accounts": 5, "join_window_minutes": 15, "creation_distance_hours": 6}}
                        owner = SimpleNamespace(pause=mock.AsyncMock(return_value=record))
                        cog = SimpleNamespace(_joinwatch_waves=owner, _channel_is_private=lambda *args: True)
                        view = honeypot.joinwatch_commands.WaveControlView(cog, record)
                        click = SimpleNamespace(
                            user=SimpleNamespace(id=20, guild_permissions=SimpleNamespace(manage_messages=guild_allowed)),
                            permissions=SimpleNamespace(manage_messages=channel_allowed),
                            guild=SimpleNamespace(id=10), channel=object(),
                            response=SimpleNamespace(defer=mock.AsyncMock()),
                            edit_original_response=mock.AsyncMock(),
                            message=SimpleNamespace(edit=mock.AsyncMock()),
                        )
                        with mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed):
                            await view.children[0].callback(click)
                        if channel_allowed:
                            owner.pause.assert_awaited_once_with(click.guild, "wave", 20, True)
                            click.message.edit.assert_awaited_once()
                        else:
                            owner.pause.assert_not_awaited()
                            click.message.edit.assert_not_awaited()

    async def test_verify_restoration_registers_shared_persistent_handler_without_publication(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                await honeypot.captcha_commands.restore_panels(cog)
                restored, message_id = cog.bot.restored_views[0]
                self.assertIsNone(restored.timeout)
                self.assertIsNone(message_id)
                self.assertEqual(restored.children[0].custom_id, "honeypot:captcha:verify")
                self.assertIsNone(cog.config.defaults['captcha_panel_message_id'])
