from __future__ import annotations

import asyncio
import copy
import importlib
import json
import sqlite3
import threading
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


def _lock_held(cog, guild_id, member_id) -> bool:
    owners = getattr(cog, "_joinwatch_lock_owners", None) or {}
    return (guild_id, member_id) in owners


def _open_delete_window(store, cog, opened, release, held_box):
    real_delete_user = store.delete_user
    real_delete = store.delete
    armed = {"done": False}
    loop = asyncio.get_running_loop()

    def delete_user(user_id):
        real_delete_user(user_id)
        held_box["held"] = _lock_held(cog, 100, int(user_id))
        loop.call_soon_threadsafe(opened.set)
        if not held_box["held"]:
            release.wait()

    def delete(guild_id, user_id, kind, expected_incident_id=None, compare=False):
        result = real_delete(guild_id, user_id, kind, expected_incident_id, compare)
        if int(user_id) == 30 and not armed["done"]:
            armed["done"] = True
            held_box["held"] = _lock_held(cog, int(guild_id), int(user_id))
            loop.call_soon_threadsafe(opened.set)
            if not held_box["held"]:
                release.wait()
        return result

    store.delete_user = delete_user
    store.delete = delete


async def _stale_privacy_writer(state, cog, guild, gate, payloads):
    await gate["opened"].wait()
    try:
        if gate["held"]["held"]:
            async with state.member_lock(cog, guild.id, 30):
                current = await state.read_row(cog, guild, 30, "pending_role")
                if current is not None:
                    current["stage"] = 4
                    await state.write_row(cog, guild, 30, "pending_role", current)
            return
        await state.write_row(cog, guild, 30, "pending_role", payloads["member"])
        await state.write_row(cog, guild, 20, "pending_role", payloads["other"])
    finally:
        gate["release"].set()


async def _advance_stage(state, cog, guild):
    async with state.member_lock(cog, guild.id, 20):
        row = await state.read_row(cog, guild, 20, "pending_role")
        row["stage"] = 2
        await state.write_row(cog, guild, 20, "pending_role", row)


async def _completion_during_snapshot(honeypot, reader_name, operation_name):
    state = honeypot.joinwatch_state
    entry = _entry(stage=1)
    with TemporaryDirectory() as directory:
        cog = _cog(honeypot, directory, _maps(pending={"20": entry}))
        assert await state.cutover_guild(cog, 100)
        guild = SimpleNamespace(id=100)
        snapshot = asyncio.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()
        store = cog._case_store
        original = getattr(store, reader_name)

        def read_snapshot(*args, **kwargs):
            maps = original(*args, **kwargs)
            lock = getattr(cog, "_joinwatch_live_source_lock", None)
            held = lock is not None and lock.locked()
            loop.call_soon_threadsafe(snapshot.set)
            if not held:
                finished.wait()
            return maps

        async def complete():
            await snapshot.wait()
            fresh = await state.read_row(cog, guild, 20, "pending_role")
            fresh["stage"] = 2
            fresh["verification_state"] = "release_pending"
            await state.write_row(cog, guild, 20, "pending_role", fresh)
            finished.set()

        setattr(store, reader_name, read_snapshot)
        try:
            await asyncio.gather(getattr(state, operation_name)(cog, guild), complete())
        finally:
            setattr(store, reader_name, original)
        assert store.cutover_source(100) is None
        assert await state.cutover_guild(cog, 100)
        return store.get(100, 20, "pending_role")


class JoinWatchLiveRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reschedule_keeps_a_fresher_row_and_skips_a_deleted_one(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            kept = _entry(applied_at="2026-10-08T15:00:00+00:00", stage=1)
            removed = _entry(
                applied_at="2026-10-08T15:00:00+00:00",
                incident_id="removed",
                stage=1,
            )
            cog = _cog(honeypot, directory, _maps(pending={"20": kept, "21": removed}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            guild = SimpleNamespace(id=100)
            snapshot = asyncio.Event()
            mutated = asyncio.Event()
            original = state.open_maps

            async def open_maps(target, target_guild):
                result = await original(target, target_guild)
                if not snapshot.is_set():
                    snapshot.set()
                    await mutated.wait()
                return result

            async def interfere():
                await snapshot.wait()
                fresh = cog._case_store.get(100, 20, "pending_role")
                fresh["stage"] = 2
                fresh["challenge"] = _challenge(9)
                cog._case_store.upsert(100, 20, "pending_role", fresh)
                cog._case_store.delete(100, 21, "pending_role")
                mutated.set()

            with mock.patch.object(state, "open_maps", open_maps):
                updates = await asyncio.gather(
                    state.reschedule_pending_roles(cog, guild, 60, 30),
                    interfere(),
                )
            saved = cog._case_store.get(100, 20, "pending_role")
            self.assertEqual(saved["stage"], 2)
            self.assertEqual(saved["challenge"], _challenge(9))
            self.assertEqual(saved["applied_at"], "2026-10-08T15:00:00+00:00")
            self.assertEqual(saved["expires_at"], "2026-10-08T15:30:00+00:00")
            self.assertIsNone(cog._case_store.get(100, 21, "pending_role"))
            published = updates[0]
            self.assertEqual([item[0]["incident_id"] for item in published], ["incident"])
            self.assertEqual(published[0][0]["stage"], 2)

    async def test_alert_updates_do_not_overwrite_a_concurrent_stage(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            entry = _entry(stage=1)
            cog = _cog(honeypot, directory, _maps(pending={"20": entry}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            guild = SimpleNamespace(id=100)
            original = state.read_row

            async def advance(stage, phase):
                await phase["started"].wait()
                try:
                    async with state.member_lock(cog, guild.id, 20):
                        row = await state.read_row(cog, guild, 20, "pending_role")
                        row["stage"] = stage
                        await state.write_row(cog, guild, 20, "pending_role", row)
                finally:
                    phase["release"].set()

            async def run(operation, stage):
                phase = {
                    "arm": True,
                    "paused": False,
                    "started": asyncio.Event(),
                    "release": asyncio.Event(),
                }

                async def read_row(target, target_guild, user_id, kind):
                    row = await original(target, target_guild, user_id, kind)
                    if (
                        phase["arm"]
                        and user_id == 20
                        and row is not None
                        and not phase["paused"]
                    ):
                        phase["paused"] = True
                        phase["started"].set()
                        if not _lock_held(target, target_guild.id, user_id):
                            await phase["release"].wait()
                    return row

                with mock.patch.object(state, "read_row", read_row):
                    await asyncio.gather(operation(), advance(stage, phase))

            await run(
                lambda: state.store_alert_reference(cog, guild, 20, 7, 8),
                2,
            )
            saved = cog._case_store.get(100, 20, "pending_role")
            self.assertEqual(saved["stage"], 2)
            self.assertEqual(saved["alert_channel_id"], 7)
            self.assertEqual(saved["alert_message_id"], 8)
            incident = {"stage": 0}
            await run(
                lambda: state.disable_alert_updates(cog, guild, 20, incident=incident),
                3,
            )
            saved = cog._case_store.get(100, 20, "pending_role")
            self.assertEqual(saved["stage"], 3)
            self.assertTrue(saved["alert_updates_disabled"])
            self.assertEqual(saved["alert_channel_id"], 7)
            self.assertTrue(incident["alert_updates_disabled"])

    async def test_privacy_delete_does_not_restore_a_stale_write(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            kept = _entry(incident_id="kept", stage=1)
            kept.update(enrollment_moderator=30, completion_moderator=30, completion_reason="noted")
            removed = _entry(incident_id="removed", stage=1)
            cog = _cog(honeypot, directory, _maps(pending={"20": kept, "30": removed}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            guild = SimpleNamespace(id=100)
            cog.bot.guilds = [guild]
            stale_member = copy.deepcopy(removed)
            stale_member["stage"] = 4
            stale_other = copy.deepcopy(kept)
            stale_other["stage"] = 2
            gate = {
                "opened": asyncio.Event(),
                "release": threading.Event(),
                "held": {"held": False},
            }
            _open_delete_window(cog._case_store, cog, gate["opened"], gate["release"], gate["held"])
            module = importlib.import_module(f"{honeypot.__package__}.joinwatch_verification")
            owner = module.JoinwatchVerification(cog)
            try:
                await asyncio.gather(
                    owner.delete_user_data(30),
                    _stale_privacy_writer(
                        state, cog, guild, gate, {"member": stale_member, "other": stale_other}
                    ),
                    _advance_stage(state, cog, guild),
                )
            finally:
                await owner.close()
            store = cog._case_store
            self.assertIsNone(store.get(100, 30, "pending_role"))
            self.assertIsNone(store.get(100, 30, "verified"))
            survivor = store.get(100, 20, "pending_role")
            self.assertEqual(survivor["stage"], 2)
            self.assertIsNone(survivor["enrollment_moderator"])
            self.assertIsNone(survivor["completion_moderator"])
            self.assertIsNone(survivor["completion_reason"])
            self.assertEqual(survivor["incident_id"], "kept")
            backup = store.live_backup(100)
            self.assertEqual(backup["pending_role"]["30"]["incident_id"], "removed")
            self.assertEqual(backup["pending_role"]["20"]["enrollment_moderator"], 30)

    async def test_export_and_restore_keep_a_completion_during_the_snapshot(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            for reader_name, operation_name in (
                ("config_maps", "export_live_state"),
                ("live_backup", "restore_live_backup"),
            ):
                with self.subTest(operation=operation_name):
                    saved = await _completion_during_snapshot(honeypot, reader_name, operation_name)
                    self.assertEqual(saved["stage"], 2)
                    self.assertEqual(saved["verification_state"], "release_pending")

    def test_data_statement_mentions_the_live_backup(self):
        info = json.loads(
            (Path(__file__).parents[1] / "NHCogs" / "honeypot" / "info.json").read_text()
        )
        statement = info["end_user_data_statement"]
        self.assertIn("joinwatch_live_backup", statement)
        self.assertIn("one-time copy of the three JoinWatch maps", statement)
        self.assertIn("active challenge", statement)
        self.assertIn("7 days", statement)
        self.assertIn("not rewritten on user deletion", statement)
        self.assertIn("leaves the guild", statement)


def _source_held(cog) -> bool:
    lock = getattr(cog, "_joinwatch_live_source_lock", None)
    return lock is not None and lock.locked()


def _arm_config_clear(cog, opened, release, *, pause_at, before):
    original = cog.config.guild_from_id
    state = {"count": 0}

    def guild_from_id(guild_id):
        guild_config = original(guild_id)
        real_clear = guild_config.clear_raw

        async def clear_raw(key):
            state["count"] += 1
            hit = state["count"] == pause_at
            if hit and before:
                opened.set()
                if not _source_held(cog):
                    await release.wait()
            await real_clear(key)
            if hit and not before:
                opened.set()
                if not _source_held(cog):
                    await release.wait()

        guild_config.clear_raw = clear_raw
        return guild_config

    cog.config.guild_from_id = guild_from_id


def _arm_guild_list(config, opened, release):
    original = config.all_guilds
    armed = {"done": False}

    async def all_guilds():
        snapshot = await original()
        if not armed["done"]:
            armed["done"] = True
            opened.set()
            await release.wait()
        return snapshot

    config.all_guilds = all_guilds


def _arm_unloaded_listing(store, loop, started, advanced):
    real_list = store.list_open
    real_history = store.delete_verification_history
    listed = {"done": False}

    def list_open(guild_id):
        rows = real_list(guild_id)
        if int(guild_id) == 200 and not listed["done"]:
            listed["done"] = True
            loop.call_soon_threadsafe(started.set)
            advanced.wait()
        return rows

    def delete_verification_history(*args, **kwargs):
        result = real_history(*args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        return result

    store.list_open = list_open
    store.delete_verification_history = delete_verification_history


async def _write_joined_member(state, cog, guild, opened, release):
    await opened.wait()
    await state.write_row(cog, guild, 21, "pending_role", _entry(incident_id="joined"))
    release.set()


async def _export_when_open(state, cog, guild, opened, release):
    await opened.wait()
    copied = await state.export_live_state(cog, guild)
    release.set()
    return copied


async def _advance_unloaded_stage(state, cog, started, advanced):
    await started.wait()
    guild = SimpleNamespace(id=200)
    async with state.member_lock(cog, 200, 20):
        row = await state.read_row(cog, guild, 20, "pending_role")
        row["stage"] = 2
        await state.write_row(cog, guild, 20, "pending_role", row)
    advanced.set()


class JoinWatchMigrationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cutover_keeps_a_join_written_after_the_guild_list(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            entry = _entry()
            cog = _cog(honeypot, directory, _maps(pending={"20": entry}))
            guild = SimpleNamespace(id=100)
            opened = asyncio.Event()
            release = asyncio.Event()
            _arm_guild_list(cog.config, opened, release)
            await asyncio.gather(
                state.cutover_live_state(cog),
                _write_joined_member(state, cog, guild, opened, release),
            )
            store = cog._case_store
            self.assertEqual(store.get(100, 20, "pending_role")["incident_id"], "incident")
            joined = store.get(100, 21, "pending_role")
            self.assertIsNotNone(joined)
            self.assertEqual(joined["incident_id"], "joined")
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_roles"], {})
    async def test_sqlite_cutover_clear_keeps_an_in_flight_export(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            entry = _entry()
            cog = _cog(honeypot, directory, _maps(pending={"20": entry}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            guild = SimpleNamespace(id=100)
            opened = asyncio.Event()
            release = asyncio.Event()
            _arm_config_clear(cog, opened, release, pause_at=1, before=True)
            await asyncio.gather(
                state.cutover_guild(cog, 100),
                _export_when_open(state, cog, guild, opened, release),
            )
            saved = await state.read_row(cog, guild, 20, "pending_role")
            self.assertIsNotNone(saved)
            self.assertEqual(saved["incident_id"], "incident")
    async def test_second_export_does_not_restore_a_deleted_member(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            cog = _cog(honeypot, directory, _maps(pending={"20": _entry()}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            guild = SimpleNamespace(id=100)
            await state.export_live_state(cog, guild)
            self.assertIsNone(cog._case_store.cutover_source(100))
            deleted = asyncio.Event()

            async def remove():
                await state.delete_row(cog, guild, 20, "pending_role")
                deleted.set()

            async def export_again():
                await deleted.wait()
                return await state.export_live_state(cog, guild)

            _removed, copied = await asyncio.gather(remove(), export_again())
            self.assertFalse(copied)
            self.assertIsNone(await state.read_row(cog, guild, 20, "pending_role"))
    async def test_export_after_restore_leaves_config_unchanged(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            entry = _entry()
            cog = _cog(honeypot, directory, _maps(pending={"20": entry}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            advanced = copy.deepcopy(entry)
            advanced["stage"] = 2
            cog._case_store.upsert(100, 20, "pending_role", advanced)
            guild = SimpleNamespace(id=100)
            restored = asyncio.Event()

            async def restore():
                copied = await state.restore_live_backup(cog, guild)
                restored.set()
                return copied

            async def export_after():
                await restored.wait()
                return await state.export_live_state(cog, guild)

            restored_ok, exported = await asyncio.gather(restore(), export_after())
            self.assertTrue(restored_ok)
            self.assertFalse(exported)
            pending = cog.config.guilds[100]["joinwatch_pending_roles"]["20"]
            self.assertEqual(pending["stage"], 1)
            pending["stage"] = 3
            self.assertFalse(await state.restore_live_backup(cog, guild))
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_roles"]["20"]["stage"], 3)
    async def test_export_command_says_when_config_is_already_live(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            cog = _cog(honeypot, directory, _maps(pending={"20": _entry()}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            guild = SimpleNamespace(id=100)
            sent = []

            async def send(message):
                sent.append(message)

            async def is_owner(_author):
                return True

            cog.bot.is_owner = is_owner
            ctx = SimpleNamespace(author=SimpleNamespace(), guild=guild, send=send)
            command = honeypot.Honeypot.debug_export_joinwatch
            callback = getattr(command, "callback", command)
            await callback(cog, ctx)
            await state.delete_row(cog, guild, 20, "pending_role")
            await callback(cog, ctx)
            self.assertEqual(
                sent,
                [
                    "JoinWatch live state is in Config again",
                    "JoinWatch live state is already in Config",
                ],
            )
            self.assertIsNone(await state.read_row(cog, guild, 20, "pending_role"))
    async def test_guild_removal_does_not_restore_an_export(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            state = honeypot.joinwatch_state
            cog = _cog(honeypot, directory, _maps(pending={"20": _entry()}))
            self.assertTrue(await state.cutover_guild(cog, 100))
            guild = SimpleNamespace(id=100)
            opened = asyncio.Event()
            release = asyncio.Event()
            _arm_config_clear(cog, opened, release, pause_at=3, before=False)
            module = importlib.import_module(f"{honeypot.__package__}.joinwatch_verification")
            owner = module.JoinwatchVerification(cog)
            try:
                await asyncio.gather(
                    owner.delete_guild_data(guild),
                    _export_when_open(state, cog, guild, opened, release),
                )
            finally:
                await owner.close()
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_roles"], {})
            self.assertEqual(cog.config.guilds[100]["joinwatch_verified_members"], {})
            self.assertEqual(cog.config.guilds[100]["joinwatch_pending_role_assignments"], {})
            self.assertIsNone(cog._case_store.get(100, 20, "pending_role"))
            self.assertIsNone(cog._case_store.cutover_source(100))
