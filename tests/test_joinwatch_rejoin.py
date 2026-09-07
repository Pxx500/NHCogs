from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support


class _Store:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Ranked:
    def __init__(self, position: int):
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position


class _Embed:
    def __init__(self, *, title=None, description=None, color=None, timestamp=None):
        self.title = title
        self.description = description
        self.color = color
        self.timestamp = timestamp
        self.fields = []
        self.author = None
        self.thumbnail = None

    def set_author(self, **kwargs):
        self.author = kwargs

    def set_thumbnail(self, **kwargs):
        self.thumbnail = kwargs

    def add_field(self, *, name, value, inline=False):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


def _make_runtime(honeypot, *, random_delay: bool):
    role = _Ranked(5)
    role.id = 501
    role.mention = "<@&501>"
    partial_message = SimpleNamespace(edit=mock.AsyncMock())
    alert_channel = SimpleNamespace(
        id=601,
        send=mock.AsyncMock(),
        get_partial_message=mock.Mock(return_value=partial_message),
        fetch_message=mock.AsyncMock(return_value=SimpleNamespace(embeds=[])),
    )
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
        get_channel=lambda channel_id: alert_channel if channel_id == alert_channel.id else None,
        get_thread=lambda _channel_id: None,
    )
    member = SimpleNamespace(
        id=200,
        guild=guild,
        bot=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        joined_at=datetime.now(timezone.utc),
        display_name="Rejoiner",
        display_avatar=None,
        mention="<@200>",
        roles=[],
        guild_permissions=SimpleNamespace(manage_guild=False),
        top_role=_Ranked(1),
    )

    async def apply_role(assigned_role, **_kwargs):
        member.roles.append(assigned_role)

    member.add_roles = mock.AsyncMock(side_effect=apply_role)
    guild.get_member = lambda member_id: member if member_id == member.id else None
    pending_assignments = {}
    pending_roles = {}
    stats = {}
    raw_config = {
        "dry_run": False,
        "joinwatch_enabled": True,
        "joinwatch_alert_enabled": True,
        "joinwatch_channel": alert_channel.id,
        "joinwatch_min_age_hours": 24,
        "joinwatch_auto_role_enabled": True,
        "joinwatch_auto_role_id": role.id,
        "joinwatch_auto_role_timer_minutes": 4320,
        "joinwatch_auto_role_random_delay_enabled": random_delay,
        "joinwatch_auto_role_random_delay_min_minutes": 1,
        "joinwatch_auto_role_random_delay_max_minutes": 15,
        "joinwatch_pending_role_assignments": pending_assignments,
        "joinwatch_pending_roles": pending_roles,
    }
    guild_config = SimpleNamespace(
        all=mock.AsyncMock(return_value=raw_config),
        joinwatch_pending_role_assignments=lambda: _Store(pending_assignments),
        joinwatch_pending_roles=lambda: _Store(pending_roles),
        stats=lambda: _Store(stats),
    )
    bot = _Bot()
    bot.owner_ids = ()
    bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
    bot.is_mod = mock.AsyncMock(return_value=False)
    bot.is_admin = mock.AsyncMock(return_value=False)
    cog = honeypot.Honeypot(bot, _operational_support())
    cog.config = SimpleNamespace(guild=lambda _guild: guild_config)
    cog._record_daily_stat = mock.AsyncMock()
    return SimpleNamespace(
        role=role,
        partial_message=partial_message,
        alert_channel=alert_channel,
        guild=guild,
        member=member,
        pending_assignments=pending_assignments,
        pending_roles=pending_roles,
        cog=cog,
    )


class JoinwatchRejoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_shadowban_records_daily_stat_before_lifetime_counter(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)
                original_increment = runtime.cog._increment_stat

                async def fail_role_counter(guild, key, amount=1):
                    if key == "joinwatch_auto_roles":
                        raise RuntimeError("config unavailable")
                    await original_increment(guild, key, amount)

                runtime.cog._increment_stat = mock.AsyncMock(
                    side_effect=fail_role_counter
                )
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "config unavailable"):
                        await runtime.cog.on_member_join(runtime.member)

                runtime.member.add_roles.assert_awaited_once()
                runtime.cog._record_daily_stat.assert_awaited_once_with(
                    runtime.guild, mock.ANY, "shadowbans"
                )

    async def test_rejoin_reuses_alert_and_preserves_original_deadline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)

                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    await runtime.cog.on_member_join(runtime.member)
                    first_deadline = runtime.pending_roles[str(runtime.member.id)]["expires_at"]

                    runtime.member.roles = []
                    runtime.member.joined_at = datetime.now(timezone.utc)
                    await runtime.cog.on_member_join(runtime.member)

                self.assertEqual(runtime.alert_channel.send.await_count, 1)
                self.assertEqual(runtime.member.add_roles.await_count, 2)
                self.assertEqual(len(runtime.pending_roles), 1)
                incident = runtime.pending_roles[str(runtime.member.id)]
                self.assertEqual(incident["expires_at"], first_deadline)
                self.assertEqual(incident["join_count"], 2)
                runtime.alert_channel.get_partial_message.assert_called_once_with(701)
                runtime.partial_message.edit.assert_awaited_once()
                edited_embed = runtime.partial_message.edit.await_args.kwargs["embed"]
                activity_fields = [
                    field for field in edited_embed.fields if field.name == "Join activity:"
                ]
                self.assertEqual(len(activity_fields), 1)
                self.assertIn("2", activity_fields[0].value)

    async def test_rejoin_keeps_original_randomized_assignment_and_deadline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=True)
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                    mock.patch.object(
                        honeypot.joinwatch.random,
                        "randint",
                        side_effect=(5, 10),
                    ) as randint,
                ):
                    await runtime.cog.on_member_join(runtime.member)
                    first_assignment = dict(runtime.pending_assignments[str(runtime.member.id)])

                    runtime.member.joined_at = datetime.now(timezone.utc)
                    await runtime.cog.on_member_join(runtime.member)

                assignment = runtime.pending_assignments[str(runtime.member.id)]
                self.assertEqual(runtime.alert_channel.send.await_count, 1)
                self.assertEqual(randint.call_count, 1)
                self.assertEqual(assignment["apply_at"], first_assignment["apply_at"])
                self.assertIn("expires_at", assignment)
                self.assertEqual(assignment["join_count"], 2)
                self.assertEqual(assignment["alert_message_id"], 701)
                runtime.partial_message.edit.assert_awaited_once()

    async def test_randomized_assignment_preserves_incident_deadline_when_applied(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=True)
                runtime.cog.bot.guilds = [runtime.guild]
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                    mock.patch.object(
                        honeypot.joinwatch.random,
                        "randint",
                        return_value=5,
                    ),
                ):
                    await runtime.cog.on_member_join(runtime.member)

                member_key = str(runtime.member.id)
                original_deadline = runtime.pending_assignments[member_key]["expires_at"]
                runtime.pending_assignments[member_key]["apply_at"] = (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat()

                with (
                    mock.patch.object(
                        honeypot.joinwatch.joinwatch_publication,
                        "publish_joinwatch_incident",
                        mock.AsyncMock(),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)

                self.assertNotIn(member_key, runtime.pending_assignments)
                self.assertIn(member_key, runtime.pending_roles)
                applied = runtime.pending_roles[member_key]
                self.assertEqual(applied["expires_at"], original_deadline)
                self.assertEqual(applied["join_count"], 1)
                self.assertEqual(applied["alert_message_id"], 701)
                self.assertIn(runtime.role, runtime.member.roles)

    async def test_new_incident_status_edit_uses_partial_message_without_fetch(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    await runtime.cog.on_member_join(runtime.member)
                    incident = runtime.pending_roles[str(runtime.member.id)]
                    await honeypot.joinwatch.joinwatch_publication.publish_joinwatch_incident(
                        runtime.cog,
                        runtime.guild,
                        incident,
                        "Banned",
                    )

                runtime.alert_channel.fetch_message.assert_not_awaited()
                runtime.alert_channel.get_partial_message.assert_called_once_with(701)
                runtime.partial_message.edit.assert_awaited_once()
                edited_embed = runtime.partial_message.edit.await_args.kwargs["embed"]
                status_fields = [
                    field for field in edited_embed.fields if field.name == "Auto-role:"
                ]
                self.assertEqual(len(status_fields), 1)
                self.assertEqual(status_fields[0].value, "Banned")
                runtime.cog._record_daily_stat.assert_awaited_once_with(
                    runtime.guild,
                    mock.ANY,
                    "shadowbans",
                )

    async def test_missing_alert_disables_later_edits_without_weakening_role(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)
                runtime.cog._record_operational_failure = mock.AsyncMock()
                runtime.partial_message.edit.side_effect = honeypot.discord.NotFound()
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    await runtime.cog.on_member_join(runtime.member)

                    runtime.member.roles = []
                    runtime.member.joined_at = datetime.now(timezone.utc)
                    await runtime.cog.on_member_join(runtime.member)

                    runtime.member.roles = []
                    runtime.member.joined_at = datetime.now(timezone.utc)
                    await runtime.cog.on_member_join(runtime.member)

                incident = runtime.pending_roles[str(runtime.member.id)]
                self.assertTrue(incident.get("alert_updates_disabled", False))
                self.assertEqual(runtime.partial_message.edit.await_count, 1)
                self.assertEqual(runtime.member.add_roles.await_count, 3)
                self.assertEqual(runtime.alert_channel.send.await_count, 1)
                runtime.cog._record_operational_failure.assert_awaited_once()

    async def test_transient_alert_failure_keeps_later_updates_enabled(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)
                runtime.cog._record_operational_failure = mock.AsyncMock()
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    await runtime.cog.on_member_join(runtime.member)
                    runtime.partial_message.edit.side_effect = (
                        honeypot.discord.HTTPException("temporary failure")
                    )
                    incident = runtime.pending_roles[str(runtime.member.id)]
                    await honeypot.joinwatch.joinwatch_publication.publish_joinwatch_incident(
                        runtime.cog,
                        runtime.guild,
                        incident,
                        "Retrying later.",
                    )

                self.assertFalse(incident.get("alert_updates_disabled", False))
                runtime.cog._record_operational_failure.assert_awaited_once()

    async def test_missing_alert_channel_disables_later_updates(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)
                runtime.cog._record_operational_failure = mock.AsyncMock()
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    await runtime.cog.on_member_join(runtime.member)
                    incident = runtime.pending_roles[str(runtime.member.id)]
                    runtime.guild.get_channel = lambda _channel_id: None
                    runtime.cog.bot.get_channel = mock.Mock(return_value=None)
                    await honeypot.joinwatch.joinwatch_publication.publish_joinwatch_incident(
                        runtime.cog,
                        runtime.guild,
                        incident,
                        "Role manually removed",
                    )

                self.assertTrue(incident.get("alert_updates_disabled", False))
                runtime.partial_message.edit.assert_not_awaited()
                runtime.cog._record_operational_failure.assert_awaited_once()

    async def test_terminal_timer_settles_in_original_alert_without_new_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)
                runtime.cog.bot.guilds = [runtime.guild]
                discord_stubs = (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(
                            orange=mock.Mock(return_value=None),
                            dark_red=mock.Mock(return_value=None),
                        ),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                )
                with discord_stubs[0], discord_stubs[1], discord_stubs[2]:
                    await runtime.cog.on_member_join(runtime.member)
                    member_key = str(runtime.member.id)
                    runtime.pending_roles[member_key]["expires_at"] = (
                        datetime.now(timezone.utc) - timedelta(minutes=1)
                    ).isoformat()

                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)

                self.assertNotIn(member_key, runtime.pending_roles)
                self.assertEqual(runtime.alert_channel.send.await_count, 1)
                runtime.partial_message.edit.assert_awaited_once()
                final_embed = runtime.partial_message.edit.await_args.kwargs["embed"]
                status_fields = [
                    field for field in final_embed.fields if field.name == "Auto-role:"
                ]
                self.assertEqual(len(status_fields), 1)
                self.assertEqual(
                    status_fields[0].value,
                    "Auto-role timer expired",
                )

    async def test_rejoins_after_delayed_role_application_share_one_counter(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=True)
                runtime.cog.bot.guilds = [runtime.guild]
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                    mock.patch.object(
                        honeypot.joinwatch.random,
                        "randint",
                        side_effect=(5, 7, 9),
                    ) as randint,
                ):
                    await runtime.cog.on_member_join(runtime.member)
                    member_key = str(runtime.member.id)
                    original_deadline = runtime.pending_assignments[member_key]["expires_at"]
                    runtime.pending_assignments[member_key]["apply_at"] = (
                        datetime.now(timezone.utc) - timedelta(minutes=1)
                    ).isoformat()

                    with mock.patch.object(
                        honeypot.joinwatch.joinwatch_publication,
                        "publish_joinwatch_incident",
                        mock.AsyncMock(),
                    ):
                        await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)

                    runtime.member.roles = []
                    runtime.member.joined_at = datetime.now(timezone.utc)
                    await runtime.cog.on_member_join(runtime.member)

                    runtime.member.joined_at = datetime.now(timezone.utc)
                    await runtime.cog.on_member_join(runtime.member)

                assignment = runtime.pending_assignments[member_key]
                self.assertEqual(assignment["join_count"], 3)
                self.assertEqual(assignment["expires_at"], original_deadline)
                self.assertEqual(randint.call_count, 2)
                self.assertEqual(runtime.alert_channel.send.await_count, 1)

    async def test_pre_change_pending_role_keeps_its_deadline_on_rejoin(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime = _make_runtime(honeypot, random_delay=False)
                member_key = str(runtime.member.id)
                original_deadline = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
                runtime.pending_roles[member_key] = {
                    "role_id": runtime.role.id,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": original_deadline,
                    "alert_channel_id": runtime.alert_channel.id,
                    "alert_message_id": 701,
                }
                with (
                    mock.patch.object(honeypot.discord, "Embed", _Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(orange=mock.Mock(return_value=None)),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "utils",
                        SimpleNamespace(format_dt=lambda value, style: value.isoformat()),
                        create=True,
                    ),
                ):
                    await runtime.cog.on_member_join(runtime.member)

                upgraded = runtime.pending_roles[member_key]
                self.assertEqual(upgraded["expires_at"], original_deadline)
                self.assertEqual(upgraded["join_count"], 2)
                self.assertEqual(runtime.alert_channel.send.await_count, 0)
                self.assertEqual(runtime.member.add_roles.await_count, 1)
                runtime.partial_message.edit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
