import asyncio
import hashlib
import importlib.util
import inspect
import sqlite3
import sys
import types
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.test_forum_autopin import make_support

ROOT_PACKAGE_NAME = "nhmisc_role_analytics_commands_test_root"
PACKAGE_NAME = f"{ROOT_PACKAGE_NAME}.nhmisc"
ROOT_PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs"
PACKAGE_PATH = ROOT_PACKAGE_PATH / "nhmisc"


class UserFeedbackCheckFailure(Exception):
    pass


class FakeCommand:
    def __init__(self, callback, parent=None, **attrs):
        self.callback = callback
        self.parent = parent
        self.attrs = attrs

    def command(self, **attrs):
        return lambda callback: FakeCommand(callback, parent=self, **attrs)

    def group(self, **attrs):
        return lambda callback: FakeCommand(callback, parent=self, **attrs)

    async def can_run(self, ctx):
        return await _can_run(self, ctx)


async def _can_run(command, ctx):
    while command is not None:
        callback = command.callback
        direct_permissions = _installed_direct_permissions(callback)
        if direct_permissions is not None and not _passes_permissions(
            ctx, direct_permissions
        ):
            return False
        mod_permissions = getattr(callback, "mod_or_permissions", None)
        if mod_permissions is not None and not _passes_permissions(
            ctx,
            mod_permissions,
            "is_red_mod",
            "is_red_admin",
        ):
            return False
        admin_permissions = getattr(callback, "admin_or_permissions", None)
        if admin_permissions is not None and not _passes_permissions(
            ctx,
            admin_permissions,
            "is_red_admin",
        ):
            return False
        command = getattr(command, "parent", None)
    return True


def _installed_direct_permissions(callback):
    for name in ("has_permissions", "direct_permissions", "required_permissions"):
        payload = getattr(callback, name, None)
        if payload is not None:
            return payload
    return None


def _passes_permissions(ctx, permissions, *privilege_flags):
    if any(getattr(ctx, flag, False) for flag in privilege_flags):
        return True
    guild_permissions = ctx.author.guild_permissions
    return all(
        getattr(guild_permissions, name, False) is required
        for name, required in permissions.items()
    )


def _tag_permissions(*names, permissions):
    def decorator(target):
        callback = target.callback if isinstance(target, FakeCommand) else target
        for name in names:
            setattr(callback, name, permissions)
        return target

    return decorator


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


