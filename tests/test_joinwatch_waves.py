"""Frozen historical waves delegate all punishment effects to JoinWatch."""

import asyncio
import copy
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.harness import _isolated_honeypot_modules


class _Value:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self

    def __await__(self):
        async def read():
            return copy.deepcopy(self.value)
        return read().__await__()

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False

    async def set(self, value):
        self.value = copy.deepcopy(value)


class _Lifecycle:
    def __init__(self):
        self.entries = {}
        self.prepared = set()
        self.releases = []

    async def check_configuration(self, guild):
        return None

    async def cancel_wave_preparation(self, guild, wave_id):
        # This fake prepares and activates immediately, so it has no abandoned queue.
        return 0

    async def eligibility(self, member):
        return SimpleNamespace(status="active" if member.id in self.entries else "eligible")

    async def eligibility_many(self, members):
        return {member.id: await self.eligibility(member) for member in members}

    async def prepare_enrollment(self, member, **kwargs):
        self.prepared.add(member.id)
        return SimpleNamespace(status="ready", incident_id=str(member.id))

    async def enroll_prepared(self, member, **kwargs):
        if member.id not in self.prepared:
            raise AssertionError("Restrictions must wait for both questions")
        self.entries[member.id] = {"incident_id": str(member.id), "wave_id": kwargs["wave_id"],
                                   "source": "wave", "ready": True, "role_id": 77}
        member.roles.append(SimpleNamespace(id=77))
        return SimpleNamespace(status="enrolled", incident_id=str(member.id))

    async def inspect(self, member):
        return self.entries.get(member.id)

    async def inspect_id(self, guild, user_id):
        return self.entries.get(user_id)

    async def release_id(self, guild, user_id, **kwargs):
        return await self.release(SimpleNamespace(id=user_id), **kwargs)

    async def release(self, member, *, incident_id, outcome):
        self.releases.append((member.id, incident_id, outcome))
        self.entries.pop(member.id, None)
        return SimpleNamespace(status="released")


class _AuxiliaryStore:
    def __init__(self, history):
        self.history = copy.deepcopy(history)
        self.waves = {}

    def get_joinwatch_history(self, guild_id):
        return copy.deepcopy(self.history)

    def save_joinwatch_history(self, guild_id, history):
        self.history = copy.deepcopy(history)

    def get_joinwatch_waves(self, guild_id):
        return copy.deepcopy(self.waves)

    def save_joinwatch_wave(self, guild_id, record):
        self.waves[record["id"]] = copy.deepcopy(record)

    def delete_joinwatch_wave(self, guild_id, wave_id):
        self.waves.pop(wave_id, None)

    def clear_joinwatch_auxiliary(self, guild_id):
        self.history = {"version": 1, "revision": 0, "import_revision": 0,
                        "sources": [], "observations": {}}
        self.waves.clear()


def _fixture(groups, count=7):
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    created = now - timedelta(days=60)
    base = (int(created.timestamp() * 1000) - 1420070400000) << 22
    ids = [base + offset for offset in range(count)]
    history = groups.empty_history()
    history["sources"] = [{"source": "test archive", "complete": True}]
    history["observations"] = {str(uid): {"first_joined_at": (now - timedelta(days=1) + timedelta(minutes=i)).isoformat(),
                                         "imported": True} for i, uid in enumerate(ids)}
    sent = []

    async def send(content, **kwargs):
        sent.append((content, kwargs))
        return SimpleNamespace(id=999)

    cfg = SimpleNamespace(joinwatch_pending_roles=_Value({}))
    configuration = {"joinwatch_auto_role_id": 77, "captcha_channel": 88,
                     "captcha_panel_message_id": 99, "captcha_panel_channel_id": 88}

    async def all_config():
        return configuration

    cfg.all = all_config
    guild = SimpleNamespace(id=123)
    members = {uid: SimpleNamespace(id=uid, guild=guild, bot=False, roles=[]) for uid in ids}
    guild.get_member = members.get
    guild.get_channel = lambda _channel_id: SimpleNamespace(send=send)
    lifecycle = _Lifecycle()
    cog = SimpleNamespace(config=SimpleNamespace(guild=lambda _: cfg),
                          _joinwatch_verification=lifecycle, _case_store=_AuxiliaryStore(history))
    return cog, guild, cfg, lifecycle, ids, now, sent, configuration


