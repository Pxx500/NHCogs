"""Joinwatch retry loop: role application, expiry and the retry bookkeeping
that survives a failed Discord call.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules


class JoinwatchRetryTests(unittest.IsolatedAsyncioTestCase):
    class _Store:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def test_loop_applies_assignment_before_expiring_active_role(self):
        class Ranked:
            def __init__(self, position):
                self.position = position

            def __le__(self, other):
                return self.position <= other.position

            def __ge__(self, other):
                return self.position >= other.position

        class FakeEmbed:
            def __init__(self, fields=()):
                self.fields = list(fields)

            @classmethod
            def from_dict(cls, data):
                return cls(SimpleNamespace(**field) for field in data.get("fields", ()))

            def set_field_at(self, index, *, name, value, inline):
                self.fields[index] = SimpleNamespace(
                    name=name,
                    value=value,
                    inline=inline,
                )

            def add_field(self, *, name, value, inline):
                self.fields.append(
                    SimpleNamespace(name=name, value=value, inline=inline)
                )

        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                effects = []
                role = Ranked(5)
                role.id = 501
                role.mention = "<@&501>"
                me = SimpleNamespace(
                    guild_permissions=SimpleNamespace(
                        manage_roles=True,
                        kick_members=True,
                        ban_members=False,
                    ),
                    top_role=Ranked(10),
                )
                assignment_message = SimpleNamespace(
                    embeds=[
                        SimpleNamespace(
                            to_dict=lambda: {
                                "fields": [
                                    {
                                        "name": "Auto-role:",
                                        "value": "Pending.",
                                        "inline": False,
                                    }
                                ]
                            }
                        )
                    ],
                    edit=mock.AsyncMock(),
                )
                expiration_message = SimpleNamespace(
                    embeds=[
                        SimpleNamespace(
                            to_dict=lambda: {
                                "fields": [
                                    {
                                        "name": "Auto-role:",
                                        "value": "Active.",
                                        "inline": False,
                                    }
                                ]
                            }
                        )
                    ],
                    edit=mock.AsyncMock(),
                )
                channels = {
                    601: SimpleNamespace(
                        fetch_message=mock.AsyncMock(return_value=assignment_message)
                    ),
                    602: SimpleNamespace(
                        fetch_message=mock.AsyncMock(return_value=expiration_message)
                    ),
                }
                guild = SimpleNamespace(
                    id=100,
                    me=me,
                    get_channel=lambda channel_id: channels.get(channel_id),
                    get_thread=lambda channel_id: None,
                )

                async def add_assignment_role(*_args, **_kwargs):
                    effects.append(("add-role", 201))

                async def kick_active_member(*_args, **_kwargs):
                    effects.append(("kick", 202))

                assignment_member = SimpleNamespace(
                    id=201,
                    guild=guild,
                    guild_permissions=SimpleNamespace(manage_guild=False),
                    top_role=Ranked(1),
                    roles=[],
                    mention="<@201>",
                    add_roles=mock.AsyncMock(side_effect=add_assignment_role),
                )
                active_member = SimpleNamespace(
                    id=202,
                    guild=guild,
                    guild_permissions=SimpleNamespace(manage_guild=False),
                    top_role=Ranked(1),
                    roles=[role],
                    mention="<@202>",
                    kick=mock.AsyncMock(side_effect=kick_active_member),
                )
                members = {201: assignment_member, 202: active_member}
                guild.get_member = lambda member_id: members.get(member_id)
                guild.fetch_member = mock.AsyncMock(
                    side_effect=lambda member_id: members.get(member_id)
                )
                guild.get_role = lambda role_id: role if role_id == 501 else None
                assignments = {
                    "201": {
                        "role_id": 501,
                        "apply_at": "2026-07-15T11:59:00+00:00",
                        "alert_channel_id": 601,
                        "alert_message_id": 701,
                    }
                }
                roles = {
                    "202": {
                        "role_id": 501,
                        "applied_at": "2026-07-15T11:00:00+00:00",
                        "expires_at": "2026-07-15T11:59:00+00:00",
                        "alert_channel_id": 602,
                        "alert_message_id": 702,
                    }
                }
                stats = {}
                raw_config = {
                    "dry_run": False,
                    "joinwatch_auto_role_enabled": True,
                    "joinwatch_auto_role_id": 501,
                    "joinwatch_auto_role_timer_minutes": 30,
                    "joinwatch_auto_role_action": "kick",
                    "joinwatch_pending_role_assignments": assignments,
                    "joinwatch_pending_roles": roles,
                }
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value=raw_config),
                    joinwatch_pending_role_assignments=lambda: self._Store(assignments),
                    joinwatch_pending_roles=lambda: self._Store(roles),
                    stats=lambda: self._Store(stats),
                )
                bot = _Bot()
                bot.guilds = [guild]
                bot.owner_ids = ()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(guild=lambda _guild: guild_config)
                cog._record_daily_stat = mock.AsyncMock()

                with mock.patch.object(
                    honeypot.discord,
                    "utils",
                    SimpleNamespace(
                        format_dt=lambda value, style: value.isoformat()
                    ),
                    create=True,
                ), mock.patch.object(
                    honeypot.discord,
                    "Embed",
                    FakeEmbed,
                ), mock.patch.object(
                    honeypot.modlog,
                    "create_case",
                    new=mock.AsyncMock(),
                    create=True,
                ):
                    await cog.joinwatch_auto_role_loop.function(cog)

                self.assertEqual(effects, [("add-role", 201), ("kick", 202)])
                assignment_member.add_roles.assert_awaited_once_with(
                    role,
                    reason="Automated account status update.",
                )
                active_member.kick.assert_awaited_once_with(
                    reason="Suspicious Account"
                )
                self.assertEqual(assignments, {})
                self.assertNotIn("202", roles)
                self.assertEqual(roles["201"]["role_id"], 501)
                self.assertEqual(
                    (
                        stats["joinwatch_auto_roles"],
                        stats["joinwatch_auto_role_punishments"],
                        [
                            call.args[2]
                            for call in cog._record_daily_stat.await_args_list
                        ],
                    ),
                    (1, 1, ["shadowbans"]),
                )
                assignment_message.edit.assert_awaited_once()
                expiration_message.edit.assert_awaited_once()
                self.assertIn(
                    "<@&501> applied until",
                    assignment_message.edit.await_args.kwargs["embed"].fields[0].value,
                )
                self.assertEqual(
                    expiration_message.edit.await_args.kwargs["embed"].fields[0].value,
                    "Kicked",
                )

    async def test_successful_joinwatch_ban_records_daily_ban(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
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
                guild = SimpleNamespace(
                    id=100,
                    me=SimpleNamespace(id=101),
                    ban=mock.AsyncMock(),
                )
                member = SimpleNamespace(id=200)
                settings = honeypot.GuildSettings.from_mapping(
                    {
                        "dry_run": False,
                        "joinwatch_auto_role_action": "ban",
                    }
                )

                with mock.patch.object(
                    honeypot.modlog,
                    "create_case",
                    new=mock.AsyncMock(),
                    create=True,
                ):
                    label, failed = await honeypot.joinwatch._execute_joinwatch_action(
                        cog,
                        guild,
                        member,
                        member.id,
                        settings,
                        reason="Suspicious Account",
                    )

                self.assertIsNotNone(label)
                self.assertIsNone(failed)
                guild.ban.assert_awaited_once()
                cog._record_daily_stat.assert_awaited_once_with(
                    guild,
                    mock.ANY,
                    "joinwatch_bans",
                )

    async def test_delayed_shadowban_records_daily_stat_before_lifetime_counter(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime(2026, 8, 19, 20, tzinfo=timezone.utc)
                role = SimpleNamespace(id=501, mention="<@&501>")
                member = SimpleNamespace(
                    id=200,
                    roles=[],
                    add_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=100,
                    get_role=mock.Mock(return_value=role),
                )
                cog = SimpleNamespace(
                    _get_member_or_fetch=mock.AsyncMock(return_value=member),
                    _is_protected_member=mock.AsyncMock(return_value=False),
                    _punitive_effect_allowed=mock.AsyncMock(return_value=True),
                    _missing_role_assignment_permission=mock.Mock(return_value=None),
                    _increment_stat=mock.AsyncMock(
                        side_effect=RuntimeError("config unavailable")
                    ),
                    _record_daily_stat=mock.AsyncMock(),
                )
                action = honeypot.joinwatch_state.JoinwatchSelectedAction(
                    "apply_role",
                    str(member.id),
                    member.id,
                    role.id,
                    now,
                    {"expires_at": (now + timedelta(minutes=30)).isoformat()},
                )
                settings = honeypot.GuildSettings.from_mapping(
                    {"joinwatch_auto_role_timer_minutes": 30}
                )

                with self.assertRaisesRegex(RuntimeError, "config unavailable"):
                    await honeypot.joinwatch._apply_joinwatch_assignment_actions(
                        cog,
                        guild,
                        settings,
                        (action,),
                        now,
                        joinwatch_channel=None,
                    )

                member.add_roles.assert_awaited_once()
                cog._record_daily_stat.assert_awaited_once_with(
                    guild, mock.ANY, "shadowbans"
                )

    async def test_assignment_settles_before_incident_publication(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime(2026, 8, 19, 20, tzinfo=timezone.utc)
                role = SimpleNamespace(id=501, mention="<@&501>")
                member = SimpleNamespace(
                    id=200,
                    roles=[],
                    add_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=100,
                    get_role=mock.Mock(return_value=role),
                )
                member.guild = guild
                assignments = {
                    str(member.id): {
                        "role_id": role.id,
                        "apply_at": now.isoformat(),
                        "expires_at": (now + timedelta(minutes=30)).isoformat(),
                        "member_id": member.id,
                    }
                }
                roles = {}
                guild_config = SimpleNamespace(
                    joinwatch_pending_role_assignments=lambda: self._Store(
                        assignments
                    ),
                    joinwatch_pending_roles=lambda: self._Store(roles),
                )
                cog = SimpleNamespace(
                    config=SimpleNamespace(guild=mock.Mock(return_value=guild_config)),
                    _get_member_or_fetch=mock.AsyncMock(return_value=member),
                    _is_protected_member=mock.AsyncMock(return_value=False),
                    _punitive_effect_allowed=mock.AsyncMock(return_value=True),
                    _missing_role_assignment_permission=mock.Mock(return_value=None),
                    _increment_stat=mock.AsyncMock(),
                    _record_daily_stat=mock.AsyncMock(),
                )
                action = honeypot.joinwatch_state.JoinwatchSelectedAction(
                    "apply_role",
                    str(member.id),
                    member.id,
                    role.id,
                    now,
                    assignments[str(member.id)],
                )
                settings = honeypot.GuildSettings.from_mapping(
                    {"joinwatch_auto_role_timer_minutes": 30}
                )

                with (
                    mock.patch.object(
                        honeypot.joinwatch.joinwatch_publication,
                        "publish_joinwatch_incident",
                        new=mock.AsyncMock(
                            side_effect=RuntimeError("publication failed")
                        ),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(
                            format_dt=lambda value, style: value.isoformat()
                        ),
                        create=True,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "publication failed",
                    ):
                        await honeypot.joinwatch._apply_joinwatch_assignment_actions(
                            cog,
                            guild,
                            settings,
                            (action,),
                            now,
                            joinwatch_channel=None,
                        )

                self.assertNotIn(str(member.id), assignments)
                self.assertIn(str(member.id), roles)
                member.add_roles.assert_awaited_once()

    async def test_assignment_and_role_retries_are_scheduled_one_minute_later(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store = honeypot.DetectionCaseStore(Path(directory) / "joinwatch.sqlite")
                await asyncio.to_thread(cog._case_store.initialize)
                guild = SimpleNamespace(id=100)
                assignments = {"200": {"retry_count": 0}}
                roles = {"200": {"retry_count": 0}}
                guild_config = SimpleNamespace(
                    joinwatch_pending_role_assignments=lambda: self._Store(assignments),
                    joinwatch_pending_roles=lambda: self._Store(roles),
                )
                cog.config = SimpleNamespace(guild=lambda _guild: guild_config)
                honeypot.joinwatch.joinwatch_publication.publish_joinwatch_incident = mock.AsyncMock()
                now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)

                with mock.patch.object(
                    honeypot.discord,
                    "utils",
                    SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                    create=True,
                ):
                    assignment_result = await honeypot.joinwatch._reschedule_joinwatch_assignment_retry(
                        cog, guild, "200", assignments["200"], now, failure="assignment failed"
                    )
                    role_result = await honeypot.joinwatch._reschedule_joinwatch_role_retry(
                        cog, guild, "200", roles["200"], now, failure="action failed"
                    )

                self.assertTrue(assignment_result)
                self.assertTrue(role_result)
                self.assertEqual(
                    datetime.fromisoformat(assignments["200"]["apply_at"]),
                    now + timedelta(minutes=1),
                )
                self.assertEqual(
                    datetime.fromisoformat(roles["200"]["expires_at"]),
                    now + timedelta(minutes=1),
                )
                failures = await asyncio.to_thread(
                    cog._case_store.list_operational_failures, guild.id
                )
                self.assertEqual(
                    {failure.source for failure in failures},
                    {"joinwatch_role_assignment", "joinwatch_role_action"},
                )

    async def test_fifth_retry_is_the_last_and_a_sixth_is_not_scheduled(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store = honeypot.DetectionCaseStore(Path(directory) / "joinwatch.sqlite")
                await asyncio.to_thread(cog._case_store.initialize)
                guild = SimpleNamespace(id=100)
                assignments = {"200": {"retry_count": 5}}
                guild_config = SimpleNamespace(
                    joinwatch_pending_role_assignments=lambda: self._Store(assignments),
                )
                cog.config = SimpleNamespace(guild=lambda _guild: guild_config)
                honeypot.joinwatch.joinwatch_publication.publish_joinwatch_incident = mock.AsyncMock()

                scheduled = await honeypot.joinwatch._reschedule_joinwatch_assignment_retry(
                    cog,
                    guild,
                    "200",
                    assignments["200"],
                    datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
                    failure="still failing",
                )

                self.assertFalse(scheduled)
                self.assertNotIn("200", assignments)
                failures = await asyncio.to_thread(
                    cog._case_store.list_operational_failures, guild.id
                )
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].source, "joinwatch_role_assignment")