class FakeTextChannel:
    """Marker so the shared log resolver accepts command-test channels."""


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
    discord.TextChannel = FakeTextChannel

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
    commands.has_permissions = lambda **permissions: _tag_permissions(
        "has_permissions",
        "required_permissions",
        permissions=permissions,
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

    root_package = types.ModuleType(ROOT_PACKAGE_NAME)
    root_package.__path__ = [str(ROOT_PACKAGE_PATH)]
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_PATH)]
    module_names = (
        "discord",
        "redbot",
        "redbot.core",
        "redbot.core.commands",
        "redbot.core.data_manager",
        ROOT_PACKAGE_NAME,
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
            ROOT_PACKAGE_NAME: root_package,
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


class FakeChannel(FakeTextChannel):
    def __init__(self, guild, *, public=False, bot_permissions=None):
        self.id = 321
        self.mention = "#alerts"
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
        self.channels = {}

    def get_channel(self, channel_id):
        if self.channel.id == channel_id:
            return self.channel
        return self.channels.get(channel_id)

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


async def bind_error_channel(cog, ctx):
    await cog._support.config.guild(ctx.guild).error_channel.set(ctx.channel.id)


class RoleAnalyticsCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = types.SimpleNamespace(
            guilds=[],
            intents=types.SimpleNamespace(members=True),
            application_flags=types.SimpleNamespace(
                gateway_guild_members=True, gateway_guild_members_limited=False,
            ),
            wait_for=mock.AsyncMock(),
            get_channel=lambda _channel_id: None,
        )
        cog._support = make_support(cog.bot, types.SimpleNamespace(), module=nhmisc)
        cog._activity_store = mock.AsyncMock()
        cog._sticky_roles = mock.AsyncMock()
        cog._role_analytics_store = mock.AsyncMock()
        cog._role_analytics = mock.Mock()
        cog._achievement_store = mock.AsyncMock()
        cog._achievement_store.is_bootstrapped.return_value = True
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()
        cog._upload_achievement_sync_backup = mock.AsyncMock()
        cog._gate_increment_store = mock.AsyncMock()
        cog._achievement_syncing_guilds = set()
        cog.report_operational_error = mock.AsyncMock()
        return cog

    async def test_cog_unload_awaits_owned_tasks(self):
        stopped = asyncio.Event()

        async def owned_writer():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.set()

        writer_task = asyncio.create_task(owned_writer())
        await asyncio.sleep(0)
        cog = object.__new__(nhmisc.NHMisc)
        cog._audit_log_tasks = set()
        cog._activity_task = writer_task
        cog._achievement_reconciliations = {}
        cog._achievement_reconciliation_runs = set()
        cog._role_analytics_startup_task = None
        cog._role_analytics_daily_task = None
        cog._gate_increment_recovery_task = None
        cog._unregister_gate_increment_context_menu = mock.Mock()
        cog._unregister_achievement_commands = mock.Mock()
        cog._role_analytics = types.SimpleNamespace(
            cancel=mock.Mock(),
            shutdown=mock.AsyncMock(),
        )

        result = nhmisc.NHMisc.cog_unload(cog)
        try:
            self.assertTrue(inspect.isawaitable(result))
            await result
            self.assertTrue(stopped.is_set())
            self.assertTrue(writer_task.done())
            cog._role_analytics.shutdown.assert_awaited_once_with()
        finally:
            if not writer_task.done():
                writer_task.cancel()
                await asyncio.gather(writer_task, return_exceptions=True)

    async def test_commands_reject_members_without_manage_messages(self):
        denied = types.SimpleNamespace(
            author=types.SimpleNamespace(
                guild_permissions=types.SimpleNamespace(manage_messages=False)
            )
        )
        allowed = types.SimpleNamespace(
            author=types.SimpleNamespace(
                guild_permissions=types.SimpleNamespace(manage_messages=True)
            )
        )
        for command_name in (
            "rolesync",
            "rolesync_discord",
            "rolestats",
            "roleusers",
            "achievement",
            "achievement_create",
            "achievement_role",
            "achievement_role_bind",
            "achievement_role_unbind",
            "achievement_role_replace",
            "achievement_role_list",
            "achievement_revoke",
        ):
            with self.subTest(command=command_name):
                command = getattr(nhmisc.NHMisc, command_name)
                self.assertFalse(await command.can_run(denied))
                self.assertTrue(await command.can_run(allowed))
                self.assertTrue(command.callback.guild_only)
        child = nhmisc.NHMisc.nhmisc_roleanalytics_disable
        self.assertIs(
            child.parent.callback,
            nhmisc.NHMisc.nhmisc_roleanalytics.callback,
        )
        self.assertFalse(await child.can_run(denied))
        self.assertTrue(await child.can_run(allowed))

        self.assertEqual(nhmisc.NHMisc.rolestats.callback.cooldown, (1, 5, "user"))
        self.assertEqual(nhmisc.NHMisc.roleusers.callback.cooldown, (1, 10, "guild"))

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
        cog._reconcile_achievement_roles_for_guild.assert_awaited_once_with(
            ctx.guild
        )

    async def test_rolesync_operational_failure_reaches_private_reporter(self):
        cog = self.make_cog()
        cog._role_analytics.is_syncing.return_value = False
        failure = RuntimeError("sync failed")
        cog._role_analytics.sync_guild = mock.AsyncMock(side_effect=failure)
        ctx = make_context(FakeGuild(public=True))
        ctx.message = types.SimpleNamespace(id=300)

        with self.assertRaises(UserFeedbackCheckFailure):
            await nhmisc.NHMisc.rolesync.callback(cog, ctx)

        cog.report_operational_error.assert_awaited_once_with(
            guild_id=ctx.guild.id,
            source="NHMisc",
            action="synchronize role analytics",
            error=failure,
            channel_id=ctx.channel.id,
            message_id=300,
        )

    async def test_multi_role_lookup_returns_each_holder_set(self):
        cog = self.make_cog()
        cog._role_analytics_store.matching_user_ids.side_effect = (
            (10,),
            (20, 21),
        )

        users_by_role = await cog._role_analytics_users_with_roles(
            123,
            (100, 200),
        )

        self.assertEqual(users_by_role, ((10,), (20, 21)))
        self.assertEqual(
            [call.args[-1] for call in cog._role_analytics_store.matching_user_ids.await_args_list],
            [(100,), (200,)],
        )

    async def test_discord_snapshot_rejects_an_incomplete_member_cache(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        guild.members = [FakeMember(10)]
        guild.member_count = 2

        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            "member cache is incomplete",
        ):
            await cog._achievement_discord_snapshot(guild)

        cog._role_analytics_store.matching_user_ids.assert_not_awaited()

    async def test_discord_snapshot_rejects_an_incomplete_analytics_generation(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        guild.members = [FakeMember(10)]
        guild.member_count = 1
        cog._role_analytics_store.get_state.return_value = types.SimpleNamespace(
            status=nhmisc.SyncStatus.READY,
            last_completed_at="2026-08-04T12:00:00+00:00",
        )
        cog._achievement_store.is_bootstrapped.return_value = False
        cog._role_analytics_store.count_matching.return_value = 0

        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            "member count does not match Discord",
        ):
            await cog._achievement_discord_snapshot(guild)

    async def test_discord_snapshot_rejects_role_holders_that_differ_from_discord(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        tracked_role_ids = (
            *nhmisc.GATE_TIER_ROLE_IDS,
            nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
        )
        guild.roles.update({role_id: FakeRole(role_id) for role_id in tracked_role_ids})
        guild.members = [FakeMember(10, (nhmisc.GATE_TIER_ROLE_IDS[0],))]
        guild.member_count = 1
        cog._role_analytics_store.get_state.return_value = types.SimpleNamespace(
            status=nhmisc.SyncStatus.READY,
            last_completed_at="2026-08-04T12:00:00+00:00",
        )
        cog._role_analytics_store.count_matching.return_value = 1
        cog._achievement_store.is_bootstrapped.return_value = False
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=tuple(() for _role_id in tracked_role_ids)
        )

        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            "role holders do not match Discord",
        ):
            await cog._achievement_discord_snapshot(guild)

    async def test_discord_snapshot_keeps_validated_raw_role_holders_for_backup(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        tracked_role_ids = (
            *nhmisc.GATE_TIER_ROLE_IDS,
            nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
        )
        guild.roles.update({role_id: FakeRole(role_id) for role_id in tracked_role_ids})
        guild.members = [
            FakeMember(
                10,
                (
                    nhmisc.GATE_TIER_ROLE_IDS[0],
                    nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
                ),
            )
        ]
        guild.member_count = 1
        cog._role_analytics_store.get_state.return_value = types.SimpleNamespace(
            status=nhmisc.SyncStatus.READY,
            last_completed_at="2026-08-04T12:00:00+00:00",
        )
        cog._role_analytics_store.count_matching.return_value = 1
        cog._achievement_store.is_bootstrapped.return_value = False
        users_by_role = tuple(
            (10,)
            if role_id
            in (
                nhmisc.GATE_TIER_ROLE_IDS[0],
                nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            )
            else ()
            for role_id in tracked_role_ids
        )
        cog._role_analytics_users_with_roles = mock.AsyncMock(return_value=users_by_role)

        snapshot = await cog._achievement_discord_snapshot(guild)

        self.assertEqual(snapshot.gate_tiers, {10: 1})
        self.assertEqual(snapshot.boolean_users, {"solo_gater": (10,)})
        self.assertEqual(
            snapshot.role_holders,
            dict(zip(tracked_role_ids, users_by_role, strict=True)),
        )
        self.assertEqual(snapshot.cached_member_count, 1)
        self.assertEqual(snapshot.reported_member_count, 1)

    async def test_sync_backup_uploads_database_and_discord_role_artifacts(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        guild.members = [FakeMember(10, (100,), name="alice", display_name="Alice")]
        guild.member_count = 1
        alert_channel = types.SimpleNamespace(send=mock.AsyncMock())
        database_bytes = b"SQLite format 3\x00backup"
        cog._achievement_store.backup_database.return_value = database_bytes
        snapshot = types.SimpleNamespace(
            snapshot_at="2026-08-04T12:00:00+00:00",
            role_holders={100: (10,)},
            cached_member_count=1,
            reported_member_count=1,
        )

        await nhmisc.NHMisc._upload_achievement_sync_backup(
            cog,
            guild,
            alert_channel,
            snapshot,
        )

        send_call = alert_channel.send.await_args
        files = send_call.kwargs["files"]
        files_by_suffix = {Path(file.filename).suffixes[-1]: file for file in files}
        sqlite_file = files_by_suffix[".sqlite3"]
        jsonl_file = files_by_suffix[".gz"]
        self.assertEqual(sqlite_file.data, database_bytes)
        self.assertTrue(jsonl_file.data.startswith(b"\x1f\x8b"))
        self.assertIn(
            hashlib.sha256(sqlite_file.data).hexdigest(),
            send_call.args[0],
        )
        self.assertIn(
            hashlib.sha256(jsonl_file.data).hexdigest(),
            send_call.args[0],
        )

    async def test_rolesync_discord_requires_existing_analytics_snapshot(self):
        cog = self.make_cog()
        guild = FakeGuild()

        async def snapshot(_guild_id):
            self.assertIn(guild.id, cog._achievement_syncing_guilds)

        cog._achievement_discord_snapshot = mock.AsyncMock(side_effect=snapshot)
        ctx = make_context(guild)
        await bind_error_channel(cog, ctx)

        await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        ctx.send.assert_awaited_once_with(
            "Role analytics is not ready. Run `!rolesync` first, then run "
            "`!rolesync discord` again."
        )
        cog._achievement_store.bootstrap_guild.assert_not_awaited()
        self.assertNotIn(guild.id, cog._achievement_syncing_guilds)

    async def test_rolesync_discord_bootstraps_only_after_confirmation(self):
        cog = self.make_cog()
        guild = FakeGuild()
        ctx = make_context(guild)
        await bind_error_channel(cog, ctx)
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_store.is_bootstrapped.return_value = False
        cog._achievement_discord_snapshot = mock.AsyncMock(
            side_effect=(snapshot, snapshot)
        )
        cog._support.send_log_message = mock.AsyncMock(return_value=types.SimpleNamespace())
        cog.bot.wait_for = mock.AsyncMock(
            return_value=types.SimpleNamespace(
                guild=guild,
                channel=ctx.channel,
                author=ctx.author,
                content="confirm",
            )
        )
        cog._achievement_store.bootstrap_guild.return_value = True

        await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._upload_achievement_sync_backup.assert_awaited_once_with(
            guild,
            ctx.channel,
            snapshot,
        )
        cog._achievement_store.bootstrap_guild.assert_awaited_once_with(
            guild.id,
            gate_tiers={10: 1},
            boolean_definitions=(nhmisc.SOLO_GATER_DEFINITION,),
            boolean_users={"solo_gater": (11,)},
        )
        confirmation_check = cog.bot.wait_for.await_args.kwargs["check"]
        self.assertTrue(
            confirmation_check(
                types.SimpleNamespace(
                    guild=guild,
                    channel=ctx.channel,
                    author=ctx.author,
                    content="confirm",
                )
            )
        )
        self.assertFalse(
            confirmation_check(
                types.SimpleNamespace(
                    guild=guild,
                    channel=ctx.channel,
                    author=types.SimpleNamespace(id=ctx.author.id + 1),
                    content="confirm",
                )
            )
        )
        self.assertFalse(
            confirmation_check(
                types.SimpleNamespace(
                    guild=guild,
                    channel=types.SimpleNamespace(id=ctx.channel.id + 1),
                    author=ctx.author,
                    content="confirm",
                )
            )
        )
        self.assertNotIn(guild.id, cog._achievement_syncing_guilds)

    async def test_rolesync_discord_stops_before_plan_when_backup_upload_fails(self):
        cog = self.make_cog()
        guild = FakeGuild()
        ctx = make_context(guild)
        await bind_error_channel(cog, ctx)
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_store.is_bootstrapped.return_value = False
        cog._achievement_discord_snapshot = mock.AsyncMock(return_value=snapshot)
        cog._support.send_log_message = mock.AsyncMock(return_value=types.SimpleNamespace())
        cog._upload_achievement_sync_backup.side_effect = UserFeedbackCheckFailure("backup failed")

        with self.assertRaisesRegex(UserFeedbackCheckFailure, "backup failed"):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._support.send_log_message.assert_not_awaited()
        cog._achievement_store.bootstrap_guild.assert_not_awaited()

    async def test_rolesync_discord_rejects_invocation_outside_the_error_channel(self):
        cog = self.make_cog()
        guild = FakeGuild()
        ctx = make_context(guild)
        cog._achievement_discord_snapshot = mock.AsyncMock()

        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            "Configure the shared error channel before running this command",
        ):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        error_channel = FakeChannel(guild)
        error_channel.id = 999
        error_channel.mention = "#mod-logs"
        guild.channels[999] = error_channel
        await cog._support.config.guild(guild).error_channel.set(999)
        with self.assertRaisesRegex(UserFeedbackCheckFailure, "Run this command in #mod-logs"):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        guild.channels.clear()
        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            r"Run this command in the configured error channel \(`999`\)",
        ):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._achievement_discord_snapshot.assert_not_called()
        cog._upload_achievement_sync_backup.assert_not_awaited()

    async def test_rolesync_discord_rejects_a_public_error_channel(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        ctx = make_context(guild)
        await bind_error_channel(cog, ctx)
        cog._achievement_discord_snapshot = mock.AsyncMock()

        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            "hidden from",
        ):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._achievement_discord_snapshot.assert_not_called()
        cog._upload_achievement_sync_backup.assert_not_awaited()
        cog._achievement_store.bootstrap_guild.assert_not_awaited()

    async def test_rolesync_discord_requires_attach_files_in_the_error_channel(self):
        cog = self.make_cog()
        guild = FakeGuild(
            bot_permissions=types.SimpleNamespace(
                view_channel=True,
                send_messages=True,
                attach_files=False,
            )
        )
        ctx = make_context(guild)
        await bind_error_channel(cog, ctx)
        cog._achievement_discord_snapshot = mock.AsyncMock()

        with self.assertRaisesRegex(UserFeedbackCheckFailure, "attach files"):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._achievement_discord_snapshot.assert_not_called()
        cog._upload_achievement_sync_backup.assert_not_awaited()

    async def test_rolesync_discord_aborts_when_database_plan_changes(self):
        cog = self.make_cog()
        guild = FakeGuild()
        ctx = make_context(guild)
        await bind_error_channel(cog, ctx)
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_discord_snapshot = mock.AsyncMock(side_effect=(snapshot, snapshot))
        cog._achievement_discord_sync_summary = mock.AsyncMock(
            side_effect=("original plan", "changed plan")
        )
        cog._support.send_log_message = mock.AsyncMock(return_value=types.SimpleNamespace())
        cog.bot.wait_for = mock.AsyncMock(
            return_value=types.SimpleNamespace(
                guild=guild,
                channel=ctx.channel,
                author=ctx.author,
                content="confirm",
            )
        )

        await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._upload_achievement_sync_backup.assert_awaited_once_with(
            guild,
            ctx.channel,
            snapshot,
        )
        self.assertEqual(
            cog._support.send_log_message.await_args_list[-1].args,
            (
                ctx.channel,
                "Achievement data changed. Run `!rolesync discord` again.",
            ),
        )
        cog._achievement_store.apply_discord_snapshot.assert_not_awaited()
        cog._achievement_store.bootstrap_guild.assert_not_awaited()

    async def test_rolesync_discord_rejects_a_second_pending_plan(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        cog._achievement_syncing_guilds.add(guild.id)
        ctx = make_context(guild)

        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            "already awaiting confirmation",
        ):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._achievement_store.bootstrap_guild.assert_not_awaited()

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
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cog = object.__new__(nhmisc.NHMisc)
            cog._bot_proxy = None
            cog._bot_proxy_store = nhmisc.BotProxyStore(root / "bot_proxy.sqlite")
            cog._activity_store = nhmisc.ActivityStore(root / "activity.sqlite")
            cog._sticky_roles = nhmisc.StickyRoleStore(root / "sticky.sqlite")
            cog._role_analytics_store = nhmisc.RoleAnalyticsStore(root / "roles.sqlite")
            cog._achievement_store = nhmisc.AchievementStore(root / "achievements.sqlite")
            cog._gate_increment_store = nhmisc.GateIncrementStore(root / "achievements.sqlite")
            for store in (
                cog._activity_store,
                cog._sticky_roles,
                cog._role_analytics_store,
                cog._achievement_store,
                cog._gate_increment_store,
                cog._bot_proxy_store,
            ):
                await store.initialize()
            await _seed_nhmisc_user(cog, 42)
            await _seed_nhmisc_user(cog, 99)
            for user_id in (42, 99):
                await cog._bot_proxy_store.create_character(
                    guild_id=123, preset_name=f"Persona-{user_id}", display_name="Persona",
                    avatar_bytes=b"avatar", avatar_media_type="image/png", moderator_id=user_id,
                )

            await cog.red_delete_data_for_user(
                requester="discord_deleted_user",
                user_id=42,
            )

            await _assert_nhmisc_user_absent(self, cog, 42)
            await _assert_nhmisc_user_present(self, cog, 99)
            self.assertIsNone(await cog._bot_proxy_store.get_character(123, "Persona-42"))
            self.assertIsNotNone(await cog._bot_proxy_store.get_character(123, "Persona-99"))

    async def test_user_deletion_uses_active_proxy_manager_for_every_requester(self):
        for requester in ("user", "user_strict", "owner", "discord_deleted_user"):
            with self.subTest(requester=requester):
                cog = object.__new__(nhmisc.NHMisc)
                cog._bot_proxy = mock.Mock(delete_user_data=mock.AsyncMock())
                cog._bot_proxy_store = mock.Mock(delete_user_data=mock.AsyncMock())
                cog._activity_store = mock.Mock(delete_user_everywhere=mock.AsyncMock())
                cog._sticky_roles = mock.Mock(delete_user_everywhere=mock.AsyncMock())
                cog._role_analytics_store = mock.Mock(delete_user_everywhere=mock.AsyncMock())
                cog._achievement_store = mock.Mock(delete_user_everywhere=mock.AsyncMock())
                cog._gate_increment_store = mock.Mock(redact_user_data=mock.AsyncMock())
                await cog.red_delete_data_for_user(requester=requester, user_id=42)
                cog._bot_proxy.delete_user_data.assert_awaited_once_with(42)
                cog._bot_proxy_store.delete_user_data.assert_not_awaited()