def _real_lifecycle_fixture(honeypot, groups, directory, *, count=3):
    verification = importlib.import_module("NHCogs.honeypot.joinwatch_verification")
    cog, guild, cfg, _, ids, now, sent, configuration = _fixture(groups, count=count)
    cfg.joinwatch_pending_role_assignments = _Value({})
    cfg.joinwatch_verified_members = _Value({})
    configuration["joinwatch_auto_role_timer_minutes"] = 60

    async def all_config():
        return {**configuration, "joinwatch_pending_roles": cfg.joinwatch_pending_roles.value,
                "joinwatch_pending_role_assignments": cfg.joinwatch_pending_role_assignments.value,
                "joinwatch_verified_members": cfg.joinwatch_verified_members.value}

    async def get_raw(*keys, default=None):
        raw = await all_config()
        for key in keys:
            if not isinstance(raw, dict) or key not in raw:
                return copy.deepcopy(default)
            raw = raw[key]
        return copy.deepcopy(raw)

    cfg.all = all_config
    cfg.get_raw = get_raw
    role = SimpleNamespace(id=77)
    guild.get_role = lambda value: role if value == 77 else None
    members = {uid: guild.get_member(uid) for uid in ids}
    for member in members.values():
        member.add_roles = AsyncMock(side_effect=lambda role, member=member, **_kwargs: member.roles.append(role))
        member.remove_roles = AsyncMock(side_effect=lambda role, member=member, **_kwargs: member.roles.remove(role))
    cog.bot = SimpleNamespace(guilds=[guild])
    cog._is_protected_member = AsyncMock(return_value=False)
    cog._punitive_effect_allowed = AsyncMock(return_value=True)
    cog._missing_role_assignment_permission = Mock(return_value=None)
    cog._get_text_channel_or_thread = Mock(return_value=None)
    cog._get_member_or_fetch = AsyncMock(side_effect=lambda guild, uid: guild.get_member(uid))
    history = cog._case_store.get_joinwatch_history(guild.id)
    cog._case_store = honeypot.DetectionCaseStore(Path(directory) / "wave.sqlite")
    cog._case_store.initialize()
    cog._case_store.save_joinwatch_history(guild.id, history)
    lifecycle = verification.JoinwatchVerification(cog)
    # Fake only the Discord readiness check. Enrollment and release use the real owner.
    lifecycle.check_configuration = AsyncMock()
    cog._joinwatch_verification = lifecycle
    return cog, guild, cfg, lifecycle, ids, now, sent, members, role


class JoinwatchWaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_failure_keeps_batch_cooldown_after_resume(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
            waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
            cog, guild, cfg, lifecycle, _, now, sent, _, _ = _real_lifecycle_fixture(
                honeypot, groups, directory, count=10
            )
            owner = waves.JoinwatchWaves(cog)
            channel = guild.get_channel(88)
            send = channel.send
            channel.send = AsyncMock(side_effect=ConnectionError("Invitation outcome unknown"))
            guild.get_channel = lambda _: channel
            try:
                record = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                await owner.confirm(guild, record["id"], 42, True, now=now)
                await owner.tick(guild, now=now)
                await lifecycle.preparation.wait()
                with self.assertRaises(ConnectionError):
                    await owner.tick(guild, now=now + timedelta(seconds=5))
                self.assertEqual(len(await cfg.joinwatch_pending_roles()), 5)
                await owner.resume(guild, record["id"], 42, True)
                channel.send = send
                await owner.tick(guild, now=now + timedelta(seconds=10))
                await lifecycle.preparation.wait()
                await owner.tick(guild, now=now + timedelta(seconds=14))
                self.assertEqual(len(await cfg.joinwatch_pending_roles()), 5)
                self.assertEqual(sent, [])
                await owner.tick(guild, now=now + timedelta(seconds=20))
                self.assertEqual(len(await cfg.joinwatch_pending_roles()), 10)
                self.assertEqual(len(sent), 1)
            finally:
                await lifecycle.close()

    async def test_real_preparation_does_not_add_a_second_batch_cooldown(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
            waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
            cog, guild, cfg, lifecycle, ids, now, sent, _, _ = _real_lifecycle_fixture(
                honeypot, groups, directory, count=12
            )
            owner = waves.JoinwatchWaves(cog)
            deliveries = []
            try:
                record = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                await owner.confirm(guild, record["id"], 42, True, now=now)
                for second in range(0, 36, 5):
                    before = len(sent)
                    await owner.tick(guild, now=now + timedelta(seconds=second))
                    await lifecycle.preparation.wait()
                    if len(sent) > before:
                        deliveries.append(second)
                    expected = 0 if second < 5 else 5 if second < 20 else 10 if second < 35 else 12
                    self.assertEqual(len(await cfg.joinwatch_pending_roles()), expected, second)
                self.assertEqual(deliveries, [5, 20, 35])
                self.assertEqual(
                    [user.id for _, kwargs in sent for user in kwargs["allowed_mentions"].users], ids
                )
                self.assertTrue(all(kwargs["delete_after"] == 25 for _, kwargs in sent))
                self.assertEqual((await owner.status(guild, record["id"]))["status"], "completed")
            finally:
                await lifecycle.close()

    async def test_interrupted_wave_role_application_is_not_an_active_timer_or_auto_punishment(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
            waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
            cog, guild, cfg, lifecycle, ids, now, _, members, role = _real_lifecycle_fixture(honeypot, groups, directory)
            configuration = await cfg.all()
            configuration.update(joinwatch_auto_role_action="ban", joinwatch_auto_role_enabled=True)
            cfg.all = AsyncMock(side_effect=lambda: {
                **configuration, "joinwatch_pending_roles": cfg.joinwatch_pending_roles.value,
                "joinwatch_pending_role_assignments": cfg.joinwatch_pending_role_assignments.value,
            })
            entered = asyncio.Event()
            reply = asyncio.Event()
            member = members[ids[0]]

            async def lost_role_response(added_role, **kwargs):
                member.roles.append(added_role)
                entered.set()
                await reply.wait()
                raise honeypot.discord.HTTPException("role response unavailable")

            member.add_roles = AsyncMock(side_effect=lost_role_response)
            guild.ban = AsyncMock()
            cog._record_operational_failure = AsyncMock()
            tick = None
            try:
                owner = waves.JoinwatchWaves(cog)
                preview = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                await owner.confirm(guild, preview["id"], 42, True, now=now)
                await owner.tick(guild, now=now)
                await lifecycle.preparation.wait()
                tick = asyncio.create_task(owner.tick(guild, now=now + timedelta(seconds=15)))
                await asyncio.wait_for(entered.wait(), timeout=5)
                self.assertIsNone(await lifecycle.inspect(member))
                reply.set()
                await asyncio.wait_for(tick, timeout=5)
                self.assertEqual((await lifecycle.start(member)).status, "unavailable")
                await lifecycle.close()
                lifecycle = importlib.import_module("NHCogs.honeypot.joinwatch_verification").JoinwatchVerification(cog)
                cog._joinwatch_verification = lifecycle
                await lifecycle.restore()
                future = datetime.now(timezone.utc) + timedelta(hours=2)
                joinwatch = importlib.import_module("NHCogs.honeypot.joinwatch")
                with patch.object(joinwatch, "datetime", SimpleNamespace(now=lambda tz: future)):
                    await joinwatch.joinwatch_auto_role_loop(cog)
                self.assertIsNone(await lifecycle.inspect(member))
                guild.ban.assert_not_awaited()
                self.assertEqual(cog._case_store.get_daily_stats(guild.id, datetime.now(timezone.utc).date()).wave_guests, 0)
            finally:
                reply.set()
                if tick is not None:
                    await asyncio.wait_for(tick, timeout=5)
                await lifecycle.close()

    async def test_rollback_allows_same_candidates_in_new_wave(self):
        for completed in (False, True):
            with self.subTest(completed=completed), TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, cfg, lifecycle, ids, now, _, _, _ = _real_lifecycle_fixture(honeypot, groups, directory)
                try:
                    owner = waves.JoinwatchWaves(cog)
                    criteria = groups.GroupCriteria(3, 15, 6)
                    old = await owner.preview(guild, criteria, 42, now=now)
                    await owner.confirm(guild, old["id"], 42, True, now=now)
                    await owner.tick(guild, now=now)
                    if completed:
                        await lifecycle.preparation.wait()
                        await owner.tick(guild, now=now + timedelta(seconds=15))
                        self.assertEqual((await owner.status(guild, old["id"]))["status"], "completed")
                        self.assertEqual(set(await cfg.joinwatch_pending_roles()), {str(uid) for uid in ids})
                        report_dates = {datetime.fromisoformat(entry["effect_at"]).date()
                                        for entry in (await cfg.joinwatch_pending_roles()).values()}
                        self.assertEqual(sum(cog._case_store.get_daily_stats(guild.id, day).wave_guests
                                             for day in report_dates), len(ids))
                    else:
                        self.assertEqual(await cfg.joinwatch_pending_roles(), {})
                    rollback = await owner.rollback_preview(guild, old["id"], 42, True, now=now)
                    await owner.rollback(guild, old["id"], 42, True, rollback["confirmation_token"], now=now)
                    self.assertEqual(await cfg.joinwatch_pending_roles(), {})
                    self.assertEqual(await cfg.joinwatch_verified_members(), {})
                    if completed:
                        self.assertEqual(sum(cog._case_store.get_daily_stats(guild.id, day).wave_guests
                                             for day in report_dates), 0)
                    new = await owner.preview(guild, criteria, 42, now=now)
                    await owner.confirm(guild, new["id"], 42, True, now=now)
                    await owner.tick(guild, now=now)
                    await lifecycle.preparation.wait()
                    await owner.tick(guild, now=now + timedelta(seconds=15))
                    active = await cfg.joinwatch_pending_roles()
                    self.assertEqual(set(active), {str(uid) for uid in ids})
                    self.assertTrue(all(entry["wave_id"] == new["id"] for entry in active.values()))
                finally:
                    await lifecycle.close()

    async def test_live_criteria_preview_without_history_changes_only_confirmed_future_settings(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, cfg, lifecycle, _, now, sent, _ = _fixture(groups)
                cog._case_store.save_joinwatch_history(guild.id, groups.empty_history())
                cfg.joinwatch_groups_minimum_accounts = _Value(5)
                cfg.joinwatch_groups_join_window_minutes = _Value(15)
                cfg.joinwatch_groups_creation_distance_hours = _Value(6)
                owner = groups.JoinwatchGroups(cog)
                cog._joinwatch_waves = waves.JoinwatchWaves(cog)
                preview = await owner.criteria_preview(guild, groups.GroupCriteria(3, 30, 24), 42, now=now)
                self.assertEqual(preview["new"], 0)
                self.assertFalse(preview["complete"])
                self.assertEqual((await owner.criteria(guild)).minimum_accounts, 5)
                with self.assertRaises(PermissionError):
                    await owner.confirm_criteria(guild, preview, 42, False, now=now)
                result = await owner.confirm_criteria(guild, preview, 42, True, now=now)
                self.assertEqual(result["historical_enrollments"], 0)
                self.assertEqual((await owner.criteria(guild)).minimum_accounts, 3)
                self.assertEqual(lifecycle.entries, {})
                self.assertEqual(sent, [])
                self.assertEqual(cog._case_store.get_joinwatch_waves(guild.id), {})

    async def test_real_lifecycle_creates_one_timer_after_ready_and_rolls_back_departed_account(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, cfg, lifecycle, ids, now, _, members, role = _real_lifecycle_fixture(honeypot, groups, directory)
                try:
                    owner = waves.JoinwatchWaves(cog)
                    preview = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                    await owner.confirm(guild, preview["id"], 42, True, now=now)
                    await owner.tick(guild, now=now)
                    self.assertEqual(await cfg.joinwatch_pending_roles(), {})
                    await lifecycle.preparation.wait()
                    await owner.tick(guild, now=now + timedelta(seconds=15))
                    self.assertEqual(len(await cfg.joinwatch_pending_roles()), 3)
                    self.assertTrue(all(role in member.roles for member in members.values()))
                    for member in members.values():
                        self.assertIsNotNone(await lifecycle.inspect(member))
                    # Later independent moderation retains the shared role after wave rollback.
                    cfg.joinwatch_pending_roles.value[str(ids[1])]["manual_role_reasons"] = [77]
                    # A departed participant still owns a timer. Rollback must cancel it.
                    del members[ids[0]]
                    guild.get_member = members.get
                    rollback = await owner.rollback_preview(guild, preview["id"], 42, True, now=now)
                    self.assertEqual(rollback["rollback_count"], 3)
                    result = await owner.rollback(guild, preview["id"], 42, True, rollback["confirmation_token"], now=now)
                    self.assertEqual(result["status"], "rolled_back")
                    self.assertEqual(await cfg.joinwatch_pending_roles(), {})
                    self.assertIn(role, members[ids[1]].roles)
                    self.assertEqual(members[ids[2]].roles, [])
                finally:
                    await lifecycle.close()

    async def test_pause_while_questions_prepare_prevents_restriction_and_notification(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, _, lifecycle, _, now, sent, _ = _fixture(groups)
                preparing = asyncio.Event()
                proceed = asyncio.Event()
                original = lifecycle.prepare_enrollment

                async def delayed_prepare(member, **kwargs):
                    preparing.set()
                    await proceed.wait()
                    return await original(member, **kwargs)

                lifecycle.prepare_enrollment = delayed_prepare
                owner = waves.JoinwatchWaves(cog)
                preview = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                await owner.confirm(guild, preview["id"], 42, True, now=now)
                execution = asyncio.create_task(owner.tick(guild, now=now))
                await preparing.wait()
                await owner.pause(guild, preview["id"], 42, True)
                proceed.set()
                await execution
                self.assertEqual(lifecycle.entries, {})
                self.assertEqual(sent, [])

    async def test_restart_pauses_and_rollback_releases_only_exact_owned_incidents(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, _, lifecycle, ids, now, sent, _ = _fixture(groups)
                owner = waves.JoinwatchWaves(cog)
                preview = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                await owner.confirm(guild, preview["id"], 42, True, now=now)
                await owner.tick(guild, now=now)
                recovered = waves.JoinwatchWaves(cog)
                await recovered.restore(guild)
                self.assertIsNone(await recovered.tick(guild, now=now + timedelta(minutes=1)))
                self.assertEqual(len(lifecycle.entries), 5)
                # Later moderation replaced an incident. Rollback must leave it alone.
                lifecycle.entries[ids[0]]["incident_id"] = "replacement"
                rollback = await recovered.rollback_preview(guild, preview["id"], 42, True, now=now)
                self.assertEqual(rollback["rollback_count"], 4)
                await recovered.rollback(guild, preview["id"], 42, True, rollback["confirmation_token"], now=now)
                self.assertEqual(set(lifecycle.entries), {ids[0]})
                self.assertEqual(len(lifecycle.releases), 4)
                self.assertEqual(len(sent), 1)

    async def test_stale_preview_and_single_queue_do_not_enroll_additional_accounts(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, cfg, lifecycle, _, now, _, configuration = _fixture(groups)
                owner = waves.JoinwatchWaves(cog)
                preview = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                configuration["joinwatch_auto_role_id"] = 78
                with self.assertRaises(ValueError):
                    await owner.confirm(guild, preview["id"], 42, True, now=now)
                configuration["joinwatch_auto_role_id"] = 77
                # Routine observations do not stale a frozen preview or grow its list.
                history = cog._case_store.get_joinwatch_history(guild.id)
                history["revision"] += 1
                cog._case_store.save_joinwatch_history(guild.id, history)
                await owner.confirm(guild, preview["id"], 42, True, now=now)
                another = await owner.preview(guild, groups.GroupCriteria(3, 15, 24), 42, now=now)
                self.assertEqual(another["already"], 7)
                with self.assertRaises(ValueError):
                    await owner.confirm(guild, another["id"], 42, True, now=now)
                await owner.pause(guild, preview["id"], 42, True)
                self.assertIsNone(await owner.tick(guild, now=now))
                self.assertEqual(lifecycle.entries, {})

    async def test_failed_notification_is_not_retried_after_restart(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, _, lifecycle, _, now, _, _ = _fixture(groups)
                calls = []

                async def unknown_send(*args, **kwargs):
                    calls.append(args)
                    if len(calls) == 1:
                        raise ConnectionError("reply lost")
                    return SimpleNamespace(id=1000)

                guild.get_channel = lambda _: SimpleNamespace(send=unknown_send)
                owner = waves.JoinwatchWaves(cog)
                preview = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                await owner.confirm(guild, preview["id"], 42, True, now=now)
                with self.assertRaises(ConnectionError):
                    await owner.tick(guild, now=now)
                restarted = waves.JoinwatchWaves(cog)
                await restarted.restore(guild)
                self.assertIsNone(await restarted.tick(guild, now=now + timedelta(minutes=1)))
                resumed = await restarted.resume(guild, preview["id"], 42, True, now=now)
                self.assertEqual(sum(entry["status"] == "notification_unknown" for entry in resumed["entries"].values()), 5)
                await restarted.tick(guild, now=now + timedelta(seconds=15))
                # The two remaining frozen accounts can proceed. No previous ping is retried.
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(lifecycle.entries), 7)
                self.assertNotIn(str(next(iter(lifecycle.entries))), calls[1][0])

    async def test_preview_has_no_effect_and_confirm_freezes_a_rate_limited_queue(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                waves = importlib.import_module("NHCogs.honeypot.joinwatch_waves")
                cog, guild, cfg, lifecycle, ids, now, sent, _ = _fixture(groups)
                owner = waves.JoinwatchWaves(cog)
                preview = await owner.preview(guild, groups.GroupCriteria(3, 15, 6), 42, now=now)
                self.assertEqual(preview["new"], 7)
                self.assertEqual(lifecycle.entries, {})
                with self.assertRaises(PermissionError):
                    await owner.confirm(guild, preview["id"], 43, True, now=now)
                await owner.confirm(guild, preview["id"], 42, True, now=now)
                await owner.confirm(guild, preview["id"], 42, True, now=now)
                await owner.tick(guild, now=now)
                self.assertEqual(len(lifecycle.entries), 5)
                await owner.tick(guild, now=now + timedelta(seconds=14))
                self.assertEqual(len(lifecycle.entries), 5)
                await owner.tick(guild, now=now + timedelta(seconds=15))
                self.assertEqual(set(lifecycle.entries), set(ids))
                self.assertEqual(len(sent), 2)
                for content, kwargs in sent:
                    self.assertEqual(kwargs.get("delete_after"), 25)
                    self.assertEqual(content.splitlines()[1], "Please complete the verification below")
                    self.assertEqual(kwargs["view"].children[0].custom_id, "honeypot:captcha:verify")
                self.assertEqual(sent[0][1]["allowed_mentions"].everyone, False)
                self.assertEqual(sent[0][1]["allowed_mentions"].roles, False)
                self.assertEqual([user.id for user in sent[0][1]["allowed_mentions"].users], ids[:5])
                record = await owner.status(guild, preview["id"])
                self.assertEqual(record["targets"], [str(uid) for uid in ids])
                self.assertEqual(record["status"], "completed")
