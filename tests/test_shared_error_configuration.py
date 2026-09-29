import importlib
import inspect
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules
from tests.test_forum_autopin import FakeConfigRoot
from tests.test_settings_commands import _OverviewEmbed


@contextmanager
def shared_reporting():
    with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)):
        names = ("NHCogs.operational_support", "NHCogs.operational_errors", "NHCogs.command_overview")
        previous = {name: sys.modules.pop(name, None) for name in names}
        try:
            module = importlib.import_module("NHCogs.operational_support")
            for command in vars(module.OperationalSupport).values():
                if getattr(command, "kind", None) in {"command", "group"}:
                    command.short_doc = ""
                    command.signature = ""
            with mock.patch.object(module.Config, "get_conf", side_effect=lambda *a, **kw: FakeConfigRoot()), mock.patch.object(module.discord, "Embed", _OverviewEmbed):
                yield module
        finally:
            for name in names:
                sys.modules.pop(name, None)
                if previous[name] is not None:
                    sys.modules[name] = previous[name]


def restore_command_help(module):
    for command in vars(module.OperationalSupport).values():
        if getattr(command, "kind", None) not in {"command", "group"}:
            continue
        doc = (command.callback.__doc__ or "").strip()
        command.short_doc = doc.splitlines()[0] if doc else ""
        pieces = []
        for param in list(inspect.signature(command.callback).parameters.values())[2:]:
            if param.default is inspect.Parameter.empty:
                pieces.append(f"<{param.name}>")
            else:
                pieces.append(f"[{param.name}]")
        command.signature = " ".join(pieces)


def rendered_messages(ctx):
    parts = []
    for call in ctx.send.await_args_list:
        embed = call.kwargs.get("embed")
        if embed is None:
            continue
        if embed.description:
            parts.append(embed.description)
        parts.extend(field.value for field in embed.fields)
    return "\n".join(parts)


def context(module, *, public=False):
    default_role = object()
    member = SimpleNamespace(id=30, display_name="Maintainer", mention="<@30>")
    guild = SimpleNamespace(id=10, default_role=default_role, me=object())
    channel = SimpleNamespace(
        id=20, name="maintainer-errors", mention="<#20>", guild=guild,
        permissions_for=lambda role: SimpleNamespace(
            view_channel=public if role is default_role else True,
            send_messages=True, attach_files=True,
        ),
        send=mock.AsyncMock(),
    )
    guild.get_channel = lambda channel_id: channel if channel_id == channel.id else None
    guild.get_member = lambda user_id: member if user_id == member.id else None
    bot = SimpleNamespace(get_guild=lambda _id: guild, get_channel=guild.get_channel)
    ctx = SimpleNamespace(
        guild=guild, bot=bot, channel=channel, send=mock.AsyncMock(), clean_prefix="!",
        author=SimpleNamespace(guild_permissions=SimpleNamespace(manage_messages=True)),
        command=module.OperationalSupport.errors,
    )
    return ctx, member


class SharedErrorConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def test_shared_command_tree_and_manage_messages_permission(self):
        with shared_reporting() as module:
            names = {
                value.qualified_name for value in vars(module.OperationalSupport).values()
                if getattr(value, "kind", None) in {"command", "group"}
            }
        self.assertEqual(names, {
            "nhcogs", "nhcogs errors", "nhcogs errors channel",
            "nhcogs errors channel set",
            "nhcogs errors maintainer", "nhcogs errors maintainer set",
        })
        self.assertTrue(module.OperationalSupport.error_channel_set.hidden)
        self.assertTrue(module.OperationalSupport.error_maintainer_set.hidden)
        self.assertEqual(module.OperationalSupport.error_channel.usage, "[channel|clear]")
        self.assertEqual(
            module.OperationalSupport.error_maintainer.usage, "[member|clear]"
        )

    async def test_set_commands_store_values_and_bare_groups_show_them(self):
        with shared_reporting() as module:
            ctx, member = context(module)
            support = module.OperationalSupport(ctx.bot)
            await module.OperationalSupport.error_channel_set.callback(support, ctx, ctx.channel)
            await module.OperationalSupport.error_maintainer_set.callback(support, ctx, member)
            self.assertEqual(await support.config.guild(ctx.guild).error_channel(), 20)
            self.assertEqual(await support.config.guild(ctx.guild).error_maintainer_id(), 30)
            ctx.send.reset_mock()
            ctx.command = module.OperationalSupport.error_channel
            await module.OperationalSupport.error_channel.callback(support, ctx)
            channel_embed = ctx.send.await_args_list[0].kwargs["embed"]
            self.assertEqual(channel_embed.fields[0].value, "#maintainer-errors")
            ctx.send.reset_mock()
            ctx.command = module.OperationalSupport.error_maintainer
            await module.OperationalSupport.error_maintainer.callback(support, ctx)
            maintainer_embed = ctx.send.await_args_list[0].kwargs["embed"]
            self.assertEqual(maintainer_embed.fields[0].value, "Maintainer")

    async def test_errors_overview_lists_nullable_leaves_without_set(self):
        with shared_reporting() as module:
            restore_command_help(module)
            ctx, member = context(module)
            support = module.OperationalSupport(ctx.bot)
            await module.OperationalSupport.errors.callback(support, ctx)
            rendered = rendered_messages(ctx)
            self.assertIn(
                "`!nhcogs errors channel [channel|clear]` - Show, set, or clear the shared private error channel",
                rendered,
            )
            self.assertIn(
                "`!nhcogs errors maintainer [member|clear]` - Show, set, or clear the error maintainer",
                rendered,
            )
            self.assertNotIn("channel set", rendered)
            self.assertNotIn("maintainer set", rendered)

            ctx.send.reset_mock()
            ctx.command = module.OperationalSupport.error_channel
            await module.OperationalSupport.error_channel.callback(support, ctx, "clear")
            self.assertIsNone(await support.config.guild(ctx.guild).error_channel())
            self.assertEqual(ctx.send.await_args.args[0], "Error channel cleared")

            with self.assertRaisesRegex(
                module.commands.UserFeedbackCheckFailure,
                "Provide a member or use clear",
            ):
                await module.OperationalSupport.error_maintainer.callback(
                    support, ctx, "nope"
                )

            await module.OperationalSupport.error_maintainer.callback(support, ctx, "clear")
            self.assertIsNone(await support.config.guild(ctx.guild).error_maintainer_id())
            self.assertEqual(member.display_name, "Maintainer")

    async def test_public_overview_does_not_read_settings_and_cannot_change_them(self):
        with shared_reporting() as module:
            ctx, member = context(module, public=True)
            support = module.OperationalSupport(ctx.bot)
            support.config = SimpleNamespace(guild=mock.Mock(side_effect=AssertionError("private read")))
            await module.OperationalSupport.errors.callback(support, ctx)
            support.config.guild.assert_not_called()
            with self.assertRaises(module.commands.UserFeedbackCheckFailure):
                await module.OperationalSupport.error_maintainer_set.callback(support, ctx, member)
            await module.OperationalSupport.error_channel.callback(support, ctx)
            support.config.guild.assert_not_called()
            self.assertEqual(
                ctx.send.await_args.args[0],
                "Run this command in a private moderator channel",
            )

    async def test_private_commands_require_manage_messages(self):
        with shared_reporting() as module:
            ctx, _member = context(module)
            ctx.is_red_mod = False
            ctx.is_red_admin = False
            for allowed in (False, True):
                ctx.author.guild_permissions.manage_messages = allowed
                for command in (
                    module.OperationalSupport.error_channel,
                    module.OperationalSupport.error_channel_set,
                    module.OperationalSupport.error_maintainer,
                    module.OperationalSupport.error_maintainer_set,
                ):
                    self.assertEqual(await command.can_run(ctx), allowed)

    async def test_technical_alerts_share_private_destination_and_only_ping_maintainer(self):
        with shared_reporting() as module:
            ctx, member = context(module)
            support = module.OperationalSupport(ctx.bot)
            await module.OperationalSupport.error_channel_set.callback(support, ctx, ctx.channel)
            await module.OperationalSupport.error_maintainer_set.callback(support, ctx, member)
            await support.send_technical_alert(ctx.guild.id, "Honeypot operation failed")
            self.assertEqual(ctx.channel.send.await_count, 1)
            sent = ctx.channel.send.await_args
            self.assertIn("Honeypot operation failed", sent.args[0])
            self.assertEqual(sent.kwargs["allowed_mentions"].users, [member])
            self.assertFalse(sent.kwargs["allowed_mentions"].everyone)
            self.assertFalse(sent.kwargs["allowed_mentions"].roles)
            ctx.channel.permissions_for = lambda _role: SimpleNamespace(view_channel=True)
            await support.send_technical_alert(ctx.guild.id, "Must remain private")
            self.assertEqual(ctx.channel.send.await_count, 1)
