from __future__ import annotations

import copy
import importlib
import sqlite3
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules
from tests.test_joinwatch_verification import _configured_group_runtime

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


class _Clock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


async def _saved_runtime(honeypot, *, expires_at=NOW - timedelta(days=7)):
    runtime, channels = _configured_group_runtime(honeypot)
    member, cog = runtime.member, runtime.cog
    member.roles.append(runtime.role)
    member.guild.me = SimpleNamespace(id=999)
    member.guild.ban = mock.AsyncMock()
    runtime.raw.update(
        dry_run=False,
        joinwatch_auto_role_enabled=True,
        joinwatch_auto_role_action="ban",
        joinwatch_auto_role_timer_minutes=30,
    )
    cog._missing_action_permission = mock.Mock(return_value=None)
    cog._ban_delete_message_seconds = lambda: 0
    cog._schedule_post_ban_sweep = mock.Mock()
    cog._get_user_or_object = mock.AsyncMock(return_value=SimpleNamespace(id=member.id))
    runtime.owner.preparation = SimpleNamespace(
        get=mock.Mock(return_value=(SimpleNamespace(), SimpleNamespace())),
        request=mock.Mock(),
        close=mock.AsyncMock(),
    )
    await honeypot.joinwatch_state.store_pending_role(
        cog, member, runtime.role.id, expires_at,
        applied_at=NOW - timedelta(days=8),
        incident={
            "incident_id": "saved-incident",
            "member_id": member.id,
            "captcha_enabled": True,
            "verification_state": "active",
            "stage": 1,
            "failures": 1,
        },
    )
    maps = {
        kind: copy.deepcopy(runtime.raw.get(config_key, {}))
        for kind, config_key in honeypot.joinwatch_state._KIND_CONFIG.items()
    }
    assert cog._case_store.replace_from_config(member.guild.id, maps)
    for config_key in honeypot.joinwatch_state._KIND_CONFIG.values():
        runtime.raw[config_key] = {}
    cog._case_store = honeypot.DetectionCaseStore(honeypot.cog_data_path() / "verification.sqlite")
    cog._case_store.initialize()
    return runtime, channels


@contextmanager
def _running(honeypot, runtime):
    publication = importlib.import_module(f"{honeypot.__package__}.joinwatch_publication")
    _Clock.current = NOW
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(runtime.module, "datetime", _Clock))
        stack.enter_context(mock.patch.object(honeypot.joinwatch, "datetime", _Clock))
        stack.enter_context(mock.patch.object(publication, "publish_joinwatch_incident", new=mock.AsyncMock()))
        stack.enter_context(mock.patch.object(honeypot.modlog, "create_case", new=mock.AsyncMock(return_value=None), create=True))
        stack.enter_context(mock.patch.object(honeypot.discord.utils, "format_dt", side_effect=lambda value, **_kw: value.isoformat(), create=True))
        yield


class JoinwatchStartupGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_role_application_starts_a_full_normal_timer_and_keeps_grace(self):
        for minutes in (30, 1440):
            with self.subTest(minutes=minutes), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    runtime, _channels = await _saved_runtime(honeypot)
                    guild = runtime.member.guild
                    await honeypot.joinwatch_state.delete_row(runtime.cog, guild, runtime.member.id, "pending_role")
                    runtime.member.roles.clear()
                    runtime.raw["joinwatch_auto_role_timer_minutes"] = minutes
                    await honeypot.joinwatch_state.store_pending_assignment(
                        runtime.cog, runtime.member, runtime.role.id, NOW - timedelta(days=7),
                        expires_at=NOW - timedelta(days=6),
                        incident={"incident_id": "delayed-incident", "captcha_enabled": True,
                                  "member_id": runtime.member.id, "source": "join"},
                    )
                    with _running(honeypot, runtime):
                        await runtime.owner.restore()
                        _Clock.current = NOW + timedelta(hours=1)
                        await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    saved = await honeypot.joinwatch_state.read_row(runtime.cog, guild, runtime.member.id, "pending_role")
                    expected = max(_Clock.current + timedelta(minutes=minutes), NOW + timedelta(hours=24))
                    self.assertEqual(datetime.fromisoformat(saved["expires_at"]), expected)
                    self.assertIn(runtime.role, runtime.member.roles)
                    guild.ban.assert_not_awaited()
                    await runtime.owner.close()

    async def test_future_deadlines_get_at_least_24_hours_without_shortening_later_ones(self):
        for remaining in (timedelta(hours=1), timedelta(days=3)):
            with self.subTest(remaining=remaining), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    runtime, _channels = await _saved_runtime(honeypot, expires_at=NOW + remaining)
                    with _running(honeypot, runtime):
                        await runtime.owner.restore()
                        await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    saved = await honeypot.joinwatch_state.read_row(
                        runtime.cog, runtime.member.guild, runtime.member.id, "pending_role"
                    )
                    self.assertEqual(datetime.fromisoformat(saved["expires_at"]), NOW + max(remaining, timedelta(hours=24)))
                    runtime.member.guild.ban.assert_not_awaited()
                    await runtime.owner.close()

    async def test_missing_panel_blocks_timers_until_recovery_then_starts_the_24_hours(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime, channels = await _saved_runtime(honeypot)
                self.addAsyncCleanup(runtime.owner.close)
                panel = channels[88].fetch_message.return_value
                channels[88].fetch_message.return_value = SimpleNamespace(components=[])
                with _running(honeypot, runtime):
                    await runtime.owner.restore()
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    runtime.member.guild.ban.assert_not_awaited()
                    runtime.cog._record_operational_failure.assert_awaited_once()
                    _Clock.current = NOW + timedelta(days=3)
                    channels[88].fetch_message.return_value = panel
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    result = await runtime.owner.start(runtime.member, now=_Clock.current)
                    self.assertEqual(result.status, "question")
                    saved = await honeypot.joinwatch_state.read_row(
                        runtime.cog, runtime.member.guild, runtime.member.id, "pending_role"
                    )
                    self.assertEqual(datetime.fromisoformat(saved["expires_at"]), NOW + timedelta(days=4))
                    _Clock.current += timedelta(hours=1)
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    unchanged = await honeypot.joinwatch_state.read_row(
                        runtime.cog, runtime.member.guild, runtime.member.id, "pending_role"
                    )
                    self.assertEqual(unchanged["expires_at"], saved["expires_at"])
                    runtime.member.guild.ban.assert_not_awaited()
                    channels[89].send.assert_awaited_once()

    async def test_failed_deadline_write_keeps_enforcement_blocked_and_retryable(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime, _channels = await _saved_runtime(honeypot)
                self.addAsyncCleanup(runtime.owner.close)
                original = runtime.cog._case_store.upsert

                def fail_grace_write(guild_id, user_id, kind, payload, **kwargs):
                    if payload.get("startup_grace_until"):
                        raise sqlite3.OperationalError("database is locked")
                    return original(guild_id, user_id, kind, payload, **kwargs)

                with _running(honeypot, runtime):
                    with mock.patch.object(runtime.cog._case_store, "upsert", side_effect=fail_grace_write):
                        await runtime.owner.restore()
                        await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    runtime.member.guild.ban.assert_not_awaited()
                    _Clock.current = NOW + timedelta(hours=2)
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    saved = await honeypot.joinwatch_state.read_row(
                        runtime.cog, runtime.member.guild, runtime.member.id, "pending_role"
                    )
                    self.assertEqual(datetime.fromisoformat(saved["expires_at"]), _Clock.current + timedelta(hours=24))
                    runtime.member.guild.ban.assert_not_awaited()

    async def test_normal_enforcement_resumes_after_the_saved_grace_deadline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime, _channels = await _saved_runtime(honeypot)
                self.addAsyncCleanup(runtime.owner.close)
                with _running(honeypot, runtime):
                    await runtime.owner.restore()
                    _Clock.current = NOW + timedelta(hours=23)
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    runtime.member.guild.ban.assert_not_awaited()
                    _Clock.current = NOW + timedelta(hours=24, seconds=1)
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)
                runtime.member.guild.ban.assert_awaited_once()

    async def test_timer_change_cannot_shorten_the_restart_minimum(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime, _channels = await _saved_runtime(honeypot)
                self.addAsyncCleanup(runtime.owner.close)
                _Clock.current = NOW
                with mock.patch.object(runtime.module, "datetime", _Clock):
                    await runtime.owner.restore()
                    await honeypot.joinwatch_state.reschedule_pending_roles(
                        runtime.cog, runtime.member.guild, 30, 1
                    )
                saved = await honeypot.joinwatch_state.read_row(
                    runtime.cog, runtime.member.guild, runtime.member.id, "pending_role"
                )
                self.assertEqual(datetime.fromisoformat(saved["expires_at"]), NOW + timedelta(hours=24))

    async def test_restart_restores_captcha_before_any_overdue_ban(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                runtime, _channels = await _saved_runtime(honeypot)
                self.addAsyncCleanup(runtime.owner.close)
                with _running(honeypot, runtime):
                    await runtime.owner.restore()
                    await honeypot.joinwatch.joinwatch_auto_role_loop(runtime.cog)

                    runtime.member.guild.ban.assert_not_awaited()
                    result = await runtime.owner.start(runtime.member, now=NOW)
                    self.assertEqual(result.status, "question")
                    saved = (await honeypot.joinwatch_state.open_maps(
                        runtime.cog, runtime.member.guild
                    ))["pending_role"][str(runtime.member.id)]
                    self.assertEqual(datetime.fromisoformat(saved["expires_at"]), NOW + timedelta(hours=24))
                    self.assertEqual(saved["applied_at"], (NOW - timedelta(days=8)).isoformat())
                    self.assertEqual((saved["stage"], saved["failures"]), (1, 1))
