import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules
from tests.test_ban_research import BOT, LogChannel, ban_embed, message
from tests.test_settings_commands import _OverviewEmbed, _registered_command_tree


def context(*, public=False, source_permissions=True, attach_files=True):
    guild = SimpleNamespace(
        id=10, me=object(), default_role=object(), filesize_limit=100_000, roles=[]
    )
    ctx = SimpleNamespace(
        guild=guild,
        clean_prefix="??",
        channel=SimpleNamespace(
            permissions_for=lambda role: SimpleNamespace(
                view_channel=public if role is guild.default_role else True,
                attach_files=attach_files,
            )
        ),
        send=mock.AsyncMock(),
    )
    moderation = LogChannel(20, [message(1, 5, ban_embed())])
    members = LogChannel(30, [])
    for channel in (moderation, members):
        channel.guild = guild
        channel.permissions_for = lambda role: SimpleNamespace(
            view_channel=source_permissions,
            read_message_history=source_permissions,
        )
    return ctx, moderation, members


class BanResearchCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_overview_uses_runtime_prefix_and_full_leaf_syntax(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            cog = object.__new__(honeypot.Honeypot)
            ctx, _, _ = context(public=True)
            ctx.command = _registered_command_tree(honeypot, honeypot.Honeypot.research)
            with mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed):
                await honeypot.Honeypot.research.callback(cog, ctx)
            rendered = "\n".join(
                field.value
                for call in ctx.send.await_args_list
                for field in call.kwargs["embed"].fields
            )
            self.assertIn(
                "??honeypot research bans <moderation_channel> <member_channel>", rendered
            )
            self.assertEqual(honeypot.Honeypot.honeypot.has_permissions, {"manage_messages": True})
            self.assertIs(honeypot.Honeypot.research.parent, honeypot.Honeypot.honeypot)
            self.assertIs(honeypot.Honeypot.research_bans.parent, honeypot.Honeypot.research)

    async def test_unsafe_output_or_unavailable_sources_are_rejected_before_history(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            cog = object.__new__(honeypot.Honeypot)
            cases = (
                {"public": True},
                {"source_permissions": False},
                {"attach_files": False},
                {"other_guild": True},
            )
            for options in cases:
                with self.subTest(options=options):
                    other_guild = options.pop("other_guild", False)
                    ctx, moderation, members = context(**options)
                    if other_guild:
                        members.guild = SimpleNamespace(id=99)
                    await honeypot.Honeypot.research_bans.callback(cog, ctx, moderation, members)
                    self.assertEqual(moderation.history_calls, [])
                    self.assertEqual(members.history_calls, [])
                    self.assertEqual(ctx.send.await_count, 1)
                    self.assertNotIn("file", ctx.send.await_args.kwargs)

    async def test_private_command_sends_archive_and_cleans_up_after_success(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            cog = object.__new__(honeypot.Honeypot)
            cog.bot = SimpleNamespace(user=SimpleNamespace(id=BOT))
            cog._ban_research_guilds = set()
            ctx, moderation, members = context()
            files = []

            async def capture(*args, **kwargs):
                if "file" in kwargs:
                    files.append(Path(kwargs["file"].fp))
                    self.assertTrue(files[-1].is_file())

            ctx.send.side_effect = capture
            await honeypot.Honeypot.research_bans.callback(cog, ctx, moderation, members)
            self.assertEqual(len(files), 1)
            self.assertFalse(files[0].exists())
            self.assertEqual(cog._ban_research_guilds, set())
            self.assertIn("1 accounts", ctx.send.await_args.args[0])

    async def test_concurrent_run_is_rejected_and_failed_run_releases_its_slot(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            cog = object.__new__(honeypot.Honeypot)
            cog.bot = SimpleNamespace(user=SimpleNamespace(id=BOT))
            cog._ban_research_guilds = {10}
            ctx, moderation, members = context()
            await honeypot.Honeypot.research_bans.callback(cog, ctx, moderation, members)
            self.assertEqual(moderation.history_calls, [])

            cog._ban_research_guilds.clear()
            moderation.history = mock.Mock(side_effect=honeypot.discord.Forbidden("Denied"))
            with self.assertRaises(honeypot.discord.Forbidden):
                await honeypot.Honeypot.research_bans.callback(cog, ctx, moderation, members)
            self.assertEqual(cog._ban_research_guilds, set())
            self.assertFalse(any("file" in call.kwargs for call in ctx.send.await_args_list))
            self.assertEqual(os.listdir(directory), [])

    async def test_destination_becoming_public_during_scan_prevents_file_delivery(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            cog = object.__new__(honeypot.Honeypot)
            cog.bot = SimpleNamespace(user=SimpleNamespace(id=BOT))
            cog._ban_research_guilds = set()
            ctx, moderation, members = context()
            history = moderation.history

            async def change_permissions(**kwargs):
                async for item in history(**kwargs):
                    ctx.channel.permissions_for = lambda role: SimpleNamespace(
                        view_channel=True, attach_files=True
                    )
                    yield item

            moderation.history = change_permissions
            await honeypot.Honeypot.research_bans.callback(cog, ctx, moderation, members)
            self.assertFalse(any("file" in call.kwargs for call in ctx.send.await_args_list))
            self.assertEqual(cog._ban_research_guilds, set())
