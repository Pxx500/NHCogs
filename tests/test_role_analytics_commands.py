import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_NAME = "nhmisc_role_analytics_commands_test_package"
PACKAGE_PATH = Path(__file__).parents[1] / "NHMisc"


class UserFeedbackCheckFailure(Exception):
    pass


class FakeCommand:
    def __init__(self, callback, **attrs):
        self.callback = callback
        self.attrs = attrs

    def command(self, **attrs):
        return lambda callback: FakeCommand(callback, **attrs)

    def group(self, **attrs):
        return lambda callback: FakeCommand(callback, **attrs)


def _tag(name, value=True):
    def decorator(target):
        callback = target.callback if isinstance(target, FakeCommand) else target
        setattr(callback, name, value)
        return target

    return decorator


def _command(**attrs):
    return lambda callback: FakeCommand(callback, **attrs)


class FakeCog:
    @staticmethod
    def listener(event_name=None):
        return _tag("listener_event", event_name)


class FakeFile:
    def __init__(self, fp, *, filename):
        self.filename = filename
        self.data = fp.read()


class FakeEmbed:
    def __init__(self, *, title=None, description=None, color=None):
        self.title = title
        self.description = description
        self.color = color
        self.fields = []

    def add_field(self, *, name, value, inline=True):
        self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))


ALLOWED_MENTIONS_NONE = object()


