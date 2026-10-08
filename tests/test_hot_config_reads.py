"""Hot listeners read the settings they use and leave JoinWatch member maps alone."""

import asyncio
import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    DetectionPipelineTestCase,
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
)

_MEMBER_MAPS = (
    "joinwatch_pending_role_assignments",
    "joinwatch_pending_roles",
    "joinwatch_verified_members",
)


class _Store:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _RecordingGroup:
    def __init__(self, values):
        self.values = values
        self.reads = []
        self.all_calls = 0

    async def all(self):
        self.all_calls += 1
        return self.values

    async def get_raw(self, *keys, default=None):
        self.reads.append(keys)
        raw = self.values
        for key in keys:
            if not isinstance(raw, dict) or key not in raw:
                return default
            raw = raw[key]
        return raw

    def joinwatch_pending_roles(self):
        return _Store(self.values.setdefault("joinwatch_pending_roles", {}))

    def stats(self):
        return _Store(self.values.setdefault("stats", {}))

    def full_map_reads(self):
        return [read for read in self.reads if len(read) == 1 and read[0] in _MEMBER_MAPS]


def _filled_maps():
    return {
        "joinwatch_pending_role_assignments": {"1": {"role_id": 1}},
        "joinwatch_pending_roles": {"2": {"role_id": 2}},
        "joinwatch_verified_members": {"3": {"passed": True}},
    }


