import types
import unittest
from unittest import mock

from tests.test_chatchart import load_nhmisc_module

nhmisc = load_nhmisc_module()


class FakeEmbed:
    def __init__(self, *, title=None, description=None, **_kwargs):
        self.title = title
        self.description = description
        self.fields = []

    def add_field(self, *, name, value, inline=False):
        self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))


class FakeConfigValue:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    async def __call__(self):
        return self._store[self._key]

    async def set(self, value):
        self._store[self._key] = value


class FakeGuildConfig:
    def __init__(self, store):
        self._store = store

    async def all(self):
        return dict(self._store)

    def __getattr__(self, name):
        return FakeConfigValue(self._store, name)


class FakeConfig:
    def __init__(self, values):
        self._values = values

    def guild(self, _guild):
        return FakeGuildConfig(self._values)


class ConfigurationStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        nhmisc.discord.Embed = FakeEmbed
        self.guild = types.SimpleNamespace(id=123)
        self.ctx = types.SimpleNamespace(
            guild=self.guild,
            clean_prefix="!",
            author=types.SimpleNamespace(
                guild_permissions=types.SimpleNamespace(
                    manage_messages=True,
                    manage_guild=False,
                )
            ),
            send=mock.AsyncMock(),
            send_help=mock.AsyncMock(),
        )
        self.cog = object.__new__(nhmisc.NHMisc)
        self.cog.bot = types.SimpleNamespace(is_admin=mock.AsyncMock(return_value=False))

    async def test_alert_group_shows_current_channel_and_change_command(self):
        self.cog.config = FakeConfig({"alert_channel": 42})

        await nhmisc.NHMisc.nhmisc_alert.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "Alert logging")
        self.assertIn("<#42>", fields["Current configuration"])
        self.assertIn(
            "!nhmisc alert channel #new-channel", fields["Change it"]
        )
        self.ctx.send_help.assert_not_awaited()

    async def test_alert_group_labels_an_unconfigured_channel(self):
        self.cog.config = FakeConfig({"alert_channel": None})

        await nhmisc.NHMisc.nhmisc_alert.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("Channel: Not configured", fields["Current configuration"])

    async def test_activity_group_shows_channel_retention_and_useful_commands(self):
        self.cog.config = FakeConfig(
            {
                "activity_channel": 73,
                "activity_detail_retention_days": 30,
                "activity_history_retention_days": -1,
            }
        )

        await nhmisc.NHMisc.nhmisc_activity.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        commands = fields["Commands"]
        self.assertEqual(embed.title, "Activity tracking")
        self.assertIn("<#73>", current)
        self.assertIn("Detail retention: 30 days", current)
        self.assertIn("History retention: Unlimited", current)
        self.assertIn("!nhmisc activity channel #new-channel", commands)
        self.assertIn("!nhmisc activity current", commands)
        self.assertIn("!nhmisc activity retention <days>", commands)
        self.ctx.send_help.assert_not_awaited()

    async def test_activity_group_keeps_configuration_private_from_non_staff(self):
        self.ctx.author.guild_permissions.manage_messages = False
        self.cog.config = FakeConfig(
            {
                "activity_channel": 73,
                "activity_detail_retention_days": 30,
                "activity_history_retention_days": -1,
            }
        )

        with self.assertRaises(nhmisc.commands.UserFeedbackCheckFailure):
            await nhmisc.NHMisc.nhmisc_activity.callback(self.cog, self.ctx)

        self.ctx.send.assert_not_awaited()

    async def test_vcjumping_group_shows_both_detection_settings(self):
        self.cog.config = FakeConfig(
            {
                "vcjumping_visit_count": 5,
                "vcjumping_window_seconds": 12,
            }
        )

        await nhmisc.NHMisc.nhmisc_vcjumping.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        commands = fields["Change it"]
        self.assertEqual(embed.title, "VC jumping detection")
        self.assertIn("Channel entries: 5", current)
        self.assertIn("Time window: 12 seconds", current)
        self.assertIn("!nhmisc vcjumping visits <count>", commands)
        self.assertIn("!nhmisc vcjumping seconds <seconds>", commands)
        self.ctx.send_help.assert_not_awaited()

    async def test_forumautopin_group_summarizes_configured_forums(self):
        self.cog._forum_autopin = types.SimpleNamespace(
            get_forum_ids=mock.AsyncMock(return_value=(88, 89))
        )

        await nhmisc.NHMisc.nhmisc_forumautopin.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        commands = fields["Commands"]
        self.assertEqual(embed.title, "Forum autopin")
        self.assertIn("Configured forums: 2", current)
        self.assertIn("<#88>", current)
        self.assertIn("<#89>", current)
        self.assertIn("!nhmisc forumautopin add #forum", commands)
        self.assertIn("!nhmisc forumautopin remove #forum", commands)
        self.assertIn("!nhmisc forumautopin list", commands)
        self.ctx.send_help.assert_not_awaited()

    async def test_stickyroles_group_summarizes_configured_roles(self):
        self.ctx.author.guild_permissions.manage_guild = True
        roles = {
            100: types.SimpleNamespace(mention="<@&100>"),
            200: types.SimpleNamespace(mention="<@&200>"),
        }
        self.guild.get_role = roles.get
        self.cog._sticky_roles = types.SimpleNamespace(
            get_sticky_roles=mock.AsyncMock(return_value=frozenset(roles))
        )

        await nhmisc.NHMisc.nhmisc_stickyroles.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        commands = fields["Commands"]
        self.assertEqual(embed.title, "Sticky roles")
        self.assertIn("Configured roles: 2", current)
        self.assertIn("<@&100>", current)
        self.assertIn("<@&200>", current)
        self.assertIn("!nhmisc stickyroles add @role", commands)
        self.assertIn("!nhmisc stickyroles remove @role", commands)
        self.ctx.send_help.assert_not_awaited()

    async def test_sticky_debuglogging_group_shows_toggle_and_channel(self):
        self.ctx.author.guild_permissions.manage_guild = True
        self.cog.config = FakeConfig(
            {
                "sticky_debug_logging_enabled": True,
                "sticky_debug_logging_channel": 321,
            }
        )

        await nhmisc.NHMisc.nhmisc_stickyroles_debuglogging.callback(
            self.cog, self.ctx
        )

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        commands = fields["Change it"]
        self.assertEqual(embed.title, "Sticky role debug logging")
        self.assertIn("Enabled: Yes", current)
        self.assertIn("Channel: <#321>", current)
        self.assertIn("!nhmisc stickyroles debuglogging toggle", commands)
        self.assertIn(
            "!nhmisc stickyroles debuglogging channel #new-channel", commands
        )
        self.ctx.send_help.assert_not_awaited()

    async def test_roleanalytics_group_shows_database_state(self):
        state = types.SimpleNamespace(
            enabled=True,
            status=types.SimpleNamespace(value="READY"),
            source_member_count=80_123,
        )
        self.cog._role_analytics_store = types.SimpleNamespace(
            get_state=mock.AsyncMock(return_value=state)
        )

        await nhmisc.NHMisc.nhmisc_roleanalytics.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        commands = fields["Commands"]
        self.assertEqual(embed.title, "Role analytics")
        self.assertIn("Enabled: Yes", current)
        self.assertIn("Status: Ready", current)
        self.assertIn("Members in snapshot: 80,123", current)
        self.assertIn("!rolesync", commands)
        self.assertIn("!nhmisc roleanalytics disable", commands)
        self.ctx.send_help.assert_not_awaited()

    async def test_nhmisc_root_shows_command_dashboard_instead_of_generic_help(self):
        await nhmisc.NHMisc.nhmisc.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "NHMisc")
        self.assertIn("!nhmisc alert", fields["Configuration"])
        self.assertIn("!nhmisc activity", fields["Configuration"])
        self.assertIn("!nhmisc chatchart", fields["Activity and moderation"])
        self.assertIn("!nhmisc cleanup", fields["Activity and moderation"])
        self.ctx.send_help.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
