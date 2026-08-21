import asyncio
import hashlib
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_NAME = "nhmisc_role_analytics_commands_test_package"
PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "nhmisc"


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

    def test_commands_require_manage_messages_and_expected_cooldowns(self):
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
                callback = getattr(nhmisc.NHMisc, command_name).callback
                self.assertEqual(
                    callback.required_permissions,
                    {"manage_messages": True},
                )
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
        self.assertEqual(
            hashlib.sha256(sqlite_file.data).hexdigest(),
            hashlib.sha256(database_bytes).hexdigest(),
        )
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
        guild = FakeGuild(public=True)

        async def snapshot(_guild_id):
            self.assertIn(guild.id, cog._achievement_syncing_guilds)

        cog._achievement_discord_snapshot = mock.AsyncMock(side_effect=snapshot)
        ctx = make_context(guild)

        await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        ctx.send.assert_awaited_once_with(
            "Role analytics is not ready. Run `!rolesync` first, then run "
            "`!rolesync discord` again."
        )
        cog._achievement_store.bootstrap_guild.assert_not_awaited()
        self.assertNotIn(guild.id, cog._achievement_syncing_guilds)

    async def test_rolesync_discord_bootstraps_only_after_confirmation(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        ctx = make_context(guild)
        maintenance_channel = FakeChannel(guild)
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_store.is_bootstrapped.return_value = False
        cog._achievement_discord_snapshot = mock.AsyncMock(
            side_effect=(snapshot, snapshot)
        )
        alert_setting = mock.AsyncMock(return_value=999)
        maintenance_setting = mock.AsyncMock(return_value=321)
        cog.config = types.SimpleNamespace(
            guild=lambda _guild: types.SimpleNamespace(
                alert_channel=alert_setting,
                maintenance_channel=maintenance_setting,
            )
        )
        cog._get_log_channel = mock.Mock(return_value=maintenance_channel)
        cog._send_voice_log = mock.AsyncMock(return_value=types.SimpleNamespace())
        cog.bot.wait_for = mock.AsyncMock(
            return_value=types.SimpleNamespace(
                guild=guild,
                channel=maintenance_channel,
                author=ctx.author,
                content="confirm",
            )
        )
        cog._achievement_store.bootstrap_guild.return_value = True

        await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._upload_achievement_sync_backup.assert_awaited_once_with(
            guild,
            maintenance_channel,
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
                    channel=maintenance_channel,
                    author=ctx.author,
                    content="confirm",
                )
            )
        )
        self.assertFalse(
            confirmation_check(
                types.SimpleNamespace(
                    guild=guild,
                    channel=maintenance_channel,
                    author=types.SimpleNamespace(id=ctx.author.id + 1),
                    content="confirm",
                )
            )
        )
        maintenance_setting.assert_awaited_once_with()
        alert_setting.assert_not_awaited()
        self.assertNotIn(guild.id, cog._achievement_syncing_guilds)

    async def test_rolesync_discord_stops_before_plan_when_backup_upload_fails(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        ctx = make_context(guild)
        alert_channel = FakeChannel(guild)
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_store.is_bootstrapped.return_value = False
        cog._achievement_discord_snapshot = mock.AsyncMock(return_value=snapshot)
        cog.config = types.SimpleNamespace(
            guild=lambda _guild: types.SimpleNamespace(
                maintenance_channel=mock.AsyncMock(return_value=321)
            )
        )
        cog._get_log_channel = mock.Mock(return_value=alert_channel)
        cog._send_voice_log = mock.AsyncMock(return_value=types.SimpleNamespace())
        cog._upload_achievement_sync_backup.side_effect = UserFeedbackCheckFailure("backup failed")

        with self.assertRaisesRegex(UserFeedbackCheckFailure, "backup failed"):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._send_voice_log.assert_not_awaited()
        cog._achievement_store.bootstrap_guild.assert_not_awaited()

    async def test_rolesync_discord_rejects_a_public_maintenance_channel(self):
        cog = self.make_cog()
        guild = FakeGuild()
        ctx = make_context(guild)
        alert_channel = FakeChannel(guild, public=True)
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_discord_snapshot = mock.AsyncMock(return_value=snapshot)
        cog.config = types.SimpleNamespace(
            guild=lambda _guild: types.SimpleNamespace(
                maintenance_channel=mock.AsyncMock(return_value=321)
            )
        )
        cog._get_log_channel = mock.Mock(return_value=alert_channel)

        with self.assertRaisesRegex(
            UserFeedbackCheckFailure,
            "Configure a private NHMisc maintenance channel first",
        ):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._upload_achievement_sync_backup.assert_not_awaited()
        cog._achievement_store.is_bootstrapped.assert_not_awaited()
        cog._achievement_store.bootstrap_guild.assert_not_awaited()

    async def test_rolesync_discord_requires_attach_files_in_maintenance_channel(self):
        cog = self.make_cog()
        guild = FakeGuild()
        ctx = make_context(guild)
        maintenance_channel = FakeChannel(
            guild,
            bot_permissions=types.SimpleNamespace(
                view_channel=True,
                send_messages=True,
                attach_files=False,
            ),
        )
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_discord_snapshot = mock.AsyncMock(return_value=snapshot)
        cog.config = types.SimpleNamespace(
            guild=lambda _guild: types.SimpleNamespace(
                maintenance_channel=mock.AsyncMock(return_value=321)
            )
        )
        cog._get_log_channel = mock.Mock(return_value=maintenance_channel)

        with self.assertRaisesRegex(UserFeedbackCheckFailure, "attach files"):
            await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._upload_achievement_sync_backup.assert_not_awaited()

    async def test_rolesync_discord_aborts_when_database_plan_changes(self):
        cog = self.make_cog()
        guild = FakeGuild(public=True)
        ctx = make_context(guild)
        alert_channel = FakeChannel(guild)
        snapshot = nhmisc.build_discord_role_snapshot(
            snapshot_at="2026-08-04T12:00:00+00:00",
            users_by_gate_role=((10,), (), (), (), (), ()),
            boolean_users={"solo_gater": (11,)},
        )
        cog._achievement_discord_snapshot = mock.AsyncMock(side_effect=(snapshot, snapshot))
        cog._achievement_discord_sync_summary = mock.AsyncMock(
            side_effect=("original plan", "changed plan")
        )
        cog.config = types.SimpleNamespace(
            guild=lambda _guild: types.SimpleNamespace(
                maintenance_channel=mock.AsyncMock(return_value=321)
            )
        )
        cog._get_log_channel = mock.Mock(return_value=alert_channel)
        cog._send_voice_log = mock.AsyncMock(return_value=types.SimpleNamespace())
        cog.bot.wait_for = mock.AsyncMock(
            return_value=types.SimpleNamespace(
                guild=guild,
                channel=alert_channel,
                author=ctx.author,
                content="confirm",
            )
        )

        await nhmisc.NHMisc.rolesync_discord.callback(cog, ctx)

        cog._upload_achievement_sync_backup.assert_awaited_once_with(
            guild,
            alert_channel,
            snapshot,
        )
        self.assertEqual(
            cog._send_voice_log.await_args_list[-1].args,
            (
                alert_channel,
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
        cog = self.make_cog()
        cog.config = types.SimpleNamespace(all_guilds=mock.AsyncMock(return_value={}))

        await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=42)

        cog._activity_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._sticky_roles.delete_user_everywhere.assert_awaited_once_with(42)
        cog._role_analytics_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._achievement_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._gate_increment_store.redact_user_data.assert_awaited_once_with(42)


if __name__ == "__main__":
    unittest.main()
