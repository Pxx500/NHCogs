import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.githubtickets_loader import isolated_githubtickets_modules


class FakeContext:
    def __init__(self, guild_id=42):
        self.guild = SimpleNamespace(id=guild_id)
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
                "githubtickets role",
                "githubtickets role add",
                "githubtickets role remove",
                "githubtickets category",
                "githubtickets category add",
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
            await cog.store.add_category(
                42,
                "Rendering",
                datetime.now(timezone.utc),
            )
            ctx = FakeContext()

            await cog.githubtickets(ctx)

        content = ctx.send.await_args.args[0]
        self.assertIn("Ticket channel: <#100>", content)
        self.assertIn("Participant roles: <@&200>, <@&201>", content)
        self.assertIn("Categories: rendering", content)
        self.assertIn("Maximum pings: 3", content)
        allowed_mentions = ctx.send.await_args.kwargs["allowed_mentions"]
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)

    async def test_resource_commands_store_values_and_use_accepted_confirmations(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            await cog.store.initialize()
            ctx = FakeContext()
            channel = SimpleNamespace(id=100, mention="#github-tickets")
            role = SimpleNamespace(id=200, mention="@GT:NH Devs")

            await cog.githubtickets_channel_set(ctx, channel)
            await cog.githubtickets_role_add(ctx, role)
            await cog.githubtickets_category_add(ctx, name="Rendering")
            await cog.githubtickets_maxpings(ctx, count=5)
            await cog.githubtickets_timing_donotdisturb(ctx, duration="8h")

            config = await cog.config.guild_from_id(42).all()
            categories = await cog.store.list_categories(42)

        self.assertEqual(config["ticket_channel_id"], 100)
        self.assertEqual(config["participant_role_ids"], [200])
        self.assertEqual(config["max_pings"], 5)
        self.assertEqual(config["dnd_response_seconds"], 8 * 60 * 60)
        self.assertEqual(tuple(category.name for category in categories), ("rendering",))
        messages = [call.args[0] for call in ctx.send.await_args_list]
        self.assertEqual(
            messages,
            [
                "Ticket channel set to #github-tickets",
                "Participant role added: @GT:NH Devs",
                "Category added: rendering",
                "Maximum pings set to 5",
                "Do Not Disturb response time set to 8 hours",
            ],
        )

    async def test_configuration_errors_use_only_the_accepted_copy(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(SimpleNamespace())
            await cog.store.initialize()
            ctx = FakeContext()
            role = SimpleNamespace(id=200, mention="@GT:NH Devs")

            await cog.githubtickets_role_remove(ctx, role)
            await cog.githubtickets_category_add(ctx, name=" ")
            await cog.githubtickets_maxpings(ctx, count=-1)
            await cog.githubtickets_timing_direct(ctx, duration="later")

        self.assertEqual(
            [call.args[0] for call in ctx.send.await_args_list],
            [
                "Participant role is not configured",
                "Category name cannot be empty",
                "Maximum pings cannot be negative",
                "Invalid duration",
            ],
        )


if __name__ == "__main__":
    unittest.main()
