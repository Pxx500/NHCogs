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


class _OverviewEmbed:
    def __init__(self, *, title=None, description=None):
        self.title = title
        self.description = description
        self.fields = []

    def add_field(self, *, name, value, inline):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


class _OverviewAllowedMentions:
    marker = object()

    @classmethod
    def none(cls):
        return cls.marker


def _registered_command_tree(honeypot, root):
    registered = [
        value
        for value in vars(honeypot.Honeypot).values()
        if getattr(value, "kind", None) in {"command", "group"}
    ]

    def build(command):
        children = [
            build(candidate)
            for candidate in registered
            if getattr(candidate, "parent", None) is command
        ]
        doc = (command.callback.__doc__ or "").strip().splitlines()
        return SimpleNamespace(
            name=command.name,
            qualified_name=command.qualified_name,
            signature="",
            short_doc=doc[0] if doc else "",
            commands=children,
        )

    return build(root)


def _leaf_command_names(command):
    if not command.commands:
        return [command.qualified_name]
    return [
        leaf
        for child in command.commands
        for leaf in _leaf_command_names(child)
    ]


class _ListSetting:
    def __init__(self, values):
        self.values = values

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.values

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ScalarSetting:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        async def read():
            return self.value

        return read()

    async def set(self, value):
        self.value = value


class RoleNtSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_registers_one_role_for_multiple_source_channels(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                configured = _ScalarSetting({})
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        manual_punishment_roles=configured,
                    )
                )
                cog._missing_role_assignment_permission = mock.Mock(
                    return_value=None
                )
                role = SimpleNamespace(id=500, name="French-n’t", mention="@French-n’t")
                channels = [
                    SimpleNamespace(id=100, name="french"),
                    SimpleNamespace(id=101, name="french-help"),
                ]
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    send=mock.AsyncMock(),
                )

                await cog.punishment_role_nt_add(ctx, role, channels)

                self.assertEqual(
                    configured.value,
                    {
                        "500": {
                            "source_channel_ids": [100, 101],
                            "notification_channel_id": None,
                        }
                    },
                )
                rendered = ctx.send.await_args.args[0]
                self.assertIn("French-n’t", rendered)
                self.assertIn("#french", rendered)
                self.assertIn("#french-help", rendered)

    async def test_list_paginates_large_role_nt_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                configured = _ScalarSetting(
                    {
                        str(role_id): {
                            "source_channel_ids": list(range(100, 125)),
                            "notification_channel_id": None,
                        }
                        for role_id in range(500, 525)
                    }
                )
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        manual_punishment_roles=configured,
                    )
                )
                guild = SimpleNamespace(
                    get_role=lambda role_id: SimpleNamespace(
                        id=role_id,
                        name=f"role-{role_id}-{'x' * 80}",
                    ),
                    get_channel=lambda channel_id: (
                        SimpleNamespace(
                            id=channel_id,
                            name=f"channel-{channel_id}-{'y' * 60}",
                        )
                        if channel_id is not None
                        else None
                    ),
                )
                ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())

                await cog.punishment_role_nt_list(ctx)

                pages = [call.args[0] for call in ctx.send.await_args_list]
                self.assertGreater(len(pages), 1)
                self.assertTrue(all(len(page) <= 2_000 for page in pages))
                rendered = "\n".join(pages)
                self.assertIn("role-500-", rendered)
                self.assertIn("role-524-", rendered)


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
                honeypot.joinwatch_state.store_alert_reference = mock.AsyncMock()
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


class GroupOverviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_channels_overview_lists_categories_and_active_prefix_commands(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                cog.config = SimpleNamespace(
                    guild=mock.Mock(
                        return_value=SimpleNamespace(
                            all=mock.AsyncMock(
                                return_value=dict(honeypot.settings.DEFAULTS)
                            )
                        )
                    )
                )
                cog._format_channel_setting = mock.Mock(
                    return_value="Not configured"
                )
                cog._send_config_dump = mock.AsyncMock()
                ctx = SimpleNamespace(guild=object(), clean_prefix="??")

                await honeypot.Honeypot.channels.callback(cog, ctx)

                cog._send_config_dump.assert_awaited_once()
                entries = cog._send_config_dump.await_args.args[2]
                rendered = "\n".join(
                    f"{label}\n{value}" for label, value in entries
                )
                self.assertIn("Destinations", rendered)
                self.assertIn("Sources and scopes", rendered)
                self.assertIn("Review: Not configured", rendered)
                self.assertIn("Errors: Not configured", rendered)
                self.assertIn("Daily stats: Not configured", rendered)
                self.assertIn("GIF debug logging: false", rendered)
                self.assertIn("??honeypot channels review [channel]", rendered)
                self.assertIn(
                    "??honeypot channels daily-stats [channel]", rendered
                )
                self.assertIn("??honeypot channels gif-debug [channel]", rendered)
                self.assertIn(
                    "??honeypot channels honeypot add <channel>", rendered
                )
                self.assertIn(
                    "??honeypot channels gif-detector remove [channel]", rendered
                )

    async def test_channels_overview_uses_names_without_repeating_channel_ids(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                channel = SimpleNamespace(
                    id=77,
                    name="automod-filter",
                    mention="<#77>",
                )
                guild = SimpleNamespace(
                    get_channel=mock.Mock(
                        side_effect=lambda channel_id: (
                            channel if channel_id == channel.id else None
                        )
                    )
                )
                configured = dict(honeypot.settings.DEFAULTS)
                configured["errors_channel"] = channel.id
                cog = object.__new__(honeypot.Honeypot)
                cog.bot = SimpleNamespace(get_channel=mock.Mock(return_value=None))
                cog.config = SimpleNamespace(
                    guild=mock.Mock(
                        return_value=SimpleNamespace(
                            all=mock.AsyncMock(return_value=configured)
                        )
                    )
                )
                cog._send_config_dump = mock.AsyncMock()
                ctx = SimpleNamespace(guild=guild, clean_prefix="!")

                await honeypot.Honeypot.channels.callback(cog, ctx)

                entries = cog._send_config_dump.await_args.args[2]
                rendered = "\n".join(
                    f"{label}\n{value}" for label, value in entries
                )
                self.assertIn("Errors: #automod-filter", rendered)
                self.assertNotIn("<#77>", rendered)
                self.assertNotIn("(77)", rendered)

    async def test_gif_scope_paths_share_permission_validation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                values = []
                guild_config = SimpleNamespace(
                    gif_detector_channels=_ListSetting(values)
                )
                cog = object.__new__(honeypot.Honeypot)
                cog.config = SimpleNamespace(
                    guild=mock.Mock(return_value=guild_config)
                )
                cog._missing_channel_permissions = mock.Mock(
                    return_value="I need `Manage Messages` in #gifs."
                )
                target = SimpleNamespace(
                    id=12,
                    parent_id=10,
                    parent=SimpleNamespace(id=10, mention="#gifs"),
                    mention="#thread",
                )
                ctx = SimpleNamespace(
                    guild=object(),
                    channel=target,
                    send=mock.AsyncMock(),
                )

                for command in (
                    honeypot.Honeypot.gif_detector_channel_add,
                    honeypot.Honeypot.channels_gif_detector_add,
                ):
                    with self.subTest(command=command.qualified_name):
                        with self.assertRaises(
                            honeypot.commands.UserFeedbackCheckFailure
                        ):
                            await command.callback(cog, ctx, target)

                self.assertEqual(values, [])
                self.assertEqual(cog._missing_channel_permissions.call_count, 2)

    async def test_deleted_channel_cleanup_clears_all_registered_references(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                deleted_id = 44
                settings = {}
                for category in honeypot.channel_routing.CHANNEL_CATEGORIES:
                    if category.cardinality == "single":
                        value = deleted_id if category.key in {
                            "errors",
                            "gif_debug",
                        } else 99
                        settings[category.config_field] = _ScalarSetting(value)
                    else:
                        values = (
                            [11, deleted_id, 55, deleted_id]
                            if category.key == "honeypot_scope"
                            else [11, 55]
                        )
                        settings[category.config_field] = _ListSetting(values)
                cog = SimpleNamespace(
                    config=SimpleNamespace(
                        guild=mock.Mock(return_value=SimpleNamespace(**settings))
                    )
                )
                channel = SimpleNamespace(
                    id=deleted_id,
                    guild=SimpleNamespace(id=123),
                )

                await honeypot.channel_routing.clear_deleted_channel(cog, channel)

                self.assertIsNone(settings["errors_channel"].value)
                self.assertIsNone(settings["gif_detector_debug_channel"].value)
                self.assertEqual(settings["review_channel"].value, 99)
                self.assertEqual(settings["honeypot_channels"].values, [11, 55])
                self.assertEqual(settings["gif_detector_channels"].values, [11, 55])

    async def test_operational_alerts_use_only_the_errors_destination(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild = object()
                channel = SimpleNamespace(send=mock.AsyncMock())
                configured = dict(honeypot.settings.DEFAULTS)
                configured["errors_channel"] = 77
                configured["review_channel"] = 88
                cog = object.__new__(honeypot.Honeypot)
                cog.bot = SimpleNamespace(get_guild=mock.Mock(return_value=guild))
                cog.config = SimpleNamespace(
                    guild_from_id=mock.Mock(
                        return_value=SimpleNamespace(
                            all=mock.AsyncMock(return_value=configured)
                        )
                    )
                )
                cog._get_text_channel_or_thread = mock.Mock(return_value=channel)

                await honeypot.Honeypot._send_operational_alert(cog, 123, "failure")

                cog._get_text_channel_or_thread.assert_called_once_with(guild, 77)
                channel.send.assert_awaited_once()
                args, kwargs = channel.send.await_args
                self.assertEqual(args[0], "failure")
                self.assertFalse(kwargs["allowed_mentions"].users)

    async def test_operational_alert_mentions_only_the_configured_maintainer(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild = object()
                channel = SimpleNamespace(send=mock.AsyncMock())
                configured = dict(honeypot.settings.DEFAULTS)
                configured["errors_channel"] = 77
                configured["maintainer_id"] = 555
                cog = object.__new__(honeypot.Honeypot)
                cog.bot = SimpleNamespace(get_guild=mock.Mock(return_value=guild))
                cog.config = SimpleNamespace(
                    guild_from_id=mock.Mock(
                        return_value=SimpleNamespace(
                            all=mock.AsyncMock(return_value=configured)
                        )
                    )
                )
                cog._get_text_channel_or_thread = mock.Mock(return_value=channel)

                with mock.patch.object(
                    honeypot.discord,
                    "Object",
                    side_effect=lambda *, id: SimpleNamespace(id=id),
                ):
                    await honeypot.Honeypot._send_operational_alert(
                        cog, 123, "failure"
                    )

                args, kwargs = channel.send.await_args
                self.assertEqual(args[0], "<@555> failure")
                mentions = kwargs["allowed_mentions"]
                self.assertFalse(mentions.everyone)
                self.assertFalse(mentions.roles)
                self.assertFalse(mentions.replied_user)
                self.assertEqual([user.id for user in mentions.users], [555])

    async def test_error_maintainer_command_sets_shows_and_clears_member(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                setting = _ScalarSetting(None)
                cog = object.__new__(honeypot.Honeypot)
                cog.config = SimpleNamespace(
                    guild=mock.Mock(
                        return_value=SimpleNamespace(maintainer_id=setting)
                    )
                )
                member = SimpleNamespace(id=555, mention="<@555>")
                guild = SimpleNamespace(
                    get_member=mock.Mock(
                        side_effect=lambda member_id: (
                            member if member_id == member.id else None
                        )
                    )
                )
                ctx = SimpleNamespace(
                    guild=guild,
                    clean_prefix="??",
                    send=mock.AsyncMock(),
                )

                await honeypot.Honeypot.honeypot_errors_maintainer_set.callback(
                    cog, ctx, member
                )
                self.assertEqual(setting.value, 555)

                ctx.send.reset_mock()
                await honeypot.Honeypot.honeypot_errors_maintainer_show.callback(
                    cog, ctx
                )
                rendered = ctx.send.await_args.args[0]
                self.assertIn("Error maintainer: <@555>", rendered)
                self.assertIn(
                    "??honeypot errors maintainer set <member>", rendered
                )
                self.assertIn("??honeypot errors maintainer clear", rendered)

                await honeypot.Honeypot.honeypot_errors_maintainer_clear.callback(
                    cog, ctx
                )
                self.assertIsNone(setting.value)

    async def test_public_group_shows_runtime_syntax_without_reading_config(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                configure = mock.AsyncMock()
                honeypot.detection.config_spam = configure
                direct_command = SimpleNamespace(
                    qualified_name="honeypot spam window",
                    signature="[seconds]",
                    short_doc="Inspect or change the detection window.",
                )
                nested_group = SimpleNamespace(
                    qualified_name="honeypot spam advanced",
                    signature="",
                    short_doc="Advanced settings.",
                    commands=[
                        SimpleNamespace(
                            qualified_name="honeypot spam advanced hidden",
                            signature="",
                            short_doc="Grandchild command.",
                        )
                    ],
                )
                ctx = SimpleNamespace(
                    clean_prefix="??",
                    command=SimpleNamespace(
                        name="spam",
                        short_doc="Configure duplicate-message spam detection.",
                        commands=[direct_command, nested_group],
                    ),
                    guild=SimpleNamespace(default_role=object()),
                    channel=SimpleNamespace(
                        permissions_for=lambda role: SimpleNamespace(view_channel=True)
                    ),
                    send=mock.AsyncMock(),
                )

                with (
                    mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        _OverviewAllowedMentions,
                    ),
                ):
                    await honeypot.Honeypot.spam.callback(cog, ctx)

                configure.assert_not_awaited()
                ctx.send.assert_awaited_once()
                kwargs = ctx.send.await_args.kwargs
                embed = kwargs["embed"]
                rendered = "\n".join(field.value for field in embed.fields)
                self.assertIn("??honeypot spam window [seconds]", rendered)
                self.assertIn("??honeypot spam advanced hidden", rendered)
                self.assertIs(
                    kwargs["allowed_mentions"],
                    _OverviewAllowedMentions.marker,
                )

    async def test_private_nested_group_reuses_parent_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                configure = mock.AsyncMock()
                honeypot.joinwatch_commands.config_joinwatch = configure
                ctx = SimpleNamespace(
                    clean_prefix="!",
                    command=SimpleNamespace(
                        name="autorole",
                        short_doc="Configure automatic young-account role assignment.",
                        commands=[
                            SimpleNamespace(
                                qualified_name="honeypot joinwatch autorole timer",
                                signature="[minutes]",
                                short_doc="Inspect or change the timer.",
                            )
                        ],
                    ),
                    guild=SimpleNamespace(default_role=object()),
                    channel=SimpleNamespace(
                        permissions_for=lambda role: SimpleNamespace(view_channel=False)
                    ),
                    send=mock.AsyncMock(),
                )

                with (
                    mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        _OverviewAllowedMentions,
                    ),
                ):
                    await honeypot.Honeypot.joinwatch_autorole.callback(cog, ctx)

                configure.assert_awaited_once_with(cog, ctx)
                embed = ctx.send.await_args.kwargs["embed"]
                rendered = "\n".join(field.value for field in embed.fields)
                self.assertIn(
                    "!honeypot joinwatch autorole timer [minutes]",
                    rendered,
                )
                self.assertNotIn("Current values are hidden", rendered)

    async def test_long_group_overview_keeps_every_command_within_embed_limits(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                commands = [
                    SimpleNamespace(
                        qualified_name=f"honeypot section command-{index}",
                        signature="<value>",
                        short_doc="Configure a deliberately verbose command option safely.",
                    )
                    for index in range(100)
                ]
                ctx = SimpleNamespace(
                    clean_prefix="!",
                    command=SimpleNamespace(
                        name="section",
                        short_doc="Configure a large command section.",
                        commands=commands,
                    ),
                    guild=SimpleNamespace(default_role=object()),
                    channel=SimpleNamespace(
                        permissions_for=lambda role: SimpleNamespace(view_channel=True)
                    ),
                    send=mock.AsyncMock(),
                )

                with (
                    mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        _OverviewAllowedMentions,
                    ),
                ):
                    await honeypot.Honeypot._send_group_overview(cog, ctx)

                self.assertGreater(ctx.send.await_count, 1)
                rendered = ""
                for call in ctx.send.await_args_list:
                    embed = call.kwargs["embed"]
                    self.assertLessEqual(len(embed.fields), 25)
                    text_length = len(embed.title or "") + len(embed.description or "")
                    text_length += sum(
                        len(field.name) + len(field.value) for field in embed.fields
                    )
                    self.assertLessEqual(text_length, 6000)
                    rendered += "\n".join(field.value for field in embed.fields)
                for index in range(100):
                    self.assertIn(
                        f"!honeypot section command-{index} <value>",
                        rendered,
                    )

    async def test_root_overview_lists_direct_categories_without_deep_commands(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                command = _registered_command_tree(
                    honeypot,
                    honeypot.Honeypot.honeypot,
                )
                ctx = SimpleNamespace(
                    clean_prefix="!",
                    command=command,
                    guild=SimpleNamespace(default_role=object()),
                    channel=SimpleNamespace(
                        permissions_for=lambda role: SimpleNamespace(view_channel=True)
                    ),
                    send=mock.AsyncMock(),
                )

                with (
                    mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        _OverviewAllowedMentions,
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "config_all",
                        mock.AsyncMock(),
                    ),
                ):
                    await honeypot.Honeypot.honeypot(cog, ctx)

                rendered = "\n".join(
                    field.value
                    for call in ctx.send.await_args_list
                    for field in call.kwargs["embed"].fields
                )
                descriptions = "\n".join(
                    call.kwargs["embed"].description or ""
                    for call in ctx.send.await_args_list
                )
                direct_names = [child.qualified_name for child in command.commands]
                deep_names = [
                    leaf
                    for child in command.commands
                    for leaf in _leaf_command_names(child)
                    if leaf not in direct_names
                ]
                self.assertGreater(len(deep_names), 0)
                for qualified_name in direct_names:
                    self.assertIn(f"!{qualified_name}", rendered)
                for qualified_name in deep_names:
                    self.assertNotIn(f"!{qualified_name}", rendered)
                self.assertIn("Run a category below", descriptions)

    async def test_action_bearing_groups_become_namespace_overviews(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                cog._send_group_overview = mock.AsyncMock()
                ctx = SimpleNamespace()

                with (
                    mock.patch.object(
                        honeypot.manual_punishment,
                        "show_status",
                        new=mock.AsyncMock(),
                    ) as evidence_status,
                    mock.patch.object(
                        honeypot.channel_routing,
                        "list_multiple",
                        new=mock.AsyncMock(),
                    ) as channel_list,
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_errors",
                        new=mock.AsyncMock(),
                    ) as errors_list,
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_errors_maintainer_show",
                        new=mock.AsyncMock(),
                    ) as maintainer_show,
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_errors_maintainer_set",
                        new=mock.AsyncMock(),
                    ) as maintainer_set,
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_stats",
                        new=mock.AsyncMock(),
                    ) as stats,
                ):
                    groups = (
                        honeypot.Honeypot.manual_evidence_settings,
                        honeypot.Honeypot.channels_honeypot,
                        honeypot.Honeypot.channels_gif_detector,
                        honeypot.Honeypot.honeypot_errors_group,
                        honeypot.Honeypot.honeypot_errors_maintainer_group,
                        honeypot.Honeypot.honeypot_stats_group,
                    )
                    action_mocks = (
                        evidence_status,
                        channel_list,
                        errors_list,
                        maintainer_show,
                        maintainer_set,
                        stats,
                    )
                    for group in groups:
                        with self.subTest(group=group.qualified_name):
                            cog._send_group_overview.reset_mock()
                            for action in action_mocks:
                                action.reset_mock()

                            await group.callback(cog, ctx)

                            cog._send_group_overview.assert_awaited_once_with(ctx)
                            for action in action_mocks:
                                action.assert_not_awaited()

    async def test_moved_group_actions_are_available_as_leaf_commands(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                expected_commands = {
                    "honeypot_errors": "honeypot errors list",
                    "honeypot_errors_maintainer_show": (
                        "honeypot errors maintainer show"
                    ),
                    "honeypot_errors_maintainer_set": (
                        "honeypot errors maintainer set"
                    ),
                    "honeypot_stats": "honeypot stats show",
                }
                for attribute, qualified_name in expected_commands.items():
                    with self.subTest(command=qualified_name):
                        self.assertTrue(hasattr(honeypot.Honeypot, attribute))
                        self.assertEqual(
                            getattr(honeypot.Honeypot, attribute).qualified_name,
                            qualified_name,
                        )

                cog = object.__new__(honeypot.Honeypot)
                ctx = SimpleNamespace()
                member = SimpleNamespace(id=123)
                with (
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_errors",
                        new=mock.AsyncMock(),
                    ) as errors_list,
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_errors_maintainer_show",
                        new=mock.AsyncMock(),
                    ) as maintainer_show,
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_errors_maintainer_set",
                        new=mock.AsyncMock(),
                    ) as maintainer_set,
                    mock.patch.object(
                        honeypot.diagnostics,
                        "honeypot_stats",
                        new=mock.AsyncMock(),
                    ) as stats,
                ):
                    await honeypot.Honeypot.honeypot_errors.callback(cog, ctx)
                    await honeypot.Honeypot.honeypot_errors_maintainer_show.callback(
                        cog, ctx
                    )
                    await honeypot.Honeypot.honeypot_errors_maintainer_set.callback(
                        cog, ctx, member
                    )
                    await honeypot.Honeypot.honeypot_stats.callback(cog, ctx)

                errors_list.assert_awaited_once_with(cog, ctx)
                maintainer_show.assert_awaited_once_with(cog, ctx)
                maintainer_set.assert_awaited_once_with(cog, ctx, member)
                stats.assert_awaited_once_with(cog, ctx)

    async def test_every_applicable_bare_group_sends_an_overview(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                groups = sorted(
                    (
                        value
                        for value in vars(honeypot.Honeypot).values()
                        if getattr(value, "kind", None) == "group"
                        and value is not honeypot.Honeypot.channels
                    ),
                    key=lambda group: group.qualified_name,
                )
                with (
                    mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        _OverviewAllowedMentions,
                    ),
                ):
                    for group in groups:
                        with self.subTest(group=group.qualified_name):
                            ctx = SimpleNamespace(
                                clean_prefix="!",
                                command=SimpleNamespace(
                                    name=group.name,
                                    short_doc="Group purpose.",
                                    commands=[
                                        SimpleNamespace(
                                            qualified_name=(
                                                f"{group.qualified_name} child"
                                            ),
                                            signature="",
                                            short_doc="Child purpose.",
                                        )
                                    ],
                                ),
                                guild=SimpleNamespace(default_role=object()),
                                channel=SimpleNamespace(
                                    permissions_for=lambda role: SimpleNamespace(
                                        view_channel=True
                                    )
                                ),
                                send=mock.AsyncMock(),
                            )

                            await group.callback(cog, ctx)

                            ctx.send.assert_awaited_once()
                            self.assertIn("embed", ctx.send.await_args.kwargs)
