from __future__ import annotations

import copy
import importlib
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    _async_noop,
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
)
from tests.test_joinwatch_verification import _runtime


class _Map:
    def __init__(self, values):
        self.values = values

    def __await__(self):
        async def read():
            return copy.deepcopy(self.values)

        return read().__await__()

    async def __aenter__(self):
        return self.values

    async def __aexit__(self, *_args):
        return False

    async def set(self, values):
        self.values.clear()
        self.values.update(values)


class _GuildConfig:
    def __init__(self, values, config):
        self.values = values
        self.config = config

    async def all(self):
        return copy.deepcopy(self.values)

    async def clear_raw(self, key):
        if self.config.fail_clear:
            raise RuntimeError("disk")
        current = self.values.get(key)
        if isinstance(current, dict):
            current.clear()
            return
        self.values.pop(key, None)

    async def get_raw(self, *path, default=None):
        value = self.values
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return copy.deepcopy(default)
            value = value[key]
        return copy.deepcopy(value)

    def __getattr__(self, name):
        current = self.values.get(name)
        if isinstance(current, dict):
            return lambda current=current: _Map(current)
        raise AttributeError(name)


class _Config:
    def __init__(self, guilds):
        self.guilds = guilds
        self.fail_clear = False

    def guild(self, guild):
        return self.guild_from_id(guild.id)

    def guild_from_id(self, guild_id):
        return _GuildConfig(self.guilds[int(guild_id)], self)

    async def all_guilds(self):
        return {
            guild_id: copy.deepcopy(values) for guild_id, values in self.guilds.items()
        }


def _challenge(answer):
    return {
        "target": "circles",
        "count": answer,
        "answer": answer,
        "tiles": [["circles"] * answer],
    }


def _entry(**updates):
    entry = {
        "incident_id": "incident",
        "role_id": 51,
        "stage": 1,
        "failures": 0,
        "challenge": [_challenge(2), _challenge(4)],
        "verification_state": "release_pending",
        "release_pending": True,
        "captcha_enabled": True,
        "expires_at": "2026-10-08T16:00:00+00:00",
        "session_id": "session",
    }
    entry.update(updates)
    return entry


def _maps(pending=None, verified=None, assignments=None):
    return {
        "joinwatch_verified_members": {} if verified is None else verified,
        "joinwatch_pending_roles": {} if pending is None else pending,
        "joinwatch_pending_role_assignments": {} if assignments is None else assignments,
    }


def _cog(honeypot, directory, values, guild_id=100):
    store = honeypot.DetectionCaseStore(Path(directory) / "detection_cases.sqlite")
    store.initialize()
    config = _Config({guild_id: values})
    cog = SimpleNamespace(
        _case_store=store,
        config=config,
        failures=[],
        bot=SimpleNamespace(guilds=[], get_cog=lambda _name: None),
        _punitive_effect_allowed=mock.AsyncMock(return_value=True),
        _missing_role_assignment_permission=mock.Mock(return_value=None),
        _is_protected_member=mock.AsyncMock(return_value=False),
        _get_text_channel_or_thread=mock.Mock(return_value=None),
    )

    async def record(guild_id, source, summary, **_kwargs):
        cog.failures.append((guild_id, source, summary))

    cog._record_operational_failure = record
    return cog


class JoinWatchLiveStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_mismatch_leaves_config_authoritative(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            pending = {"010": {"incident_id": "secret-incident", "role_id": 51, "challenge": _challenge(2)}}
            cog = _cog(honeypot, directory, _maps(pending=pending))
            copied = await honeypot.joinwatch_state.cutover_guild(cog, 100)
            self.assertFalse(copied)
            self.assertIsNone(cog._case_store.cutover_source(100))
            self.assertEqual(cog._case_store.counts(100), {
                "verified": 0, "pending_role": 0, "pending_assignment": 0,
            })
            self.assertIsNone(cog._case_store.live_backup(100))
            self.assertEqual(
                cog.config.guilds[100]["joinwatch_pending_roles"]["010"]["incident_id"],
                "secret-incident",
            )
            self.assertEqual(cog.failures[0][1], "joinwatch_live_cutover")
            self.assertNotIn("secret-incident", cog.failures[0][2])
            self.assertNotIn("010", cog.failures[0][2])

    async def test_empty_config_does_not_replace_sqlite_rows(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            cog = _cog(honeypot, directory, _maps())
            entry = _entry()
            cog._case_store.upsert(100, 20, "pending_role", entry)
            self.assertFalse(cog._case_store.replace_from_config(100, {
                "verified": {}, "pending_role": {}, "pending_assignment": {},
            }))
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            self.assertIsNone(cog._case_store.cutover_source(100))
            with closing(sqlite3.connect(cog._case_store.database_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO joinwatch_cutover (guild_id, source) VALUES (100, 'sqlite')"
                )
            self.assertTrue(await honeypot.joinwatch_state.cutover_guild(cog, 100))
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            self.assertEqual(cog._case_store.cutover_source(100), "sqlite")

    async def test_clear_failure_keeps_the_verified_copy_and_backup(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            entry = _entry()
            cog = _cog(honeypot, directory, _maps(pending={"20": entry}))
            cog.config.fail_clear = True
            self.assertFalse(await honeypot.joinwatch_state.cutover_guild(cog, 100))
            self.assertEqual(cog._case_store.cutover_source(100), "sqlite")
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            self.assertEqual(cog._case_store.live_backup(100)["pending_role"]["20"], entry)
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_roles"]["20"], entry)
            self.assertIn("Config was not cleared", cog.failures[0][2])
            self.assertNotIn("secret", cog.failures[0][2])
            cog.config.fail_clear = False
            self.assertTrue(await honeypot.joinwatch_state.cutover_guild(cog, 100))
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_roles"], {})
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            self.assertEqual(cog._case_store.live_backup(100)["pending_role"]["20"]["stage"], 1)

    async def test_export_and_backup_restore_config_before_clearing_the_marker(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            entry = _entry()
            verified = {"incident_id": "done", "completed_at": "2026-10-01T00:00:00+00:00"}
            assignment = {"incident_id": "later", "role_id": 51, "apply_at": "2026-10-08T18:00:00+00:00"}
            cog = _cog(honeypot, directory, _maps(
                pending={"20": copy.deepcopy(entry)},
                verified={"20": verified},
                assignments={"20": assignment},
            ))
            self.assertTrue(await honeypot.joinwatch_state.cutover_guild(cog, 100))
            advanced = copy.deepcopy(entry)
            advanced["stage"] = 2
            cog._case_store.upsert(100, 20, "pending_role", advanced)
            guild = SimpleNamespace(id=100)
            self.assertTrue(await honeypot.joinwatch_state.restore_live_backup(cog, guild))
            self.assertIsNone(cog._case_store.cutover_source(100))
            restored = cog.config.guilds[100]
            self.assertEqual(restored["joinwatch_pending_roles"]["20"], entry)
            self.assertEqual(restored["joinwatch_verified_members"]["20"], verified)
            self.assertEqual(restored["joinwatch_pending_role_assignments"]["20"], assignment)
            self.assertEqual(cog._case_store.get(100, 20, "pending_role")["stage"], 2)
            self.assertTrue(await honeypot.joinwatch_state.cutover_guild(cog, 100))
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            cog._case_store.upsert(100, 20, "pending_role", advanced)
            await honeypot.joinwatch_state.export_live_state(cog, guild)
            self.assertIsNone(cog._case_store.cutover_source(100))
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_roles"]["20"]["stage"], 2)
            self.assertEqual(cog.config.guilds[100]["joinwatch_verified_members"]["20"], verified)
            self.assertEqual(
                cog.config.guilds[100]["joinwatch_pending_role_assignments"]["20"], assignment
            )

    async def test_backup_expires_and_guild_removal_deletes_it(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            entry = _entry()
            cog = _cog(honeypot, directory, _maps(pending={"20": entry}))
            self.assertTrue(await honeypot.joinwatch_state.cutover_guild(cog, 100))
            with closing(sqlite3.connect(cog._case_store.database_path)) as connection, connection:
                connection.execute("UPDATE joinwatch_live_backup SET written_at = 0")
            await honeypot.joinwatch_state.cutover_live_state(cog)
            self.assertIsNone(cog._case_store.live_backup(100))
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            self.assertTrue(cog._case_store.replace_from_config(100, {
                "verified": {},
                "pending_role": {"20": entry},
                "pending_assignment": {},
            }))
            cog._case_store.delete_user(20)
            self.assertIsNone(cog._case_store.get(100, 20, "pending_role"))
            self.assertEqual(cog._case_store.live_backup(100)["pending_role"]["20"], entry)
            cog._case_store.delete_guild(100)
            self.assertIsNone(cog._case_store.live_backup(100))
            self.assertIsNone(cog._case_store.cutover_source(100))

    async def test_stale_incident_does_not_overwrite(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            entry = _entry()
            cog = _cog(honeypot, directory, _maps())
            with closing(sqlite3.connect(cog._case_store.database_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO joinwatch_cutover (guild_id, source) VALUES (100, 'sqlite')"
                )
            cog._case_store.upsert(100, 20, "pending_role", entry)
            guild = SimpleNamespace(id=100)
            stale = copy.deepcopy(entry)
            stale["stage"] = 0
            stale["incident_id"] = "other"
            written = await honeypot.joinwatch_state.write_row(
                cog, guild, 20, "pending_role", stale,
                expected_incident_id="other", compare=True,
            )
            missing = await honeypot.joinwatch_state.write_row(
                cog, guild, 99, "pending_role", entry,
                expected_incident_id="incident", compare=True,
            )
            self.assertFalse(written)
            self.assertFalse(missing)
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            self.assertIsNone(cog._case_store.get(100, 99, "pending_role"))

    async def test_release_pending_matches_after_restart_without_a_new_challenge(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            entry = _entry(completion_outcome="passed")
            active = _entry(
                incident_id="open", verification_state="active", release_pending=False
            )
            cog = _cog(honeypot, directory, _maps(pending={"20": entry, "21": active}))
            role = SimpleNamespace(id=51)
            members = {}
            guild = SimpleNamespace(
                id=100,
                get_role=lambda value: role if value == 51 else None,
                get_member=members.get,
            )
            for user_id in (20, 21):
                member = SimpleNamespace(
                    id=user_id,
                    guild=guild,
                    roles=[role],
                    bot=False,
                    display_name="Member",
                    display_avatar=None,
                    mention=f"<@{user_id}>",
                    created_at=datetime.now(timezone.utc),
                )
                member.remove_roles = mock.AsyncMock(
                    side_effect=lambda role, member=member, **_kwargs: member.roles.remove(role)
                )
                members[user_id] = member
            cog.bot.guilds = [guild]
            self.assertTrue(await honeypot.joinwatch_state.cutover_guild(cog, 100))
            self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
            self.assertEqual(cog._case_store.get(100, 21, "pending_role"), active)
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_roles"], {})
            module = importlib.import_module(f"{honeypot.__package__}.joinwatch_verification")
            owner = module.JoinwatchVerification(cog)
            try:
                with mock.patch.object(module, "generate_challenge", side_effect=AssertionError("new challenge")):
                    await owner.restore()
                self.assertIsNone(cog._case_store.get(100, 20, "pending_role"))
                self.assertEqual(
                    cog._case_store.get(100, 20, "verified")["incident_id"], "incident"
                )
                members[20].remove_roles.assert_awaited()
                requeued = cog._case_store.get(100, 21, "pending_role")
                self.assertEqual(requeued["challenge"], active["challenge"])
                self.assertEqual(requeued["stage"], 1)
            finally:
                await owner.close()

    async def test_reload_does_not_recopy_live_rows(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            bot = _Bot()
            cog = honeypot.Honeypot(bot, _operational_support())
            entry = _entry()
            await cog.config.guild_from_id(100).set_raw(
                "joinwatch_pending_roles", value={"20": copy.deepcopy(entry)}
            )
            loaded = []

            async def load(target):
                target._init_imagescan_store = _async_noop
                target._run_detection_reconciliation = _async_noop
                target._restore_detection_case_views = _async_noop
                await target.cog_load()
                loaded.append(target)
                await target._joinwatch_restore_task

            try:
                await load(cog)
                self.assertEqual(cog._case_store.cutover_source(100), "sqlite")
                self.assertEqual(cog._case_store.get(100, 20, "pending_role"), entry)
                self.assertEqual(
                    cog._case_store.live_backup(100)["pending_role"]["20"]["stage"], 1
                )
                self.assertNotIn("joinwatch_pending_roles", cog.config._guilds[100])
                advanced = copy.deepcopy(entry)
                advanced["stage"] = 2
                cog._case_store.upsert(100, 20, "pending_role", advanced)
                await cog.config.guild_from_id(100).set_raw(
                    "joinwatch_pending_roles", value={"20": copy.deepcopy(entry)}
                )
                await cog.cog_unload()
                loaded.remove(cog)
                reloaded = honeypot.Honeypot(bot, _operational_support())
                reloaded.config = cog.config
                await load(reloaded)
                self.assertEqual(reloaded._case_store.cutover_source(100), "sqlite")
                self.assertEqual(reloaded._case_store.get(100, 20, "pending_role")["stage"], 2)
                self.assertEqual(
                    reloaded._case_store.live_backup(100)["pending_role"]["20"]["stage"], 1
                )
                self.assertNotIn("joinwatch_pending_roles", cog.config._guilds[100])
            finally:
                for target in loaded:
                    await target.cog_unload()

    async def test_test_pass_is_not_stored_as_verified(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            runtime = _runtime(honeypot)
            entry = runtime.raw["joinwatch_pending_roles"]["20"]
            entry.update(test=True, completion_outcome="passed", verification_state="release_pending")
            try:
                result = await runtime.owner._release_locked(runtime.member, entry)
                self.assertEqual(result.status, "complete")
                self.assertEqual(runtime.raw["joinwatch_verified_members"], {})
                self.assertIsNone(runtime.cog._case_store.get(10, 20, "verified"))
            finally:
                await runtime.owner.close()