def _achievement_kind():
    return sys.modules[nhmisc.AchievementStore.__module__].AchievementKind


def _member_snapshot():
    return sys.modules[nhmisc.RoleAnalyticsStore.__module__].MemberSnapshot


async def _seed_nhmisc_user(cog, user_id):
    moment = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)
    for guild_id, channel_id, role_id in ((10, 100, 1000), (11, 101, 1001)):
        await cog._activity_store.record_message(
            guild_id=guild_id,
            date_utc=date(2026, 7, 26),
            hour_utc=12,
            user_id=user_id,
            channel_id=channel_id,
            thread_id=None,
            now_utc=moment,
        )
        await cog._sticky_roles.replace_member_roles(guild_id, user_id, {role_id})
        snapshot = _member_snapshot()(user_id, False, (role_id,))
        state = await cog._role_analytics_store.get_state(guild_id)
        if state.active_generation is None:
            generation = await cog._role_analytics_store.next_generation(guild_id)
            await cog._role_analytics_store.write_generation(
                guild_id, generation, [snapshot]
            )
            await cog._role_analytics_store.activate_generation(guild_id, generation, 1)
        else:
            await cog._role_analytics_store.replace_member(guild_id, snapshot)
        if not await cog._achievement_store.is_bootstrapped(guild_id):
            definition = nhmisc.AchievementDefinition(
                key="badge",
                display_name="Badge",
                kind=_achievement_kind().BOOLEAN,
                display_order=0,
            )
            await cog._achievement_store.bootstrap_guild(
                guild_id,
                gate_tiers={},
                boolean_definitions=(definition,),
                boolean_users={},
            )
        await cog._achievement_store.grant_boolean(guild_id, user_id, "badge")
    await cog._gate_increment_store.claim(
        nhmisc.SourceMessageKey(10, 20, user_id),
        user_id,
        (nhmisc.GateIncrementMemberPlan(user_id, (), 8),),
    )