class HotConfigReadTests(DetectionPipelineTestCase):
    async def test_disabled_message_returns_before_detection_settings(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, message, group = self._message_cog(honeypot, {"enabled": False, **_filled_maps()})
                cog._collect_detection_signals = mock.AsyncMock(return_value=())

                await cog.on_message(message)

                cog._collect_detection_signals.assert_not_awaited()
                self.assertEqual(group.reads, [("enabled",)])
                self.assertEqual(group.all_calls, 0)
                self.assertEqual(group.full_map_reads(), [])

    async def test_enabled_message_sees_detection_settings_without_member_maps(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                values = {
                    "enabled": True,
                    "spam_enabled": True,
                    "spam_window_seconds": 7,
                    "honeypot_channels": [400],
                    **_filled_maps(),
                }
                cog, message, group = self._message_cog(honeypot, values)
                seen = {}

                async def collect(_message, guild_settings):
                    seen["settings"] = guild_settings
                    return ()

                cog._collect_detection_signals = collect
                cog._is_protected_member = mock.AsyncMock(return_value=False)

                await cog.on_message(message)

                settings = seen["settings"]
                self.assertTrue(settings.enabled)
                self.assertTrue(settings.spam_enabled)
                self.assertEqual(settings.spam_window_seconds, 7)
                self.assertEqual(settings.honeypot_channels, [400])
                self.assertEqual(settings.joinwatch_verified_members, {})
                self.assertEqual(settings.joinwatch_pending_roles, {})
                self.assertEqual(settings.joinwatch_pending_role_assignments, {})
                self.assertEqual(group.all_calls, 0)
                self.assertEqual(group.full_map_reads(), [])
                self.assertIn(("enabled",), group.reads)
                self.assertIn(("spam_enabled",), group.reads)

    async def test_member_update_without_a_role_change_reads_no_config(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, group, before, after = self._member_update(
                    honeypot,
                    _filled_maps(),
                    before_roles=[SimpleNamespace(id=77)],
                    after_roles=[SimpleNamespace(id=77)],
                )
                after.display_name = "renamed"

                await cog.on_member_update(before, after)

                self.assertEqual(group.reads, [])
                self.assertEqual(group.all_calls, 0)

    async def test_removed_restriction_still_clears_that_member(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                values = _filled_maps()
                values["joinwatch_pending_roles"] = {"200": {"role_id": 77}}
                values["baitrole_enabled"] = False
                cog, group, before, after = self._member_update(
                    honeypot,
                    values,
                    before_roles=[SimpleNamespace(id=77)],
                    after_roles=[],
                )
                await asyncio.to_thread(cog._case_store.initialize)

                await cog.on_member_update(before, after)

                self.assertNotIn("200", group.values["joinwatch_pending_roles"])
                self.assertEqual(group.values["stats"]["joinwatch_auto_roles_cleared"], 1)
                self.assertIn(("joinwatch_pending_roles", "200"), group.reads)
                self.assertEqual(group.full_map_reads(), [])
                self.assertEqual(group.all_calls, 0)
                self.assertEqual(
                    group.values["joinwatch_verified_members"],
                    {"3": {"passed": True}},
                )

    async def test_removed_restriction_and_bait_role_both_run(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bait = SimpleNamespace(id=88)
                values = _filled_maps()
                values["joinwatch_pending_roles"] = {"200": {"role_id": 77}}
                values.update(
                    baitrole_enabled=True,
                    baitrole_id=bait.id,
                    baitrole_action="ban",
                )
                cog, group, before, after = self._member_update(
                    honeypot,
                    values,
                    before_roles=[SimpleNamespace(id=77)],
                    after_roles=[bait],
                )
                after.guild.me = SimpleNamespace(id=1)
                cog.bot.get_guild = lambda guild_id: after.guild
                after.guild.get_role = lambda role_id: bait if role_id == bait.id else None
                effect = honeypot.ModerationEffectResult(
                    "banned",
                    None,
                    honeypot.detection.EffectStatus.SUCCEEDED,
                )
                cog._execute_action = mock.AsyncMock(return_value=effect)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                await asyncio.to_thread(cog._case_store.initialize)

                await cog.on_member_update(before, after)

                self.assertNotIn("200", group.values["joinwatch_pending_roles"])
                cog._execute_action.assert_awaited_once()
                self.assertEqual(cog._execute_action.await_args.kwargs["action"], "ban")
                self.assertEqual(group.full_map_reads(), [])
                self.assertEqual(group.all_calls, 0)

    async def test_wave_poll_reads_critical_keys_without_member_maps(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                group = _RecordingGroup(
                    {"joinwatch_auto_role_id": 77, **_filled_maps()}
                )
                cog = SimpleNamespace(config=SimpleNamespace(guild=lambda _guild: group))
                owner = waves.JoinwatchWaves(cog)
                guild = SimpleNamespace(id=100)

                first = await owner._configuration(guild)
                self.assertEqual(first["joinwatch_auto_role_id"], 77)
                self.assertIs(first["joinwatch_auto_role_enabled"], False)
                self.assertIs(first["dry_run"], False)
                self.assertEqual(
                    [read[0] for read in group.reads],
                    list(waves.CRITICAL_CONFIGURATION),
                )
                self.assertEqual(group.full_map_reads(), [])
                self.assertEqual(group.all_calls, 0)

                group.values["joinwatch_auto_role_id"] = 78
                second = await owner._configuration(guild)

                self.assertNotEqual(first, second)
                self.assertEqual(second["joinwatch_auto_role_id"], 78)
                self.assertEqual(group.all_calls, 0)
                self.assertEqual(group.full_map_reads(), [])

    def _message_cog(self, honeypot, values):
        cog = honeypot.Honeypot(_Bot(), _operational_support())
        cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
        cog._message_registry._initialize_sync()
        group = _RecordingGroup(values)
        cog.config = SimpleNamespace(guild=lambda _guild: group)
        message = self._message(honeypot, attachment_count=0)
        return cog, message, group

    def _member_update(self, honeypot, values, *, before_roles, after_roles):
        cog = honeypot.Honeypot(_Bot(), _operational_support())
        cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
        guild = SimpleNamespace(id=100, get_role=lambda _role_id: None)
        shared = {
            "id": 200,
            "guild": guild,
            "bot": False,
            "mention": "<@200>",
            "display_name": "User",
            "display_avatar": None,
        }
        before = SimpleNamespace(**shared, roles=before_roles)
        after = SimpleNamespace(**shared, roles=after_roles)
        group = _RecordingGroup(values)
        cog.config = SimpleNamespace(guild=lambda _guild: group)
        return cog, group, before, after
