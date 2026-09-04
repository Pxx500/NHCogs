import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_forum_autopin import (
    ALLOWED_MENTIONS_NONE,
    FakeConfigRoot,
    FakeGuild,
    FakeTextChannel,
    UserFeedbackCheckFailure,
    discord,
    make_support,
    nhmisc,
)


class OperationalSupportTests(unittest.IsolatedAsyncioTestCase):
    def test_error_configuration_is_separate_from_cog_settings(self):
        old_config = FakeConfigRoot()
        common_config = FakeConfigRoot()
        support = make_support(SimpleNamespace(), old_config, error_config=common_config)

        self.assertIs(support.config, common_config)
        self.assertIsNot(support.config, old_config)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.guild = FakeGuild()
        self.bot = SimpleNamespace(
            get_channel=self.guild.channels.get,
            get_guild=lambda guild_id: self.guild if guild_id == self.guild.id else None,
        )
        self.config = FakeConfigRoot()
        self.error_config = FakeConfigRoot()
        self.support = make_support(self.bot, self.config, self.path, error_config=self.error_config)

    async def test_reopening_preserves_configured_destinations_and_failure_history(self):
        settings = self.config.store_for(self.guild)
        settings.update(moderation_log_channel=42)
        self.error_config.store_for(self.guild)["error_maintainer_id"] = 73
        await self.support.cog_load()
        await self.support.report_operational_error(
            guild_id=self.guild.id,
            source="CustomCommands",
            action="dispatch response",
            error=RuntimeError("send failed"),
        )

        reopened = make_support(self.bot, self.config, self.path, error_config=self.error_config)
        await reopened.cog_load()

        self.assertEqual(await reopened.log_config.guild(self.guild).moderation_log_channel(), 42)
        self.assertEqual(await reopened.config.guild(self.guild).error_maintainer_id(), 73)
        self.assertEqual(await reopened.operational_errors.active_count(self.guild.id), 1)
        self.assertTrue((self.path / "operational_errors.sqlite").is_file())

    def test_uses_the_existing_nhmisc_persistence_identity(self):
        module = sys.modules[nhmisc.OperationalSupport.__module__]
        with (
            mock.patch.object(module.Config, "get_conf", return_value=self.config) as config,
            mock.patch.object(module, "cog_data_path", return_value=self.path) as data_path,
        ):
            nhmisc.OperationalSupport(self.bot)

        config.assert_any_call(
            None,
            identifier=8597423150612235807,
            cog_name="NHMisc",
            force_registration=True,
        )
        data_path.assert_called_once_with(raw_name="NHMisc")

    async def test_moderation_logs_keep_private_destination_and_suppress_mentions(self):
        channel = FakeTextChannel()
        self.guild.channels[channel.id] = channel
        self.config.store_for(self.guild)["moderation_log_channel"] = channel.id

        delivered = await self.support.send_moderation_log(self.guild, "<@42> <@&73>")

        self.assertTrue(delivered)
        self.assertEqual(channel.sent[0], "<@42> <@&73>")
        self.assertIs(channel.allowed_mentions[0], ALLOWED_MENTIONS_NONE)

    async def test_public_moderation_channel_is_not_used(self):
        channel = FakeTextChannel()
        channel.permissions_for = lambda _role: SimpleNamespace(view_channel=True)
        self.guild.channels[channel.id] = channel
        self.config.store_for(self.guild)["moderation_log_channel"] = channel.id

        self.assertFalse(await self.support.send_moderation_log(self.guild, "private log"))
        self.assertEqual(channel.sent, [])

    async def test_required_error_destination_rechecks_privacy_and_bot_permissions(self):
        channel = FakeTextChannel()
        channel.permissions_for = lambda role: SimpleNamespace(
            view_channel=role is not self.guild.default_role, send_messages=True
        )
        self.guild.channels[channel.id] = channel
        self.error_config.store_for(self.guild)["error_channel"] = channel.id
        self.assertIs(await self.support.require_private_error_channel(self.guild), channel)

        for public, can_send in ((True, True), (False, False)):
            with self.subTest(public=public, can_send=can_send):
                channel.permissions_for = lambda role, public=public, can_send=can_send: SimpleNamespace(
                    view_channel=public if role is self.guild.default_role else True,
                    send_messages=can_send,
                )
                with self.assertRaises(UserFeedbackCheckFailure):
                    await self.support.require_private_error_channel(self.guild)

    async def test_missing_required_error_destination_is_rejected(self):
        with self.assertRaises(UserFeedbackCheckFailure):
            await self.support.require_private_error_channel(self.guild)

    async def test_log_transport_failure_is_recorded_without_raising(self):
        await self.support.cog_load()
        channel = FakeTextChannel()
        channel.guild = self.guild
        channel.send = mock.AsyncMock(side_effect=discord.HTTPException("send failed"))
        self.guild.channels[channel.id] = channel
        self.config.store_for(self.guild)["moderation_log_channel"] = channel.id

        self.assertFalse(await self.support.send_moderation_log(self.guild, "private log"))
        self.assertEqual(await self.support.operational_errors.active_count(self.guild.id), 1)

    async def test_maintainer_deletion_does_not_require_nhmisc_or_clear_other_settings(self):
        settings = self.error_config.store_for(self.guild)
        settings.update(error_maintainer_id=73, moderation_log_channel=42)
        other = self.error_config.store_for(SimpleNamespace(id=self.guild.id + 1))
        other["error_maintainer_id"] = 99

        await self.support.red_delete_data_for_user(requester="user", user_id=73)

        self.assertIsNone(settings["error_maintainer_id"])
        self.assertEqual(settings["moderation_log_channel"], 42)
        self.assertEqual(other["error_maintainer_id"], 99)