async def _assert_nhmisc_user_absent(test, cog, user_id):
    for guild_id in (10, 11):
        stats = await cog._activity_store.get_user_stats(
            guild_id, user_id, date(2026, 7, 26), 7
        )
        test.assertEqual(stats.total_messages, 0)
        test.assertEqual(await cog._sticky_roles.get_member_roles(guild_id, user_id), set())
        visible = await cog._role_analytics_store.matching_user_ids(guild_id, "1", ())
        test.assertNotIn(user_id, visible)
        profile = await cog._achievement_store.get_profile(guild_id, user_id)
        test.assertEqual(profile.boolean_keys, ())
    operation = await cog._gate_increment_store.get_operation(
        nhmisc.SourceMessageKey(10, 20, user_id)
    )
    test.assertIsNone(operation.operation.moderator_id)
    test.assertIsNone(operation.members[0].user_id)
    test.assertNotIn(user_id, _pending_stargate_user_ids(cog))


async def _assert_nhmisc_user_present(test, cog, user_id):
    for guild_id in (10, 11):
        stats = await cog._activity_store.get_user_stats(
            guild_id, user_id, date(2026, 7, 26), 7
        )
        test.assertGreaterEqual(stats.total_messages, 1)
        test.assertTrue(await cog._sticky_roles.get_member_roles(guild_id, user_id))
        visible = await cog._role_analytics_store.matching_user_ids(guild_id, "1", ())
        test.assertIn(user_id, visible)
        profile = await cog._achievement_store.get_profile(guild_id, user_id)
        test.assertIn("badge", profile.boolean_keys)
    operation = await cog._gate_increment_store.get_operation(
        nhmisc.SourceMessageKey(10, 20, user_id)
    )
    test.assertEqual(operation.operation.moderator_id, user_id)
    test.assertEqual(operation.members[0].user_id, user_id)
    test.assertIn(user_id, _pending_stargate_user_ids(cog))


def _pending_stargate_user_ids(cog):
    with sqlite3.connect(cog._gate_increment_store._path) as connection:
        rows = connection.execute(
            """
            SELECT user_id FROM achievement_awards
            WHERE state = 'pending' AND achievement_key = 'stargate_completed'
            """
        ).fetchall()
    return {row[0] for row in rows}


if __name__ == "__main__":
    unittest.main()