def load_nhmisc_module():
    discord = types.ModuleType("discord")
    discord.Forbidden = type("Forbidden", (Exception,), {})
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.File = FakeFile
    discord.AllowedMentions = types.SimpleNamespace(
        none=lambda: ALLOWED_MENTIONS_NONE
    )
    discord.MessageType = types.SimpleNamespace(default=0, reply=1)
    discord.Color = types.SimpleNamespace(
        blue=lambda: 0, green=lambda: 0, orange=lambda: 0, red=lambda: 0
    )
    discord.Embed = FakeEmbed

    commands = types.ModuleType("redbot.core.commands")
    commands.Cog = FakeCog
    commands.Context = object
    commands.UserFeedbackCheckFailure = UserFeedbackCheckFailure
    commands.BucketType = types.SimpleNamespace(user="user", guild="guild")
    commands.command = _command
    commands.group = _command
    commands.guild_only = lambda: _tag("guild_only")
    commands.admin_or_permissions = lambda **permissions: _tag(
        "admin_or_permissions", permissions
    )
    commands.mod_or_permissions = lambda **permissions: _tag(
        "mod_or_permissions", permissions
    )
    commands.has_permissions = lambda **permissions: _tag(
        "required_permissions", permissions
    )
    commands.cooldown = lambda rate, per, bucket: _tag(
        "cooldown", (rate, per, bucket)
    )

    class FakeConfig:
        @staticmethod
        def get_conf(*args, **kwargs):
            raise AssertionError("Config should not be constructed in command unit tests")

    redbot = types.ModuleType("redbot")
    core = types.ModuleType("redbot.core")
    core.Config = FakeConfig
    core.commands = commands
    data_manager = types.ModuleType("redbot.core.data_manager")
    data_manager.cog_data_path = lambda cog: Path(".")

    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_PATH)]
    module_names = (
        "discord",
        "redbot",
        "redbot.core",
        "redbot.core.commands",
        "redbot.core.data_manager",
        PACKAGE_NAME,
        f"{PACKAGE_NAME}.nhmisc",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    sys.modules.update(
        {
            "discord": discord,
            "redbot": redbot,
            "redbot.core": core,
            "redbot.core.commands": commands,
            "redbot.core.data_manager": data_manager,
            PACKAGE_NAME: package,
        }
    )
    try:
        qualified_name = f"{PACKAGE_NAME}.nhmisc"
        spec = importlib.util.spec_from_file_location(
            qualified_name, PACKAGE_PATH / "nhmisc.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


nhmisc = load_nhmisc_module()


class FakeRole:
    def __init__(self, role_id, *, default=False):
        self.id = role_id
        self.mention = f"<@&{role_id}>"
        self._default = default

    def is_default(self):
        return self._default


class FakeMember:
    def __init__(self, user_id, role_ids=(), *, name=None, display_name=None):
        self.id = user_id
        self.bot = False
        self.roles = [FakeRole(role_id) for role_id in role_ids]
        self.name = name or f"user{user_id}"
        self.display_name = display_name or f"User {user_id}"


class FakeChannel:
    def __init__(self, guild, *, public=False, bot_permissions=None):
        self.id = 321
        self.guild = guild
        self.public = public
        self.bot_permissions = bot_permissions or types.SimpleNamespace(
            view_channel=True,
            send_messages=True,
            attach_files=True,
        )

    def permissions_for(self, target):
        if target is self.guild.default_role:
            return types.SimpleNamespace(view_channel=self.public)
        if target is self.guild.me:
            return self.bot_permissions
        raise AssertionError("Unexpected permissions target")


class FakeGuild:
    def __init__(self, *, public=False, bot_permissions=None):
        self.id = 123
        self.default_role = FakeRole(123, default=True)
        self.roles = {
            self.default_role.id: self.default_role,
            10: FakeRole(10),
            20: FakeRole(20),
        }
        self.me = object()
        self.filesize_limit = 10_000_000
        self.chunked = True
        self.members = []
        self.channel = FakeChannel(
            self, public=public, bot_permissions=bot_permissions
        )

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def get_member(self, user_id):
        return next((member for member in self.members if member.id == user_id), None)


def make_context(guild):
    return types.SimpleNamespace(
        guild=guild,
        channel=guild.channel,
        author=types.SimpleNamespace(id=999),
        send=mock.AsyncMock(),
    )


class RoleAnalyticsCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = types.SimpleNamespace(guilds=[], wait_for=mock.AsyncMock())
        cog._activity_store = mock.AsyncMock()
        cog._sticky_roles = mock.AsyncMock()
        cog._role_analytics_store = mock.AsyncMock()
        cog._role_analytics = mock.Mock()
        cog._achievement_store = mock.AsyncMock()
        cog._achievement_store.is_bootstrapped.return_value = True
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()
        cog._gate_increment_store = mock.AsyncMock()
        return cog

    def test_commands_require_manage_messages_and_expected_cooldowns(self):
        for command_name in ("rolesync", "rolestats", "roleusers"):
            callback = getattr(nhmisc.NHMisc, command_name).callback
            self.assertEqual(callback.required_permissions, {"manage_messages": True})
            self.assertTrue(callback.guild_only)

        self.assertEqual(
            nhmisc.NHMisc.rolestats.callback.cooldown, (1, 5, "user")
        )
        self.assertEqual(
            nhmisc.NHMisc.roleusers.callback.cooldown, (1, 10, "guild")
        )

    async def test_rolestats_allows_public_channel_and_never_pings_roles(self):
        cog = self.make_cog()
        cog._role_analytics_store.count_matching.return_value = 7
        guild = FakeGuild(public=True)
        ctx = make_context(guild)

        await nhmisc.NHMisc.rolestats.callback(
            cog, ctx, expression="10 and <@&20>"
        )

        ctx.send.assert_awaited_once_with(
            "7 users match: <@&10> AND <@&20>",
            allowed_mentions=ALLOWED_MENTIONS_NONE,
        )

    async def test_rolesync_refuses_concurrent_sync_before_acknowledgement(self):
        cog = self.make_cog()
        cog._role_analytics.is_syncing.return_value = True
        ctx = make_context(FakeGuild(public=True))

        with self.assertRaises(UserFeedbackCheckFailure):
            await nhmisc.NHMisc.rolesync.callback(cog, ctx)

        ctx.send.assert_not_awaited()

    async def test_rolesync_reports_snapshot_counts_and_elapsed_time(self):
        cog = self.make_cog()
        cog._role_analytics.is_syncing.return_value = False
        cog._role_analytics.sync_guild = mock.AsyncMock(
            return_value=types.SimpleNamespace(
                member_count=80_000,
                membership_count=240_000,
                elapsed_seconds=12.34,
            )
        )
        ctx = make_context(FakeGuild(public=True))

        await nhmisc.NHMisc.rolesync.callback(cog, ctx)

        self.assertEqual(
            [call.args[0] for call in ctx.send.await_args_list],
            [
                "Role synchronization started",
                "Role synchronization complete: 80000 members, "
                "240000 role memberships in 12.3s",
            ],
        )

    async def test_roleusers_refuses_public_channel_before_querying(self):
        cog = self.make_cog()
        ctx = make_context(FakeGuild(public=True))

        with self.assertRaises(UserFeedbackCheckFailure):
            await nhmisc.NHMisc.roleusers.callback(cog, ctx, expression="10")

        cog._role_analytics_store.matching_user_ids.assert_not_awaited()

    async def test_roleusers_refuses_missing_bot_permission_before_querying(self):
        permissions = types.SimpleNamespace(
            view_channel=True,
            send_messages=True,
            attach_files=False,
        )
        cog = self.make_cog()
        ctx = make_context(FakeGuild(bot_permissions=permissions))

        with self.assertRaises(UserFeedbackCheckFailure):
            await nhmisc.NHMisc.roleusers.callback(cog, ctx, expression="10")

        cog._role_analytics_store.matching_user_ids.assert_not_awaited()

    async def test_roleusers_sends_csv_with_resolved_current_names(self):
        cog = self.make_cog()
        cog._role_analytics_store.matching_user_ids.return_value = (1, 2)
        guild = FakeGuild()
        guild.members = [
            FakeMember(1, name="first", display_name="First"),
            FakeMember(2, name="second", display_name="Second"),
        ]
        ctx = make_context(guild)

        await nhmisc.NHMisc.roleusers.callback(cog, ctx, expression="10 OR 20")

        kwargs = ctx.send.await_args.kwargs
        self.assertEqual(ctx.send.await_args.args[0], "2 users match: <@&10> OR <@&20>")
        self.assertIs(kwargs["allowed_mentions"], ALLOWED_MENTIONS_NONE)
        self.assertEqual(kwargs["file"].filename, "roleusers.csv")
        self.assertEqual(
            kwargs["file"].data.decode("utf-8"),
            "user_id,username,display_name\n1,first,First\n2,second,Second\n",
        )

    async def test_roleusers_zero_result_has_exact_short_message(self):
        cog = self.make_cog()
        cog._role_analytics_store.matching_user_ids.return_value = ()
        ctx = make_context(FakeGuild())

        await nhmisc.NHMisc.roleusers.callback(cog, ctx, expression="10")

        ctx.send.assert_awaited_once_with("No users match this expression")

    async def test_roleusers_missing_cached_member_refuses_incomplete_export_and_repairs(self):
        cog = self.make_cog()
        cog._role_analytics_store.matching_user_ids.return_value = (1, 2)
        guild = FakeGuild()
        guild.members = [FakeMember(1)]
        ctx = make_context(guild)

        with self.assertRaises(UserFeedbackCheckFailure):
            await nhmisc.NHMisc.roleusers.callback(cog, ctx, expression="10")

        cog._role_analytics_store.set_status.assert_awaited_once_with(
            guild.id, nhmisc.SyncStatus.NEEDS_RECONCILIATION, "member_cache_mismatch"
        )
        cog._role_analytics.schedule_guild_retry.assert_called_once_with(guild, 0)
        ctx.send.assert_not_awaited()

    async def test_unknown_and_everyone_roles_are_rejected_before_query(self):
        cog = self.make_cog()
        ctx = make_context(FakeGuild(public=True))

        for expression in ("999", "123"):
            with self.subTest(expression=expression):
                with self.assertRaises(UserFeedbackCheckFailure):
                    await nhmisc.NHMisc.rolestats.callback(
                        cog, ctx, expression=expression
                    )

        cog._role_analytics_store.count_matching.assert_not_awaited()

    async def test_analytics_listeners_use_unique_names_and_ignore_profile_updates(self):
        expected_events = {
            "on_role_analytics_member_join": "on_member_join",
            "on_role_analytics_member_update": "on_member_update",
            "on_role_analytics_member_remove": "on_member_remove",
            "on_role_analytics_role_delete": "on_guild_role_delete",
            "on_role_analytics_resumed": "on_resumed",
        }
        for method_name, event_name in expected_events.items():
            self.assertEqual(
                getattr(nhmisc.NHMisc, method_name).listener_event,
                event_name,
            )

        cog = self.make_cog()
        cog._role_analytics.member_roles_changed = mock.AsyncMock()
        guild = FakeGuild()
        before = FakeMember(1, (10,))
        before.guild = guild
        after = FakeMember(1, (10,))
        after.guild = guild

        await cog.on_role_analytics_member_update(before, after)
        cog._role_analytics.member_roles_changed.assert_not_called()

        after.roles.append(FakeRole(20))
        await cog.on_role_analytics_member_update(before, after)
        cog._role_analytics.member_roles_changed.assert_called_once_with(
            guild.id, after, guild.default_role.id
        )

    async def test_data_deletion_removes_user_from_all_guilds(self):
        cog = self.make_cog()

        await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=42)

        cog._activity_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._sticky_roles.delete_user_everywhere.assert_awaited_once_with(42)
        cog._role_analytics_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._achievement_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._gate_increment_store.redact_user_data.assert_awaited_once_with(42)


if __name__ == "__main__":
    unittest.main()
