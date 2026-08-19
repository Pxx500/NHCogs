"""Safety policy at the Discord punishment boundary."""

import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules


class _Store:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Ranked:
    def __init__(self, position):
        self.position = position

    def __le__(self, other):
        return self.position <= other.position

    def __ge__(self, other):
        return self.position >= other.position


class _FakeEmbed:
    def __init__(self, *, description=None, **_kwargs):
        self.description = description
        self.fields = []

    def set_author(self, **_kwargs):
        pass

    def set_thumbnail(self, **_kwargs):
        pass

    def add_field(self, *, name, value, inline):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


class _CompletedTask:
    def add_done_callback(self, callback):
        callback(self)


class _DiscardingLoop:
    def create_task(self, coroutine, *, name):
        coroutine.close()
        return _CompletedTask()


class PunitiveEffectPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_current_dry_run_plans(self, action: str) -> None:
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                from Honeypot.effects import (  # noqa: PLC0415
                    EffectStatus,
                    ModerationOrigin,
                )

                cog = honeypot.Honeypot(_Bot())
                current_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"dry_run": True})
                )
                cog.config = SimpleNamespace(
                    guild=mock.Mock(return_value=current_config)
                )
                cog._increment_stat = mock.AsyncMock()
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._schedule_post_ban_sweep = mock.Mock()
                honeypot.detection._activate_forward_purge = mock.Mock()

                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=11),
                    ban=mock.AsyncMock(),
                )
                member = SimpleNamespace(
                    id=20,
                    ban=mock.AsyncMock(),
                    kick=mock.AsyncMock(),
                )
                modlog_create_case = mock.AsyncMock()
                honeypot.detection.modlog.create_case = modlog_create_case
                stale_settings = honeypot.GuildSettings.from_mapping(
                    {"dry_run": False}
                )

                result = await cog._execute_action(
                    guild,
                    member,
                    datetime.now(timezone.utc),
                    stale_settings,
                    reason="Punishment safety test",
                    origin=ModerationOrigin.AUTOMATIC,
                    action=action,
                )

                member.ban.assert_not_awaited()
                member.kick.assert_not_awaited()
                modlog_create_case.assert_not_awaited()
                self.assertEqual(result.status, EffectStatus.PLANNED)

    async def test_current_dry_run_blocks_ban_with_stale_settings(self):
        await self._assert_current_dry_run_plans("ban")

    async def test_current_dry_run_blocks_kick_with_stale_settings(self):
        await self._assert_current_dry_run_plans("kick")

    async def test_successful_ban_records_its_declared_daily_origin(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                from Honeypot.effects import (  # noqa: PLC0415
                    EffectStatus,
                    ModerationOrigin,
                )

                cog = honeypot.Honeypot(_Bot())
                current_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"dry_run": False})
                )
                cog.config = SimpleNamespace(
                    guild=mock.Mock(return_value=current_config)
                )
                cog._increment_stat = mock.AsyncMock()
                cog._record_daily_stat = mock.AsyncMock()
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._schedule_post_ban_sweep = mock.Mock()
                honeypot.detection._activate_forward_purge = mock.Mock()
                honeypot.detection.modlog.create_case = mock.AsyncMock()
                guild = SimpleNamespace(id=10, me=SimpleNamespace(id=11))
                member = SimpleNamespace(id=20, ban=mock.AsyncMock())
                settings = honeypot.GuildSettings.from_mapping({"dry_run": False})
                occurred_at = datetime(2026, 8, 19, 20, tzinfo=timezone.utc)

                for origin, metric in (
                    (ModerationOrigin.AUTOMATIC, "automated_bans"),
                    (ModerationOrigin.MANUAL, "manual_bans"),
                ):
                    with self.subTest(origin=origin):
                        cog._record_daily_stat.reset_mock()
                        result = await cog._execute_action(
                            guild,
                            member,
                            occurred_at,
                            settings,
                            reason="Punishment safety test",
                            action="ban",
                            origin=origin,
                        )

                        self.assertEqual(result.status, EffectStatus.SUCCEEDED)
                        cog._record_daily_stat.assert_awaited_once_with(
                            guild, mock.ANY, metric
                        )

    async def test_successful_ban_records_daily_stat_before_lifetime_counter(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                from Honeypot.effects import ModerationOrigin  # noqa: PLC0415

                cog = honeypot.Honeypot(_Bot())
                current_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"dry_run": False})
                )
                cog.config = SimpleNamespace(
                    guild=mock.Mock(return_value=current_config)
                )
                cog._increment_stat = mock.AsyncMock(
                    side_effect=RuntimeError("config unavailable")
                )
                cog._record_daily_stat = mock.AsyncMock()
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._schedule_post_ban_sweep = mock.Mock()
                honeypot.detection._activate_forward_purge = mock.Mock()
                guild = SimpleNamespace(id=10, me=SimpleNamespace(id=11))
                member = SimpleNamespace(id=20, ban=mock.AsyncMock())
                settings = honeypot.GuildSettings.from_mapping({"dry_run": False})

                with self.assertRaisesRegex(RuntimeError, "config unavailable"):
                    await cog._execute_action(
                        guild,
                        member,
                        datetime(2026, 8, 19, 20, tzinfo=timezone.utc),
                        settings,
                        reason="Punishment safety test",
                        action="ban",
                        origin=ModerationOrigin.AUTOMATIC,
                    )

                member.ban.assert_awaited_once()
                cog._record_daily_stat.assert_awaited_once_with(
                    guild, mock.ANY, "automated_bans"
                )

    async def test_successful_kick_fail_warning_keeps_failed_effect_status(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                from Honeypot.effects import (  # noqa: PLC0415
                    EffectStatus,
                    ModerationOrigin,
                )

                cog = honeypot.Honeypot(_Bot())
                current_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"dry_run": False})
                )
                cog.config = SimpleNamespace(
                    guild=mock.Mock(return_value=current_config)
                )
                cog._increment_stat = mock.AsyncMock()
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._deactivate_forward_purge = mock.Mock()
                cog._get_user_or_object = mock.AsyncMock()
                honeypot.detection._activate_forward_purge = mock.Mock()

                guild = SimpleNamespace(id=10, me=SimpleNamespace(id=11))
                member = SimpleNamespace(
                    id=20,
                    kick=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound(
                            "kick target missing"
                        )
                    ),
                )
                cog._get_user_or_object.return_value = member
                modlog_create_case = mock.AsyncMock()
                honeypot.modlog.create_case = modlog_create_case
                settings = honeypot.GuildSettings.from_mapping(
                    {
                        "automated_kick_fail_warning": True,
                        "dry_run": False,
                    }
                )

                result = await cog._execute_action(
                    guild,
                    member,
                    datetime.now(timezone.utc),
                    settings,
                    reason="Punishment safety test",
                    origin=ModerationOrigin.AUTOMATIC,
                    action="kick",
                )

                member.kick.assert_awaited_once()
                modlog_create_case.assert_awaited_once()
                self.assertIsNone(result.failed_message)
                self.assertEqual(result.status, EffectStatus.FAILED)


class UnknownKickRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _run_reclaimed_kick(
        self,
        honeypot,
        operation_type,
        *,
        dry_run=False,
        guild_available=True,
    ):
        now = datetime.now(timezone.utc)
        member = SimpleNamespace(
            id=20,
            joined_at=now - timedelta(minutes=5),
            kick=mock.AsyncMock(),
        )
        moderator = SimpleNamespace(id=777)
        guild = SimpleNamespace(
            id=10,
            me=SimpleNamespace(id=11),
            get_member=lambda user_id: (
                member if user_id == member.id else moderator
            ),
        )
        bot = _Bot()
        bot.get_guild = lambda guild_id: (
            guild if guild_available and guild_id == guild.id else None
        )
        cog = honeypot.Honeypot(bot)
        guild_config = SimpleNamespace(
            all=mock.AsyncMock(return_value={"dry_run": dry_run})
        )
        cog.config = SimpleNamespace(
            guild_from_id=lambda guild_id: guild_config,
            guild=lambda configured_guild: guild_config,
        )
        cog._increment_stat = mock.AsyncMock()
        cog._missing_action_permission = mock.Mock(return_value=None)
        honeypot.detection._activate_forward_purge = mock.Mock()
        case_store = SimpleNamespace(
            operation_effect_started=mock.Mock(return_value=True),
            mark_case_needs_attention=mock.Mock(),
            start_operation_effect=mock.Mock(return_value=True),
        )
        cog._case_store = case_store
        operation = SimpleNamespace(
            operation_id=f"operation-{operation_type.value}",
            operation_type=operation_type,
            message_sequence=1,
            actor_id=moderator.id,
            claim_token="claim-token",
        )
        snapshot = SimpleNamespace(
            case=SimpleNamespace(case_id="case-1", guild_id=10, user_id=20),
            messages=(SimpleNamespace(sequence=1, created_at=now),),
            signals=(
                SimpleNamespace(
                    message_sequence=1,
                    signal=honeypot.DetectionSignal(
                        "spam",
                        "duplicate",
                        honeypot.ActionIntent.KICK,
                        True,
                        {},
                    ),
                ),
            ),
        )
        context = honeypot.OperationContext(
            operation=operation,
            snapshot=snapshot,
            lease=honeypot.OperationLease(
                operation_id=operation.operation_id,
                claim_token=operation.claim_token,
            ),
            now=now,
        )
        handler = import_module(
            "Honeypot.operations"
        ).OperationHandlerRegistry().resolve(operation_type)

        with mock.patch.object(
            honeypot.modlog,
            "create_case",
            new=mock.AsyncMock(),
            create=True,
        ):
            outcome = await handler(cog, context)
        return outcome, member, case_store

    async def _assert_reclaimed_kick_is_terminal(
        self,
        operation_type,
        *,
        dry_run=False,
        guild_available=True,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                try:
                    outcome, member, case_store = await self._run_reclaimed_kick(
                        honeypot,
                        honeypot.OperationType(operation_type),
                        dry_run=dry_run,
                        guild_available=guild_available,
                    )
                except RuntimeError as error:
                    self.fail(f"reclaimed kick was not terminal: {error}")

                self.assertEqual(outcome.result, "kick_outcome_unknown")
                member.kick.assert_not_awaited()
                case_store.mark_case_needs_attention.assert_called_once_with("case-1")

    async def test_automatic_reclaimed_kick_stops_with_unknown_outcome(self):
        await self._assert_reclaimed_kick_is_terminal("moderation_action")

    async def test_moderator_reclaimed_kick_stops_with_unknown_outcome(self):
        await self._assert_reclaimed_kick_is_terminal("moderator_kick")

    async def test_automatic_reclaimed_kick_ignores_new_dry_run_setting(self):
        await self._assert_reclaimed_kick_is_terminal(
            "moderation_action", dry_run=True
        )

    async def test_automatic_reclaimed_kick_is_terminal_without_guild(self):
        await self._assert_reclaimed_kick_is_terminal(
            "moderation_action", guild_available=False
        )

    async def test_moderator_reclaimed_kick_ignores_new_dry_run_setting(self):
        await self._assert_reclaimed_kick_is_terminal("moderator_kick", dry_run=True)

    async def test_moderator_reclaimed_kick_is_terminal_without_guild(self):
        await self._assert_reclaimed_kick_is_terminal(
            "moderator_kick", guild_available=False
        )


class JoinwatchDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_dry_run_plans_immediate_role_without_tracking_removal(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = _Ranked(5)
                role.id = 501
                role.mention = "<@&501>"
                alert_channel = SimpleNamespace(id=601, send=mock.AsyncMock())
                alert_channel.send.return_value = SimpleNamespace(
                    id=701,
                    channel=alert_channel,
                )
                guild = SimpleNamespace(
                    id=100,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(manage_roles=True),
                        top_role=_Ranked(10),
                    ),
                    get_role=lambda role_id: role if role_id == role.id else None,
                    get_channel=lambda channel_id: (
                        alert_channel if channel_id == alert_channel.id else None
                    ),
                    get_thread=lambda _channel_id: None,
                )
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
                    guild_permissions=SimpleNamespace(manage_guild=False),
                    top_role=_Ranked(1),
                    add_roles=mock.AsyncMock(),
                )
                pending_roles = {}
                stats = {}
                raw_config = {
                    "dry_run": True,
                    "joinwatch_enabled": True,
                    "joinwatch_alert_enabled": True,
                    "joinwatch_channel": alert_channel.id,
                    "joinwatch_min_age_hours": 24,
                    "joinwatch_auto_role_enabled": True,
                    "joinwatch_auto_role_id": role.id,
                    "joinwatch_auto_role_timer_minutes": 30,
                    "joinwatch_auto_role_random_delay_enabled": False,
                    "joinwatch_pending_roles": pending_roles,
                }
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value=raw_config),
                    joinwatch_pending_roles=lambda: _Store(pending_roles),
                    stats=lambda: _Store(stats),
                )
                bot = _Bot()
                bot.owner_ids = ()
                bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(guild=lambda _guild: guild_config)

                with mock.patch.object(honeypot.discord, "Embed", _FakeEmbed), mock.patch.object(
                    honeypot.discord,
                    "Color",
                    SimpleNamespace(orange=mock.Mock(return_value=None)),
                ), mock.patch.object(
                    honeypot.discord,
                    "utils",
                    SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                    create=True,
                ):
                    await cog.on_member_join(member)

                member.add_roles.assert_not_awaited()
                self.assertNotIn(str(member.id), pending_roles)
                moderator_embed = alert_channel.send.await_args.kwargs["embed"]
                self.assertTrue(
                    any("dry run" in field.value.lower() for field in moderator_embed.fields)
                )

    async def test_current_dry_run_discards_due_assignment_without_tracking_removal(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = _Ranked(5)
                role.id = 501
                role.mention = "<@&501>"
                guild = SimpleNamespace(
                    id=100,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(manage_roles=True),
                        top_role=_Ranked(10),
                    ),
                    get_channel=lambda _channel_id: None,
                    get_thread=lambda _channel_id: None,
                    get_role=lambda role_id: role if role_id == role.id else None,
                )
                member = SimpleNamespace(
                    id=200,
                    guild=guild,
                    roles=[],
                    guild_permissions=SimpleNamespace(manage_guild=False),
                    top_role=_Ranked(1),
                    add_roles=mock.AsyncMock(),
                )
                guild.get_member = lambda member_id: (
                    member if member_id == member.id else None
                )
                guild.fetch_member = mock.AsyncMock(return_value=member)
                assignments = {
                    str(member.id): {
                        "role_id": role.id,
                        "apply_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=1)
                        ).isoformat(),
                    }
                }
                pending_roles = {}
                stats = {}
                raw_config = {
                    "dry_run": True,
                    "joinwatch_auto_role_enabled": True,
                    "joinwatch_auto_role_id": role.id,
                    "joinwatch_auto_role_timer_minutes": 30,
                    "joinwatch_pending_role_assignments": assignments,
                    "joinwatch_pending_roles": pending_roles,
                }
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value=raw_config),
                    joinwatch_pending_role_assignments=lambda: _Store(assignments),
                    joinwatch_pending_roles=lambda: _Store(pending_roles),
                    stats=lambda: _Store(stats),
                )
                bot = _Bot()
                bot.guilds = [guild]
                bot.owner_ids = ()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(guild=lambda _guild: guild_config)

                with mock.patch.object(
                    honeypot.discord,
                    "utils",
                    SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                    create=True,
                ):
                    await cog.joinwatch_auto_role_loop.function(cog)

                member.add_roles.assert_not_awaited()
                self.assertNotIn(str(member.id), assignments)
                self.assertNotIn(str(member.id), pending_roles)

    async def test_current_dry_run_plans_due_punishment_from_stale_settings(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = _Ranked(5)
                role.id = 501
                role.mention = "<@&501>"
                guild = SimpleNamespace(
                    id=100,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            manage_roles=True,
                            kick_members=True,
                            ban_members=True,
                        ),
                        top_role=_Ranked(10),
                    ),
                    get_role=lambda role_id: role if role_id == role.id else None,
                    ban=mock.AsyncMock(),
                )
                member = SimpleNamespace(
                    id=200,
                    guild=guild,
                    roles=[role],
                    mention="<@200>",
                    guild_permissions=SimpleNamespace(manage_guild=False),
                    top_role=_Ranked(1),
                    kick=mock.AsyncMock(),
                )
                guild.get_member = lambda member_id: (
                    member if member_id == member.id else None
                )
                guild.fetch_member = mock.AsyncMock(return_value=member)
                assignments = {}
                pending_roles = {
                    str(member.id): {
                        "role_id": role.id,
                        "applied_at": (
                            datetime.now(timezone.utc) - timedelta(hours=1)
                        ).isoformat(),
                        "expires_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=1)
                        ).isoformat(),
                    }
                }
                stats = {}
                stale_config = {
                    "dry_run": False,
                    "joinwatch_auto_role_enabled": True,
                    "joinwatch_auto_role_id": role.id,
                    "joinwatch_auto_role_action": "kick",
                    "joinwatch_pending_role_assignments": assignments,
                    "joinwatch_pending_roles": pending_roles,
                }
                current_config = dict(stale_config, dry_run=True)
                config_reads = iter((stale_config, current_config))
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(side_effect=lambda: next(config_reads)),
                    joinwatch_pending_role_assignments=lambda: _Store(assignments),
                    joinwatch_pending_roles=lambda: _Store(pending_roles),
                    stats=lambda: _Store(stats),
                )
                moderator_channel = SimpleNamespace(send=mock.AsyncMock())
                bot = _Bot()
                bot.guilds = [guild]
                bot.owner_ids = ()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(guild=lambda _guild: guild_config)
                cog._get_text_channel_or_thread = mock.Mock(
                    return_value=moderator_channel
                )

                with mock.patch.object(
                    honeypot.discord,
                    "Color",
                    SimpleNamespace(dark_red=lambda: 0, orange=lambda: 0),
                ), mock.patch.object(
                    honeypot.discord, "Embed", _FakeEmbed
                ), mock.patch.object(
                    honeypot.modlog,
                    "create_case",
                    new=mock.AsyncMock(),
                    create=True,
                ):
                    await cog.joinwatch_auto_role_loop.function(cog)

                member.kick.assert_not_awaited()
                guild.ban.assert_not_awaited()
                self.assertNotIn(str(member.id), pending_roles)
                self.assertEqual(stats["dry_run_actions"], 1)
                moderator_channel.send.assert_awaited_once()
                planned_embed = moderator_channel.send.await_args.kwargs["embed"]
                self.assertIn("dry run", planned_embed.fields[0].value.lower())

    async def test_dry_run_permission_error_cannot_leave_retry_that_later_applies(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = _Ranked(5)
                role.id = 501
                role.mention = "<@&501>"
                bot_permissions = SimpleNamespace(manage_roles=False)
                guild = SimpleNamespace(
                    id=100,
                    me=SimpleNamespace(
                        guild_permissions=bot_permissions,
                        top_role=_Ranked(10),
                    ),
                    get_channel=lambda _channel_id: None,
                    get_thread=lambda _channel_id: None,
                    get_role=lambda role_id: role if role_id == role.id else None,
                )
                member = SimpleNamespace(
                    id=200,
                    guild=guild,
                    roles=[],
                    guild_permissions=SimpleNamespace(manage_guild=False),
                    top_role=_Ranked(1),
                    add_roles=mock.AsyncMock(),
                )
                guild.get_member = lambda member_id: (
                    member if member_id == member.id else None
                )
                guild.fetch_member = mock.AsyncMock(return_value=member)
                member_key = str(member.id)
                assignments = {
                    member_key: {
                        "role_id": role.id,
                        "apply_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=1)
                        ).isoformat(),
                    }
                }
                pending_roles = {}
                stats = {}
                raw_config = {
                    "dry_run": True,
                    "joinwatch_auto_role_enabled": True,
                    "joinwatch_auto_role_id": role.id,
                    "joinwatch_auto_role_timer_minutes": 30,
                    "joinwatch_pending_role_assignments": assignments,
                    "joinwatch_pending_roles": pending_roles,
                }
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(side_effect=lambda: raw_config),
                    joinwatch_pending_role_assignments=lambda: _Store(assignments),
                    joinwatch_pending_roles=lambda: _Store(pending_roles),
                    stats=lambda: _Store(stats),
                )
                bot = _Bot()
                bot.guilds = [guild]
                bot.owner_ids = ()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(guild=lambda _guild: guild_config)
                cog._case_store.initialize()

                with mock.patch.object(
                    honeypot.discord,
                    "utils",
                    SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                    create=True,
                ):
                    await cog.joinwatch_auto_role_loop.function(cog)

                    raw_config["dry_run"] = False
                    bot_permissions.manage_roles = True
                    if member_key in assignments:
                        assignments[member_key]["apply_at"] = (
                            datetime.now(timezone.utc) - timedelta(minutes=1)
                        ).isoformat()
                    await cog.joinwatch_auto_role_loop.function(cog)

                member.add_roles.assert_not_awaited()
                self.assertNotIn(member_key, assignments)
                self.assertNotIn(member_key, pending_roles)


class BaitRoleSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def _run_bait_ban(
        self,
        *,
        dry_run: bool,
        discord_failure: bool = False,
        modlog_failure: bool = False,
    ):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        modules = _isolated_honeypot_modules(Path(directory.name))
        honeypot = modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)

        bait_role = SimpleNamespace(id=501)
        bait_channel = SimpleNamespace(id=601, send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=100,
            me=SimpleNamespace(
                id=101,
                guild_permissions=SimpleNamespace(
                    ban_members=True,
                    kick_members=True,
                ),
                top_role=_Ranked(10),
            ),
            get_role=lambda role_id: bait_role if role_id == bait_role.id else None,
            get_channel=lambda channel_id: (
                bait_channel if channel_id == bait_channel.id else None
            ),
            get_thread=lambda _channel_id: None,
        )
        common_member = {
            "id": 200,
            "guild": guild,
            "bot": False,
            "mention": "<@200>",
            "display_avatar": None,
            "guild_permissions": SimpleNamespace(manage_guild=False),
            "top_role": _Ranked(1),
        }
        before = SimpleNamespace(**common_member, roles=[])
        after = SimpleNamespace(
            **common_member,
            roles=[bait_role],
            ban=mock.AsyncMock(
                side_effect=(
                    honeypot.discord.HTTPException("ban failed")
                    if discord_failure
                    else None
                )
            ),
            kick=mock.AsyncMock(),
        )
        pending_roles = {}
        stats = {}
        raw_config = {
            "dry_run": dry_run,
            "baitrole_channel": bait_channel.id,
            "baitrole_enabled": True,
            "baitrole_id": bait_role.id,
            "baitrole_action": "ban",
            "joinwatch_pending_roles": pending_roles,
        }
        guild_config = SimpleNamespace(
            all=mock.AsyncMock(return_value=raw_config),
            joinwatch_pending_roles=lambda: _Store(pending_roles),
            stats=lambda: _Store(stats),
        )
        bot = _Bot()
        bot.owner_ids = ()
        bot.loop = _DiscardingLoop()
        bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
        bot.is_mod = mock.AsyncMock(return_value=False)
        bot.is_admin = mock.AsyncMock(return_value=False)
        cog = honeypot.Honeypot(bot)
        cog.config = SimpleNamespace(
            guild=lambda _guild: guild_config,
            guild_from_id=lambda _guild_id: guild_config,
        )
        cog._case_store.initialize()
        modlog_create_case = mock.AsyncMock(
            side_effect=(RuntimeError("modlog failed") if modlog_failure else None)
        )

        with mock.patch.object(honeypot.discord, "Embed", _FakeEmbed), mock.patch.object(
            honeypot.discord,
            "Color",
            SimpleNamespace(dark_red=mock.Mock(return_value=None)),
        ), mock.patch.object(
            honeypot.modlog,
            "create_case",
            modlog_create_case,
            create=True,
        ):
            await cog.on_member_update(before, after)

        moderator_embed = bait_channel.send.await_args.kwargs["embed"]
        return after, modlog_create_case, moderator_embed

    async def test_current_dry_run_plans_bait_ban(self):
        member, modlog_create_case, moderator_embed = await self._run_bait_ban(
            dry_run=True
        )

        member.ban.assert_not_awaited()
        modlog_create_case.assert_not_awaited()
        self.assertIn("dry run", moderator_embed.description.lower())

    async def test_failed_bait_ban_reports_failure_without_modlog(self):
        member, modlog_create_case, moderator_embed = await self._run_bait_ban(
            dry_run=False,
            discord_failure=True,
        )

        member.ban.assert_awaited_once()
        modlog_create_case.assert_not_awaited()
        self.assertIn("failed", moderator_embed.description.lower())

    async def test_successful_bait_ban_reports_punishment(self):
        member, modlog_create_case, moderator_embed = await self._run_bait_ban(
            dry_run=False
        )

        member.ban.assert_awaited_once()
        modlog_create_case.assert_awaited_once()
        self.assertIn("was banned", moderator_embed.description.lower())

    async def test_successful_bait_ban_reports_modlog_failure(self):
        member, modlog_create_case, moderator_embed = await self._run_bait_ban(
            dry_run=False,
            modlog_failure=True,
        )

        member.ban.assert_awaited_once()
        modlog_create_case.assert_awaited_once()
        self.assertIn("modlog", moderator_embed.description.lower())
