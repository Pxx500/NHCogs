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


class FakeInvocationChannel:
    def __init__(self, *, public):
        self.public = public

    def permissions_for(self, _target):
        return types.SimpleNamespace(view_channel=self.public)


def command_metadata(qualified_name, signature="", *, hidden=False, children=()):
    return types.SimpleNamespace(
        qualified_name=qualified_name,
        signature=signature,
        hidden=hidden,
        commands=children,
    )


class ConfigurationStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        nhmisc.discord.Embed = FakeEmbed
        self.channels = {}
        self.guild = types.SimpleNamespace(
            id=123,
            default_role=object(),
            get_channel=self.channels.get,
        )
        self.ctx = types.SimpleNamespace(
            guild=self.guild,
            channel=FakeInvocationChannel(public=False),
            clean_prefix="!",
            command=types.SimpleNamespace(
                commands=(command_metadata("nhmisc placeholder"),)
            ),
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

    async def test_group_commands_use_metadata_prefix_signature_and_direct_children(self):
        nested = command_metadata("nhmisc alert channel nested", "<value>")
        direct = command_metadata(
            "nhmisc alert channel",
            "<channel>",
            children=(nested,),
        )
        hidden = command_metadata("nhmisc alert internal", hidden=True)
        self.ctx.clean_prefix = "?"
        self.ctx.command = types.SimpleNamespace(commands=(direct, hidden))
        self.channels[42] = types.SimpleNamespace(mention="<#42>")
        self.cog.config = FakeConfig({"alert_channel": 42})

        await nhmisc.NHMisc.nhmisc_alert.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        commands = fields["Change it"]
        self.assertIn("?nhmisc alert channel <channel>", commands)
        self.assertNotIn("nested", commands)
        self.assertNotIn("internal", commands)

    async def test_alert_group_shows_current_channel_and_change_command(self):
        self.channels[42] = types.SimpleNamespace(mention="<#42>")
        self.cog.config = FakeConfig({"alert_channel": 42})

        await nhmisc.NHMisc.nhmisc_alert.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "Alert logging")
        self.assertIn("<#42>", fields["Current configuration"])
        self.ctx.send_help.assert_not_awaited()

    async def test_public_channel_hides_configuration_but_keeps_commands(self):
        self.ctx.channel = FakeInvocationChannel(public=True)
        self.ctx.command = types.SimpleNamespace(
            commands=(command_metadata("nhmisc alert channel", "<channel>"),)
        )
        self.channels[42] = types.SimpleNamespace(mention="<#42>")
        self.cog.config = FakeConfig({"alert_channel": 42})

        await nhmisc.NHMisc.nhmisc_alert.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertNotIn("<#42>", fields["Current configuration"])
        self.assertIn("hidden from @everyone", fields["Current configuration"])
        self.assertIn("!nhmisc alert channel <channel>", fields["Change it"])

    async def test_alert_group_labels_a_missing_configured_channel_without_its_id(self):
        self.cog.config = FakeConfig({"alert_channel": 404})

        await nhmisc.NHMisc.nhmisc_alert.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        self.assertIn("Configured channel is missing", current)
        self.assertNotIn("404", current)

    async def test_alert_group_labels_an_unconfigured_channel(self):
        self.cog.config = FakeConfig({"alert_channel": None})

        await nhmisc.NHMisc.nhmisc_alert.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("Channel: Not configured", fields["Current configuration"])

    async def test_activity_group_shows_channel_retention_and_useful_commands(self):
        self.channels[73] = types.SimpleNamespace(mention="<#73>")
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
        self.assertEqual(embed.title, "Activity tracking")
        self.assertIn("<#73>", current)
        self.assertIn("Detail retention: 30 days", current)
        self.assertIn("History retention: Unlimited", current)
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
        self.assertEqual(embed.title, "VC jumping detection")
        self.assertIn("Channel entries: 5", current)
        self.assertIn("Time window: 12 seconds", current)
        self.ctx.send_help.assert_not_awaited()

    async def test_forumautopin_group_summarizes_configured_forums(self):
        self.channels.update(
            {
                88: types.SimpleNamespace(mention="<#88>"),
                89: types.SimpleNamespace(mention="<#89>"),
            }
        )
        self.cog._forum_autopin = types.SimpleNamespace(
            get_forum_ids=mock.AsyncMock(return_value=(88, 89))
        )

        await nhmisc.NHMisc.nhmisc_forumautopin.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        current = fields["Current configuration"]
        self.assertEqual(embed.title, "Forum autopin")
        self.assertIn("Configured forums: 2", current)
        self.assertIn("<#88>", current)
        self.assertIn("<#89>", current)
        self.ctx.send_help.assert_not_awaited()

    async def test_forumautopin_group_hides_a_missing_forum_id(self):
        self.cog._forum_autopin = types.SimpleNamespace(
            get_forum_ids=mock.AsyncMock(return_value=(505,))
        )

        await nhmisc.NHMisc.nhmisc_forumautopin.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        current = next(
            field.value for field in embed.fields if field.name == "Current configuration"
        )
        self.assertIn("Configured forum is missing", current)
        self.assertNotIn("505", current)

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
        self.assertEqual(embed.title, "Sticky roles")
        self.assertIn("Configured roles: 2", current)
        self.assertIn("<@&100>", current)
        self.assertIn("<@&200>", current)
        self.assertNotIn("(`100`)", current)
        self.assertNotIn("(`200`)", current)
        self.ctx.send_help.assert_not_awaited()

    async def test_stickyroles_group_hides_a_missing_role_id(self):
        self.ctx.author.guild_permissions.manage_guild = True
        self.guild.get_role = lambda _role_id: None
        self.cog._sticky_roles = types.SimpleNamespace(
            get_sticky_roles=mock.AsyncMock(return_value=frozenset({606}))
        )

        await nhmisc.NHMisc.nhmisc_stickyroles.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        current = next(
            field.value for field in embed.fields if field.name == "Current configuration"
        )
        self.assertIn("Configured role is missing", current)
        self.assertNotIn("606", current)

    async def test_sticky_debuglogging_group_shows_toggle_and_channel(self):
        self.ctx.author.guild_permissions.manage_guild = True
        self.channels[321] = types.SimpleNamespace(mention="<#321>")
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
        self.assertEqual(embed.title, "Sticky role debug logging")
        self.assertIn("Enabled: Yes", current)
        self.assertIn("Channel: <#321>", current)
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
        self.assertEqual(embed.title, "Role analytics")
        self.assertIn("Enabled: Yes", current)
        self.assertIn("Status: Ready", current)
        self.assertIn("Members in snapshot: 80,123", current)
        self.ctx.send_help.assert_not_awaited()

    async def test_nhmisc_root_shows_command_dashboard_instead_of_generic_help(self):
        nested = command_metadata("nhmisc activity retention", "<days>")
        self.ctx.command = types.SimpleNamespace(
            commands=(
                command_metadata("nhmisc alert"),
                command_metadata("nhmisc activity", children=(nested,)),
                command_metadata("nhmisc status"),
            )
        )

        await nhmisc.NHMisc.nhmisc.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "NHMisc")
        commands = fields["Commands"]
        self.assertIn("!nhmisc alert", commands)
        self.assertIn("!nhmisc activity", commands)
        self.assertIn("!nhmisc status", commands)
        self.assertNotIn("retention", commands)
        self.ctx.send_help.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
