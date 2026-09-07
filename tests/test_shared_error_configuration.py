import importlib
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
            "nhcogs errors channel clear", "nhcogs errors maintainer",
            "nhcogs errors maintainer clear",
        })

    async def test_optional_arguments_set_values_and_bare_commands_show_them(self):
        with shared_reporting() as module:
            ctx, member = context(module)
            support = module.OperationalSupport(ctx.bot)
            await module.OperationalSupport.error_channel.callback(support, ctx, ctx.channel)
            await module.OperationalSupport.error_maintainer.callback(support, ctx, member)
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

    async def test_public_overview_does_not_read_settings_and_cannot_change_them(self):
        with shared_reporting() as module:
            ctx, member = context(module, public=True)
            support = module.OperationalSupport(ctx.bot)
            support.config = SimpleNamespace(guild=mock.Mock(side_effect=AssertionError("private read")))
            await module.OperationalSupport.errors.callback(support, ctx)
            support.config.guild.assert_not_called()
            with self.assertRaises(module.commands.UserFeedbackCheckFailure):
                await module.OperationalSupport.error_maintainer.callback(support, ctx, member)
            support.config.guild.assert_not_called()

    async def test_private_commands_require_manage_messages(self):
        with shared_reporting() as module:
            ctx, _member = context(module)
            ctx.is_red_mod = False
            ctx.is_red_admin = False
            for allowed in (False, True):
                ctx.author.guild_permissions.manage_messages = allowed
                for command in (
                    module.OperationalSupport.error_channel,
                    module.OperationalSupport.error_channel_clear,
                    module.OperationalSupport.error_maintainer,
                    module.OperationalSupport.error_maintainer_clear,
                ):
                    self.assertEqual(await command.can_run(ctx), allowed)

    async def test_technical_alerts_share_private_destination_and_only_ping_maintainer(self):
        with shared_reporting() as module:
            ctx, member = context(module)
            support = module.OperationalSupport(ctx.bot)
            await module.OperationalSupport.error_channel.callback(support, ctx, ctx.channel)
            await module.OperationalSupport.error_maintainer.callback(support, ctx, member)
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
