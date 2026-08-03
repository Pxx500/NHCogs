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
        cog._role_analytics_store = mock.AsyncMock()
        cog._role_analytics = mock.Mock()
        cog._gate_migration_store = mock.AsyncMock()
        cog._gate_migration = mock.AsyncMock()
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

    def test_gate_migration_commands_are_hidden_guild_only_manage_messages(self):
        command_names = (
            "checklegacystargateusers",
            "gatemigration",
            "gatemigration_apply",
            "gatemigration_status",
            "gatemigration_resume",
            "gatemigration_verify",
            "gatemigration_restore",
            "gatemigration_export",
            "gatemigration_finalize",
        )
        for command_name in command_names:
            command = getattr(nhmisc.NHMisc, command_name)
            self.assertTrue(command.attrs["hidden"])
            self.assertEqual(
                command.callback.required_permissions,
                {"manage_messages": True},
            )
            self.assertTrue(command.callback.guild_only)

    async def test_gate_migration_root_does_not_expose_subcommands_through_help(self):
        guild = FakeGuild()
        ctx = make_context(guild)
        ctx.send_help = mock.AsyncMock()
        cog = self.make_cog()

        await nhmisc.NHMisc.gatemigration.callback(cog, ctx)

        ctx.send_help.assert_not_awaited()
        ctx.send.assert_awaited_once_with(
            "Gate migration operation required",
            allowed_mentions=ALLOWED_MENTIONS_NONE,
        )

    async def test_gate_migration_apply_rejects_early_confirm_then_accepts_new_one(self):
        guild = FakeGuild()
        for tier, role_id in enumerate(nhmisc.TARGET_TIER_ROLE_IDS, start=1):
            role = FakeRole(role_id)
            role.name = f"Legacy role {tier}"
            guild.roles[role_id] = role
        ctx = make_context(guild)
        cog = self.make_cog()
        prepared = types.SimpleNamespace(run=types.SimpleNamespace(run_id="generated"))
        cog._gate_migration.prepare_run.return_value = prepared
        cog._gate_migration.publish_preparation.return_value = object()
        cog._gate_migration.apply_run.return_value = types.SimpleNamespace(
            completed=4,
            departed=1,
            skipped_unmodifiable=2,
            tier_role_counts=(200, 20, 3, 0, 0, 0, 0, 0, 0, 1),
        )
        early = types.SimpleNamespace(
            author=ctx.author,
            channel=ctx.channel,
            guild=ctx.guild,
            content="confirm",
        )
        valid = types.SimpleNamespace(
            author=ctx.author,
            channel=ctx.channel,
            guild=ctx.guild,
            content="  CONFIRM  ",
        )
        cog.bot.wait_for.side_effect = [early, valid]

        with mock.patch.object(
            nhmisc.time, "monotonic", side_effect=[0, 1, 2, 10, 11]
        ):
            await nhmisc.NHMisc.gatemigration_apply.callback(cog, ctx)

        sent_text = [
            call.args[0]
            for call in ctx.send.await_args_list
            if call.args and isinstance(call.args[0], str)
        ]
        self.assertIn("READ BOUBOU READ (8 seconds left)", sent_text)
        cog._gate_migration_store.transition_run.assert_awaited_once_with(
            mock.ANY, nhmisc.RunState.CONFIRMED
        )
        cog._gate_migration.apply_run.assert_awaited_once()
        completion = ctx.send.await_args_list[-1]
        embed = completion.kwargs["embed"]
        self.assertEqual(embed.title, "Stargate migration complete")
        self.assertEqual(embed.description, "4 completed, 1 departed, 2 skipped")
        self.assertEqual(embed.fields[0].name, "Stargate role membership")
        self.assertIn("Tier 1 — Legacy role 1: 200 players", embed.fields[0].value)
        self.assertIn("Tier 2 — Legacy role 2: 20 players", embed.fields[0].value)
        self.assertIn("Tier 10 — Legacy role 10: 1 player", embed.fields[0].value)
        self.assertEqual(completion.kwargs["allowed_mentions"], ALLOWED_MENTIONS_NONE)

    async def test_gate_migration_resume_sends_completion_membership_embed(self):
        guild = FakeGuild()
        ctx = make_context(guild)
        cog = self.make_cog()
        cog._gate_migration_store.get_active_run.return_value = types.SimpleNamespace(
            run_id="existing-run",
            state=nhmisc.RunState.APPLY_FAILED,
        )
        cog._gate_migration.apply_run.return_value = types.SimpleNamespace(
            completed=8,
            departed=0,
            skipped_unmodifiable=0,
            tier_role_counts=(7, 1, 0, 0, 0, 0, 0, 0, 0, 0),
        )

        await nhmisc.NHMisc.gatemigration_resume.callback(cog, ctx)

        cog._gate_migration.apply_run.assert_awaited_once()
        completion = ctx.send.await_args_list[-1]
        embed = completion.kwargs["embed"]
        self.assertEqual(embed.title, "Stargate migration complete")
        self.assertIn("Tier 1: 7 players", embed.fields[0].value)
        self.assertIn("Tier 2: 1 player", embed.fields[0].value)

    async def test_any_guild_member_can_liberum_veto_without_confirm_authority(self):
        guild = FakeGuild()
        ctx = make_context(guild)
        cog = self.make_cog()
        other_author = types.SimpleNamespace(id=ctx.author.id + 1)
        foreign_confirm = types.SimpleNamespace(
            author=other_author,
            channel=ctx.channel,
            guild=ctx.guild,
            content="confirm",
        )
        veto = types.SimpleNamespace(
            author=other_author,
            channel=ctx.channel,
            guild=ctx.guild,
            content="  LiBeRuM VeTo  ",
        )
        cog.bot.wait_for.side_effect = [foreign_confirm, veto]

        with mock.patch.object(nhmisc.time, "monotonic", side_effect=[0, 1, 2]):
            confirmed = await cog._await_gate_migration_confirmation(
                ctx, "run-veto"
            )

        self.assertFalse(confirmed)
        cog._gate_migration_store.transition_run.assert_awaited_once_with(
            "run-veto", nhmisc.RunState.CANCELLED
        )
        ctx.send.assert_awaited_once_with(
            "Liberum veto! The Gate migration has been cancelled",
            allowed_mentions=nhmisc.discord.AllowedMentions.none(),
        )

    async def test_gate_migration_ignores_unrecognized_messages_until_confirm(self):
        guild = FakeGuild()
        ctx = make_context(guild)
        cog = self.make_cog()
        ignored = types.SimpleNamespace(
            author=ctx.author,
            channel=ctx.channel,
            guild=ctx.guild,
            content="not the confirmation",
        )
        valid = types.SimpleNamespace(
            author=ctx.author,
            channel=ctx.channel,
            guild=ctx.guild,
            content="confirm",
        )
        cog.bot.wait_for.side_effect = [ignored, valid]

        with mock.patch.object(
            nhmisc.time, "monotonic", side_effect=[0, 1, 11, 11]
        ):
            confirmed = await cog._await_gate_migration_confirmation(ctx, "run-confirm")

        self.assertTrue(confirmed)
        cog._gate_migration_store.transition_run.assert_awaited_once_with(
            "run-confirm", nhmisc.RunState.CONFIRMED
        )
        ctx.send.assert_not_awaited()

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

        cog._role_analytics_store.delete_user_everywhere.assert_awaited_once_with(42)


if __name__ == "__main__":
    unittest.main()
