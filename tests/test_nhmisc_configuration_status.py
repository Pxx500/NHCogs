import asyncio
import inspect
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


def command_metadata(
    qualified_name,
    signature="",
    *,
    hidden=False,
    children=(),
    short_doc="",
):
    return types.SimpleNamespace(
        qualified_name=qualified_name,
        signature=signature,
        hidden=hidden,
        commands=children,
        short_doc=short_doc,
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

    async def test_runtime_health_reports_a_stopped_required_task(self):
        pending_task = asyncio.create_task(asyncio.Event().wait())
        stopped_task = asyncio.create_task(asyncio.sleep(0))
        await stopped_task
        cog = types.SimpleNamespace(
            _activity_task=pending_task,
            _role_analytics_daily_task=stopped_task,
        )

        try:
            self.assertEqual(
                nhmisc.NHMisc.runtime_health_issues(cog),
                ("role analytics daily task stopped unexpectedly",),
            )
        finally:
            pending_task.cancel()
            await asyncio.gather(pending_task, return_exceptions=True)

    async def test_private_logs_stop_if_configured_channel_becomes_public(self):
        for config_key, sender_name in (
            ("maintenance_channel", "_send_maintenance_log"),
            ("moderation_log_channel", "_send_moderation_log"),
        ):
            with self.subTest(config_key=config_key):
                channel = types.SimpleNamespace(
                    permissions_for=lambda _target: types.SimpleNamespace(
                        view_channel=True
                    ),
                    send=mock.AsyncMock(),
                )
                self.cog.config = FakeConfig({config_key: 42})
                self.cog._get_log_channel = mock.Mock(return_value=channel)

                delivered = await getattr(self.cog, sender_name)(
                    self.guild,
                    "private log data",
                )

                self.assertFalse(delivered)
                channel.send.assert_not_awaited()

    async def test_maintenance_channel_requires_attach_files(self):
        self.guild.me = object()
        channel = types.SimpleNamespace(
            id=42,
            mention="<#42>",
            permissions_for=lambda target: types.SimpleNamespace(
                view_channel=target is self.guild.me,
                send_messages=True,
                attach_files=False,
            ),
        )
        self.cog.config = FakeConfig({"maintenance_channel": None})

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "attach files",
        ):
            await nhmisc.NHMisc.nhmisc_log_maintenance.callback(
                self.cog,
                self.ctx,
                channel,
            )

        self.assertIsNone(await self.cog.config.guild(self.guild).maintenance_channel())

    async def test_group_commands_expand_singletons_to_the_first_branch(self):
        self.ctx.author.guild_permissions.manage_guild = True
        terminal = command_metadata(
            "nhmisc stickyroles debuglogging toggle",
            "<enabled>",
        )
        singleton = command_metadata(
            "nhmisc stickyroles debuglogging",
            children=(terminal,),
        )
        hidden = command_metadata("nhmisc stickyroles internal", hidden=True)
        self.ctx.clean_prefix = "?"
        self.ctx.command = types.SimpleNamespace(commands=(singleton, hidden))
        self.cog._sticky_roles = types.SimpleNamespace(
            get_sticky_roles=mock.AsyncMock(return_value=frozenset())
        )

        await nhmisc.NHMisc.nhmisc_stickyroles.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        commands = fields["Commands"]
        self.assertIn(
            "?nhmisc stickyroles debuglogging toggle <enabled>",
            commands,
        )
        self.assertNotIn("`?nhmisc stickyroles debuglogging`", commands)
        self.assertNotIn("internal", commands)

    async def test_log_group_shows_all_destinations_and_complete_commands(self):
        self.channels.update(
            {
                41: types.SimpleNamespace(mention="<#41>"),
                42: types.SimpleNamespace(mention="<#42>"),
            }
        )
        self.cog.config = FakeConfig(
            {
                "alert_channel": 42,
                "voice_log_channel": 41,
                "maintenance_channel": None,
                "moderation_log_channel": None,
            }
        )
        self.ctx.command = types.SimpleNamespace(
            commands=(
                command_metadata("nhmisc log voice", "[channel]"),
                command_metadata("nhmisc log alert", "[channel]"),
                command_metadata("nhmisc log maintenance", "[channel]"),
                command_metadata("nhmisc log moderation", "[channel]"),
            )
        )

        await nhmisc.NHMisc.nhmisc_log.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "Logging")
        self.assertIn("Voice: <#41>", fields["Current configuration"])
        self.assertIn("Alert: <#42>", fields["Current configuration"])
        self.assertIn("Maintenance: Not configured", fields["Current configuration"])
        self.assertIn("Moderation: Not configured", fields["Current configuration"])
        for log_type in ("voice", "alert", "maintenance", "moderation"):
            self.assertIn(f"!nhmisc log {log_type} [channel]", fields["Commands"])

    async def test_log_child_without_channel_shows_current_destination(self):
        self.channels[42] = types.SimpleNamespace(mention="<#42>")
        cases = (
            ("voice", "voice_log_channel", "Voice logging"),
            ("alert", "alert_channel", "Alert logging"),
            ("maintenance", "maintenance_channel", "Maintenance logging"),
            ("moderation", "moderation_log_channel", "Moderator action logging"),
        )
        for command_name, config_key, title in cases:
            with self.subTest(command_name=command_name):
                self.ctx.send.reset_mock()
                self.cog.config = FakeConfig({config_key: 42})

                command = getattr(nhmisc.NHMisc, f"nhmisc_log_{command_name}")
                await command.callback(self.cog, self.ctx, None)

                embed = self.ctx.send.await_args.kwargs["embed"]
                fields = {field.name: field.value for field in embed.fields}
                self.assertEqual(embed.title, title)
                self.assertIn("Channel: <#42>", fields["Current configuration"])
                self.assertEqual(
                    await getattr(self.cog.config.guild(self.guild), config_key)(),
                    42,
                )

    async def test_log_child_with_channel_updates_destination(self):
        self.guild.me = object()
        channel = types.SimpleNamespace(
            id=73,
            mention="<#73>",
            permissions_for=lambda _target: types.SimpleNamespace(
                view_channel=True,
                send_messages=True,
                attach_files=True,
            ),
        )
        self.cog.config = FakeConfig({"alert_channel": None})

        await nhmisc.NHMisc.nhmisc_log_alert.callback(self.cog, self.ctx, channel)

        self.assertEqual(await self.cog.config.guild(self.guild).alert_channel(), 73)
        self.ctx.send.assert_awaited_once_with("Alert channel set to <#73>.")

    async def test_private_log_destinations_reject_public_channels(self):
        self.guild.me = object()
        channel = types.SimpleNamespace(
            id=73,
            mention="<#73>",
            permissions_for=lambda target: types.SimpleNamespace(
                view_channel=True,
                send_messages=True,
                attach_files=True,
            ),
        )
        for command_name, config_key in (
            ("maintenance", "maintenance_channel"),
            ("moderation", "moderation_log_channel"),
        ):
            with self.subTest(command_name=command_name):
                self.cog.config = FakeConfig({config_key: None})
                command = getattr(nhmisc.NHMisc, f"nhmisc_log_{command_name}")

                with self.assertRaisesRegex(
                    nhmisc.commands.UserFeedbackCheckFailure,
                    "private from @everyone",
                ):
                    await command.callback(self.cog, self.ctx, channel)

                self.assertIsNone(
                    await getattr(self.cog.config.guild(self.guild), config_key)()
                )

    async def test_public_channel_hides_log_configuration_but_keeps_commands(self):
        self.ctx.channel = FakeInvocationChannel(public=True)
        self.ctx.command = types.SimpleNamespace(
            commands=(command_metadata("nhmisc log alert", "[channel]"),)
        )
        self.channels[42] = types.SimpleNamespace(mention="<#42>")
        self.cog.config = FakeConfig(
            {
                "voice_log_channel": None,
                "alert_channel": 42,
                "maintenance_channel": None,
                "moderation_log_channel": None,
            }
        )

        await nhmisc.NHMisc.nhmisc_log.callback(self.cog, self.ctx)

        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertNotIn("<#42>", fields["Current configuration"])
        self.assertIn("hidden from @everyone", fields["Current configuration"])
        self.assertIn("!nhmisc log alert [channel]", fields["Commands"])

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

    async def test_sticky_debuglogging_group_uses_maintenance_channel(self):
        self.ctx.author.guild_permissions.manage_guild = True
        self.channels[321] = types.SimpleNamespace(mention="<#321>")
        self.cog.config = FakeConfig(
            {
                "sticky_debug_logging_enabled": True,
                "maintenance_channel": 321,
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
        self.assertIn("Maintenance channel: <#321>", current)
        self.assertFalse(
            hasattr(nhmisc.NHMisc, "nhmisc_stickyroles_debuglogging_channel")
        )
        self.ctx.send_help.assert_not_awaited()

    async def test_sticky_debug_output_uses_maintenance_channel(self):
        channel = types.SimpleNamespace(
            permissions_for=lambda _target: types.SimpleNamespace(view_channel=False),
            send=mock.AsyncMock(),
        )
        self.channels[321] = channel
        self.cog.config = FakeConfig(
            {
                "sticky_debug_logging_enabled": True,
                "alert_channel": 999,
                "maintenance_channel": 321,
            }
        )
        self.cog._get_log_channel = mock.Mock(return_value=channel)

        await self.cog._send_sticky_debug_log(self.guild, "Sticky role restored")

        self.cog._get_log_channel.assert_called_once_with(self.guild, 321)
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.args, ("Sticky role restored",))

    async def test_sticky_debug_output_stops_if_maintenance_channel_becomes_public(self):
        channel = types.SimpleNamespace(
            permissions_for=lambda _target: types.SimpleNamespace(view_channel=True),
            send=mock.AsyncMock(),
        )
        self.cog.config = FakeConfig(
            {
                "sticky_debug_logging_enabled": True,
                "maintenance_channel": 321,
            }
        )
        self.cog._get_log_channel = mock.Mock(return_value=channel)

        await self.cog._send_sticky_debug_log(self.guild, "private sticky data")

        channel.send.assert_not_awaited()

    async def test_deleted_sticky_role_prompt_uses_maintenance_channel_even_if_debug_is_off(
        self,
    ):
        channel = types.SimpleNamespace(
            permissions_for=lambda _target: types.SimpleNamespace(view_channel=False),
            send=mock.AsyncMock(),
        )
        self.channels[321] = channel
        self.cog.config = FakeConfig(
            {
                "sticky_debug_logging_enabled": False,
                "alert_channel": 999,
                "maintenance_channel": 321,
            }
        )
        self.cog._sticky_roles = types.SimpleNamespace(
            get_role_state=mock.AsyncMock(return_value=(True, 2))
        )
        self.cog._achievement_store = types.SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=())
        )
        self.cog._get_log_channel = mock.Mock(return_value=channel)
        self.cog._prompt_sticky_role_db_action = mock.AsyncMock()
        role = types.SimpleNamespace(
            id=456,
            name="Sticky",
            guild=self.guild,
        )

        await self.cog.on_guild_role_delete(role)

        self.cog._get_log_channel.assert_called_once_with(self.guild, 321)
        self.cog._prompt_sticky_role_db_action.assert_awaited_once()
        self.assertIs(
            self.cog._prompt_sticky_role_db_action.await_args.kwargs["channel"],
            channel,
        )

    async def test_deleted_sticky_role_prompt_stops_if_channel_becomes_public(self):
        channel = types.SimpleNamespace(
            permissions_for=lambda _target: types.SimpleNamespace(view_channel=True),
        )
        self.cog.config = FakeConfig({"maintenance_channel": 321})
        self.cog._sticky_roles = types.SimpleNamespace(
            get_role_state=mock.AsyncMock(return_value=(True, 2))
        )
        self.cog._achievement_store = types.SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=())
        )
        self.cog._get_log_channel = mock.Mock(return_value=channel)
        self.cog._prompt_sticky_role_db_action = mock.AsyncMock()
        role = types.SimpleNamespace(id=456, name="Sticky", guild=self.guild)

        await self.cog.on_guild_role_delete(role)

        self.cog._prompt_sticky_role_db_action.assert_not_awaited()

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
        log_children = (
            command_metadata("nhmisc log voice", "[channel]"),
            command_metadata("nhmisc log alert", "[channel]"),
        )
        roleanalytics_disable = command_metadata("nhmisc roleanalytics disable")
        self.ctx.command = types.SimpleNamespace(
            commands=(
                command_metadata("nhmisc log", children=log_children),
                command_metadata(
                    "nhmisc roleanalytics",
                    children=(roleanalytics_disable,),
                ),
                command_metadata(
                    "nhmisc activity",
                    children=(
                        command_metadata("nhmisc activity retention", "<days>"),
                        command_metadata("nhmisc activity current"),
                    ),
                ),
            )
        )

        await nhmisc.NHMisc.nhmisc.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "NHMisc")
        commands = fields["Commands"]
        self.assertIn("!nhmisc log", commands)
        self.assertIn("!nhmisc roleanalytics disable", commands)
        self.assertIn("!nhmisc activity", commands)
        self.assertNotIn("retention", commands)
        self.assertNotIn("!nhmisc status", commands)
        self.assertNotIn("!nhmisc channel", commands)
        self.ctx.send_help.assert_not_awaited()

    async def test_achievement_group_explains_every_proof_and_profile_entry_point(self):
        nested = command_metadata(
            "achievement revoke confirm",
            "<members...>",
        )
        self.ctx.command = types.SimpleNamespace(
            commands=(
                command_metadata(
                    "achievement proof",
                    "<message_link>",
                ),
                command_metadata(
                    "achievement revoke",
                    children=(nested,),
                ),
            )
        )

        await nhmisc.NHMisc.achievement.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "Achievements")
        self.assertIn("/achievements", embed.description)
        self.assertIn("Apps → View achievements", embed.description)
        self.assertIn("Apps → Grant achievements", embed.description)
        self.assertIn("Apps → Increment Gate roles", embed.description)
        self.assertIn("Apps → Add Gate Proof", embed.description)
        self.assertIn("/gaterevoke", embed.description)
        self.assertIn("Apps → Revoke Gate", embed.description)
        self.assertIn("!achievement proof <message_link>", embed.description)
        self.assertIn("!achievement proof <message_link>", fields["Commands"])
        self.assertIn("!achievement revoke", fields["Commands"])
        self.assertNotIn("confirm", fields["Commands"])
        self.assertEqual(
            self.ctx.send.await_args.kwargs["allowed_mentions"],
            nhmisc.discord.AllowedMentions.none(),
        )
        self.ctx.send_help.assert_not_awaited()

    def test_achievement_proof_command_keeps_its_permission_and_link_contract(self):
        callback = nhmisc.NHMisc.achievement_proof.callback
        parameters = inspect.signature(callback).parameters

        self.assertIn("message_link", parameters)
        self.assertEqual(parameters["message_link"].annotation, "str")
        self.assertEqual(callback.required_permissions, {"manage_messages": True})

    async def test_achievement_role_group_lists_its_direct_commands(self):
        self.ctx.command = types.SimpleNamespace(
            commands=(
                command_metadata(
                    "achievement role bind",
                    "<role>",
                    short_doc="Bind an existing Discord role to an achievement",
                ),
                command_metadata(
                    "achievement role unbind",
                    "<role>",
                    short_doc="Stop tracking an achievement role",
                ),
                command_metadata(
                    "achievement role replace",
                    "<old_role> <new_role>",
                    short_doc="Replace an achievement role binding",
                ),
                command_metadata(
                    "achievement role list",
                    short_doc="List active achievement role bindings",
                ),
            )
        )

        await nhmisc.NHMisc.achievement_role.callback(self.cog, self.ctx)

        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Achievement roles")
        self.assertIn("Discord role bindings", embed.description)
        commands = embed.fields[0].value
        self.assertEqual(
            commands.splitlines(),
            [
                "`!achievement role bind <role>`",
                "Bind an existing Discord role to an achievement",
                "`!achievement role unbind <role>`",
                "Stop tracking an achievement role",
                "`!achievement role replace <old_role> <new_role>`",
                "Replace an achievement role binding",
                "`!achievement role list`",
                "List active achievement role bindings",
            ],
        )
        self.assertEqual(
            self.ctx.send.await_args.kwargs["allowed_mentions"],
            nhmisc.discord.AllowedMentions.none(),
        )
        self.ctx.send_help.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
