import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.githubtickets_loader import isolated_githubtickets_modules

COMMAND_SIGNATURES = {
    "githubtickets github receiver set": "<host> <port>",
    "githubtickets github recovery interval": "<duration>",
    "githubtickets channel set": "<channel>",
    "githubtickets logchannel set": "<channel>",
    "githubtickets role add": "<role>",
    "githubtickets role remove": "<role>",
    "githubtickets category add": "<name>",
    "githubtickets category rename": "<old_name> <new_name>",
    "githubtickets category remove": "<name>",
    "githubtickets maxpings": "<count>",
    "githubtickets timing protection": "<duration>",
    "githubtickets timing volunteer": "<duration>",
    "githubtickets timing online": "<duration>",
    "githubtickets timing idle": "<duration>",
    "githubtickets timing donotdisturb": "<duration>",
    "githubtickets timing offline": "<duration>",
    "githubtickets timing direct": "<duration>",
    "githubtickets profile clear": "<user_id>",
}


class _OverviewEmbed:
    def __init__(self, *, title=None, description=None):
        self.title = title
        self.description = description
        self.fields = []

    def add_field(self, *, name, value, inline):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


def _registered_command_tree(cog_type, root):
    commands = tuple(cog_type.__cog_commands__)

    def clone(command):
        docstring = command.callback.__doc__ or ""
        return SimpleNamespace(
            name=command.name,
            qualified_name=command.qualified_name,
            signature=COMMAND_SIGNATURES.get(command.qualified_name, ""),
            short_doc=docstring.strip().splitlines()[0] if docstring.strip() else "",
            commands=[
                clone(child)
                for child in commands
                if getattr(child, "parent", None) is command
            ],
        )

    return clone(root)


def _leaf_command_names(command):
    if not command.commands:
        return [command.qualified_name]
    return [
        leaf
        for child in command.commands
        for leaf in _leaf_command_names(child)
    ]


class FakeContext:
    def __init__(self, guild_id=42, *, manage_messages=True, private=True):
        default_role = object()
        self.guild = (
            SimpleNamespace(id=guild_id, default_role=default_role)
            if guild_id is not None
            else None
        )
        self.author = SimpleNamespace()

        def permissions_for(target):
            if target is default_role:
                return SimpleNamespace(view_channel=not private)
            return SimpleNamespace(manage_messages=manage_messages)

        self.channel = SimpleNamespace(permissions_for=permissions_for)
        self.clean_prefix = "??"
        self.command = None
        self.send = mock.AsyncMock()


class GitHubTicketsCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_path = Path(self.temporary_directory.name)

    async def test_registers_only_the_accepted_prefix_command_tree(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            commands = {
                command.qualified_name: command
                for command in modules.githubtickets.GitHubTickets.__cog_commands__
            }

        self.assertEqual(
            set(commands),
            {
                "githubtickets",
                "githubtickets channel",
                "githubtickets channel set",
                "githubtickets channel clear",
                "githubtickets logchannel",
                "githubtickets logchannel set",
                "githubtickets logchannel clear",
                "githubtickets role",
                "githubtickets role add",
                "githubtickets role remove",
                "githubtickets category",
                "githubtickets category add",
                "githubtickets category rename",
                "githubtickets category remove",
                "githubtickets maxpings",
                "githubtickets timing",
                "githubtickets timing protection",
                "githubtickets timing volunteer",
                "githubtickets timing online",
                "githubtickets timing idle",
                "githubtickets timing donotdisturb",
                "githubtickets timing offline",
                "githubtickets timing direct",
                "githubtickets profile",
                "githubtickets profile clear",
                "githubtickets github",
                "githubtickets github enable",
                "githubtickets github disable",
                "githubtickets github receiver",
                "githubtickets github receiver set",
                "githubtickets github receiver clear",
                "githubtickets github recovery",
                "githubtickets github recovery interval",
                "githubtickets github recovery run",
            },
        )
        self.assertEqual(
            commands["githubtickets"].callback.__doc__,
            "Configure GitHub Tickets",
        )
        self.assertEqual(
            commands["githubtickets timing donotdisturb"].callback.__doc__,
            "Set the Do Not Disturb response time",
        )

    async def test_cog_check_guards_every_prefix_command_in_guilds(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            commands = modules.githubtickets.GitHubTickets.__cog_commands__

            for command in commands:
                with self.subTest(command=command.qualified_name):
                    self.assertFalse(
                        await cog.cog_check(FakeContext(manage_messages=False))
                    )
                    self.assertFalse(await cog.cog_check(FakeContext(guild_id=None)))
                    self.assertTrue(
                        await cog.cog_check(FakeContext(manage_messages=True))
                    )

    async def test_bare_group_renders_the_accepted_overview_without_mentions(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            await cog.store.initialize()
            await cog.config.guild_from_id(42).set_raw(
                "ticket_channel_id", value=100
            )
            await cog.config.guild_from_id(42).set_raw(
                "participant_role_ids", value=[200, 201]
            )
            await cog.config.guild_from_id(42).set_raw(
                "log_channel_id", value=101
            )
            await cog.store.add_category(
                42,
                "Rendering",
                datetime.now(timezone.utc),
            )
            ctx = FakeContext()
            ctx.command = _registered_command_tree(
                modules.githubtickets.GitHubTickets,
                modules.githubtickets.GitHubTickets.githubtickets,
            )

            with mock.patch.object(
                modules.githubtickets.discord,
                "Embed",
                _OverviewEmbed,
            ):
                await cog.githubtickets(ctx)

        self.assertEqual(ctx.send.await_count, 2)
        configuration = ctx.send.await_args_list[0].args[0]
        self.assertIn("Ticket channel: <#100>", configuration)
        self.assertIn("Log channel: <#101>", configuration)
        self.assertIn("Participant roles: <@&200>, <@&201>", configuration)
        self.assertIn("Categories: rendering", configuration)
        self.assertIn("Maximum pings: 3", configuration)

        command_embed = ctx.send.await_args_list[1].kwargs["embed"]
        self.assertEqual(command_embed.title, "GitHub Tickets")
        rendered_commands = "\n".join(field.value for field in command_embed.fields)
        direct_names = [child.qualified_name for child in ctx.command.commands]
        deep_names = [
            leaf
            for child in ctx.command.commands
            for leaf in _leaf_command_names(child)
            if leaf not in direct_names
        ]
        for qualified_name in direct_names:
            self.assertIn(f"??{qualified_name}", rendered_commands)
        for qualified_name in deep_names:
            self.assertNotIn(f"??{qualified_name}", rendered_commands)
        self.assertIn("??githubtickets maxpings <count>", rendered_commands)
        self.assertIn("Run a category below", command_embed.description)

        for call in ctx.send.await_args_list:
            allowed_mentions = call.kwargs["allowed_mentions"]
            self.assertFalse(allowed_mentions.users)
            self.assertFalse(allowed_mentions.roles)
            self.assertFalse(allowed_mentions.everyone)

    async def test_public_bare_group_hides_configuration_without_reading_it(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            ctx = FakeContext(private=False)
            ctx.command = _registered_command_tree(
                modules.githubtickets.GitHubTickets,
                modules.githubtickets.GitHubTickets.githubtickets,
            )

            with (
                mock.patch.object(
                    cog,
                    "_send_configuration_overview",
                    new=mock.AsyncMock(),
                ) as configuration_sender,
                mock.patch.object(
                    cog,
                    "_send_github_configuration_overview",
                    new=mock.AsyncMock(),
                ),
                mock.patch.object(
                    modules.githubtickets.discord,
                    "Embed",
                    _OverviewEmbed,
                ),
            ):
                await cog.githubtickets(ctx)

        configuration_sender.assert_not_awaited()
        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("Current configuration", fields)
        self.assertIn("hidden", fields["Current configuration"])
        self.assertIn("Commands", fields)

    async def test_every_bare_subgroup_shows_its_commands(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog_type = modules.githubtickets.GitHubTickets
            cog = cog_type(SimpleNamespace())
            commands = tuple(cog_type.__cog_commands__)
            groups = [
                command
                for command in commands
                if command.qualified_name != "githubtickets"
                and any(
                    getattr(child, "parent", None) is command
                    for child in commands
                )
            ]

            with (
                mock.patch.object(
                    cog,
                    "_send_configuration_overview",
                    new=mock.AsyncMock(),
                ) as configuration_sender,
                mock.patch.object(
                    cog,
                    "_send_github_configuration_overview",
                    new=mock.AsyncMock(),
                ) as github_configuration_sender,
                mock.patch.object(
                    modules.githubtickets.discord,
                    "Embed",
                    _OverviewEmbed,
                ),
            ):
                for group in groups:
                    with self.subTest(group=group.qualified_name):
                        ctx = FakeContext()
                        ctx.command = _registered_command_tree(cog_type, group)
                        await group.callback(cog, ctx)

                        self.assertEqual(ctx.send.await_count, 1)
                        embed = ctx.send.await_args.kwargs["embed"]
                        rendered = "\n".join(
                            field.value for field in embed.fields
                        )
                        for leaf_name in _leaf_command_names(ctx.command):
                            self.assertIn(f"??{leaf_name}", rendered)
                            signature = COMMAND_SIGNATURES.get(leaf_name)
                            if signature is not None:
                                self.assertIn(
                                    f"??{leaf_name} {signature}",
                                    rendered,
                                )
                        if group.qualified_name == "githubtickets logchannel":
                            self.assertEqual(embed.title, "Log channel")

        github_groups = {
            "githubtickets github",
            "githubtickets github receiver",
            "githubtickets github recovery",
        }
        self.assertEqual(
            configuration_sender.await_count,
            len(groups) - len(github_groups),
        )
        self.assertEqual(github_configuration_sender.await_count, len(github_groups))

    async def test_resource_commands_store_values_and_use_accepted_confirmations(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            await cog.store.initialize()
            ctx = FakeContext()
            class FakeTextChannel:
                id = 100
                mention = "#github-tickets"

            modules.githubtickets.discord.TextChannel = FakeTextChannel
            channel = FakeTextChannel()
            role = SimpleNamespace(id=200, mention="@GT:NH Devs")

            await cog.githubtickets_channel_set(ctx, channel)
            await cog.githubtickets_logchannel_set(ctx, channel)
            await cog.githubtickets_role_add(ctx, role)
            await cog.githubtickets_category_add(ctx, name="Rendring")
            await cog.githubtickets_category_rename(
                ctx,
                "Rendring",
                new_name="Rendering",
            )
            await cog.githubtickets_maxpings(ctx, count=5)
            await cog.githubtickets_timing_donotdisturb(ctx, duration="8h")
            await cog.githubtickets_logchannel_clear(ctx)

            config = await cog.config.guild_from_id(42).all()
            categories = await cog.store.list_categories(42)

        self.assertEqual(config["ticket_channel_id"], 100)
        self.assertIsNone(config["log_channel_id"])
        self.assertEqual(config["participant_role_ids"], [200])
        self.assertEqual(config["max_pings"], 5)
        self.assertEqual(config["dnd_response_seconds"], 8 * 60 * 60)
        self.assertEqual(tuple(category.name for category in categories), ("rendering",))
        messages = [call.args[0] for call in ctx.send.await_args_list]
        self.assertEqual(
            messages,
            [
                "Ticket channel set to #github-tickets",
                "Log channel set to #github-tickets",
                "Participant role added: @GT:NH Devs",
                "Category added: rendring",
                "Category renamed from rendring to rendering",
                "Maximum pings set to 5",
                "Do Not Disturb response time set to 8 hours",
                "Log channel cleared",
            ],
        )

    async def test_channel_set_rejects_a_non_text_guild_channel_with_accepted_copy(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            await cog.store.initialize()
            ctx = FakeContext()

            class FakeTextChannel:
                pass

            modules.githubtickets.discord.TextChannel = FakeTextChannel
            await cog.githubtickets_channel_set(
                ctx,
                SimpleNamespace(id=100, mention="#voice"),
            )

            config = await cog.config.guild_from_id(42).all()

        self.assertIsNone(config["ticket_channel_id"])
        ctx.send.assert_awaited_once_with("Ticket channel must be a text channel")

    async def test_configuration_errors_use_only_the_accepted_copy(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            await cog.store.initialize()
            ctx = FakeContext()
            role = SimpleNamespace(id=200, mention="@GT:NH Devs")
            now = datetime.now(timezone.utc)
            await cog.store.add_category(42, "rendering", now)
            await cog.store.add_category(42, "python", now)

            await cog.githubtickets_role_remove(ctx, role)
            await cog.githubtickets_category_add(ctx, name=" ")
            await cog.githubtickets_category_rename(
                ctx,
                "missing",
                new_name="scala",
            )
            await cog.githubtickets_category_rename(
                ctx,
                "rendering",
                new_name="python",
            )
            await cog.githubtickets_category_rename(
                ctx,
                "rendering",
                new_name=" ",
            )
            await cog.githubtickets_category_rename(
                ctx,
                "rendering",
                new_name="x" * 101,
            )
            await cog.githubtickets_maxpings(ctx, count=-1)
            await cog.githubtickets_timing_direct(ctx, duration="later")

        self.assertEqual(
            [call.args[0] for call in ctx.send.await_args_list],
            [
                "Participant role is not configured",
                "Category name cannot be empty",
                "Category not found",
                "Category already exists",
                "Category name cannot be empty",
                "Category name cannot exceed 100 characters",
                "Maximum pings cannot be negative",
                "Invalid duration",
            ],
        )

    async def test_github_configuration_commands_control_the_runtime(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            ctx = FakeContext()
            restart = mock.AsyncMock(return_value=True)
            cog._restart_github_integration = restart

            await cog.githubtickets_github_enable(ctx)
            restart.assert_not_awaited()

            await cog.githubtickets_github_receiver_set(ctx, " 127.0.0.1 ", 8080)
            restart.assert_not_awaited()

            with mock.patch.object(
                modules.githubtickets,
                "load_github_app_credentials",
                new=mock.AsyncMock(return_value=None),
            ):
                await cog.githubtickets_github_enable(ctx)
            restart.assert_not_awaited()

            with mock.patch.object(
                modules.githubtickets,
                "load_github_app_credentials",
                new=mock.AsyncMock(return_value=object()),
            ):
                await cog.githubtickets_github_enable(ctx)
            restart.assert_awaited_once()

            await cog.githubtickets_github_recovery_interval(ctx, "0s")
            self.assertEqual(restart.await_count, 1)

            await cog.githubtickets_github_recovery_interval(ctx, "30m")
            self.assertEqual(restart.await_count, 2)

            runtime = SimpleNamespace(request_recovery=mock.Mock())
            cog._github_runtime = runtime
            await cog.githubtickets_github_recovery_run(ctx)
            runtime.request_recovery.assert_called_once_with()
            self.assertEqual(restart.await_count, 2)

            await cog.githubtickets_github_disable(ctx)
            self.assertEqual(restart.await_count, 3)

            config = await cog.config.all()

        self.assertEqual(config["guild_id"], 42)
        self.assertFalse(config["enabled"])
        self.assertEqual(config["bind_host"], "127.0.0.1")
        self.assertEqual(config["bind_port"], 8080)
        self.assertEqual(config["recovery_seconds"], 30 * 60)

    async def test_clearing_the_receiver_disables_and_stops_the_integration(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            ctx = FakeContext()
            await cog.config.set_raw("enabled", value=True)
            await cog.config.set_raw("bind_host", value="127.0.0.1")
            await cog.config.set_raw("bind_port", value=8080)
            restart = mock.AsyncMock(return_value=False)
            cog._restart_github_integration = restart

            await cog.githubtickets_github_receiver_clear(ctx)

            config = await cog.config.all()

        restart.assert_awaited_once_with()
        self.assertFalse(config["enabled"])
        self.assertIsNone(config["bind_host"])
        self.assertIsNone(config["bind_port"])

    async def test_invalid_github_credentials_are_not_reported_as_missing(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            ctx = FakeContext()
            await cog.config.set_raw("bind_host", value="127.0.0.1")
            await cog.config.set_raw("bind_port", value=8080)
            error = modules.credentials.InvalidGitHubAppCredentials("invalid")

            with (
                mock.patch.object(
                    modules.githubtickets,
                    "load_github_app_credentials",
                    new=mock.AsyncMock(side_effect=error),
                ),
                mock.patch.object(
                    modules.githubtickets,
                    "report_operational_error",
                    new=mock.AsyncMock(),
                ) as reporter,
            ):
                await cog.githubtickets_github_enable(ctx)
                await cog._send_github_configuration_overview(ctx)

        self.assertEqual(
            [call.args[0] for call in ctx.send.await_args_list],
            ["GitHub credentials are invalid", mock.ANY],
        )
        self.assertIn("Credentials: Invalid", ctx.send.await_args_list[-1].args[0])
        reporter.assert_awaited_once()

    async def test_enabled_integration_reports_missing_runtime_credentials(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            await cog.config.set_raw("guild_id", value=42)
            await cog.config.set_raw("enabled", value=True)
            await cog.config.set_raw("bind_host", value="127.0.0.1")
            await cog.config.set_raw("bind_port", value=8080)

            with (
                mock.patch.object(
                    modules.githubtickets,
                    "load_github_app_credentials",
                    new=mock.AsyncMock(return_value=None),
                ),
                mock.patch.object(
                    modules.githubtickets,
                    "report_operational_error",
                    new=mock.AsyncMock(),
                ) as reporter,
            ):
                started = await cog._restart_github_integration()

        self.assertFalse(started)
        reporter.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
