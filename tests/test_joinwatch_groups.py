"""Cohort matching uses first observed joins, not current membership."""

import copy
import importlib
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.harness import _isolated_honeypot_modules


class _Value:
    def __init__(self, value):
        self.value = value

    async def __call__(self):
        return copy.deepcopy(self.value)

    async def set(self, value):
        self.value = copy.deepcopy(value)


def _history_store(directory):
    cases = importlib.import_module("NHCogs.honeypot.detection_cases")
    store = cases.DetectionCaseStore(Path(directory) / "history.sqlite")
    store.initialize()
    return store


class JoinwatchGroupTests(unittest.TestCase):
    def test_history_replay_matches_each_distinct_first_join_without_transitive_expansion(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                joined = datetime(2026, 10, 1, tzinfo=timezone.utc)
                randomizer = random.Random(42)
                history = groups.empty_history()
                for offset in range(80):
                    created = joined - timedelta(days=2, hours=randomizer.randrange(20))
                    uid = ((int(created.timestamp() * 1000) - 1420070400000) << 22) + offset
                    history["observations"][str(uid)] = {"first_joined_at": (joined + timedelta(minutes=randomizer.randrange(60))).isoformat(), "imported": True}
                criteria = groups.GroupCriteria(3, 15, 6)
                rows = groups.history_observations(history)
                expected = set()
                for index, trigger in enumerate(rows):
                    expected.update(groups.match_cohort(rows[:index + 1], trigger, criteria))
                self.assertEqual(set(groups.historical_matches(history, criteria)), expected)

    def test_threshold_includes_departed_peer_and_does_not_chain_creation_dates(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                joined = datetime(2026, 10, 1, tzinfo=timezone.utc)
                created = joined - timedelta(days=5)
                rows = [
                    groups.JoinObservation(1, joined, created),
                    groups.JoinObservation(2, joined + timedelta(minutes=3), created + timedelta(hours=5)),
                    groups.JoinObservation(3, joined + timedelta(minutes=5), created + timedelta(hours=10)),
                ]
                criteria = groups.GroupCriteria(3, 15, 6)
                self.assertEqual(groups.match_cohort(rows, rows[-1], criteria), ())
                closer = groups.JoinObservation(3, rows[-1].first_joined_at, created + timedelta(hours=6))
                self.assertEqual(groups.match_cohort(rows[:-1] + [closer], closer, criteria), (1, 2, 3))

    def test_rejoin_is_not_a_new_trigger_or_distinct_account(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                joined = datetime(2026, 10, 1, tzinfo=timezone.utc)
                created = joined - timedelta(days=60)
                first = groups.JoinObservation(1, joined, created)
                second = groups.JoinObservation(2, joined + timedelta(minutes=1), created)
                rejoin = groups.JoinObservation(1, joined + timedelta(minutes=2), created)
                self.assertEqual(groups.match_cohort([first, second, rejoin], rejoin, groups.GroupCriteria(3, 15, 6)), ())
                third = groups.JoinObservation(3, joined + timedelta(minutes=15), created)
                self.assertEqual(groups.match_cohort([first, second, rejoin, third], third, groups.GroupCriteria(3, 15, 6)), (1, 2, 3))

    def test_import_validates_metadata_derives_creation_and_merges_earliest(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                created = datetime(2026, 9, 1, tzinfo=timezone.utc)
                uid = (int(created.timestamp() * 1000) - 1420070400000) << 22
                payload = {
                    "version": 1, "guild_id": "123", "source": "moderator export",
                    "generated_at": "2026-10-03T00:00:00Z",
                    "range_start": "2026-10-01T00:00:00Z", "range_end": "2026-10-02T00:00:00Z",
                    "complete": False,
                    "observations": [
                        {"user_id": str(uid), "first_joined_at": "2026-10-01T00:03:00Z"},
                        {"user_id": str(uid), "first_joined_at": "2026-10-01T00:01:00Z"},
                    ],
                }
                normalized = groups.normalize_history(payload, guild_id=123)
                row = groups.history_observations(normalized)[0]
                self.assertEqual(row.created_at, created)
                self.assertEqual(row.first_joined_at.minute, 1)
                merged = groups.merge_history(groups.empty_history(), normalized)
                self.assertEqual(groups.merge_history(merged, normalized), merged)
                self.assertEqual(len(merged["sources"]), 1)
                with self.assertRaises(ValueError):
                    groups.normalize_history(payload, guild_id=456)
                payload["observations"][0]["first_joined_at"] = "2026-10-01T00:03:00"
                with self.assertRaises(ValueError):
                    groups.normalize_history(payload, guild_id=123)


class JoinwatchHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retention_keeps_imported_support_and_live_window_and_privacy_deletes_explicitly(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                now = datetime(2026, 10, 3, tzinfo=timezone.utc)
                store = _history_store(directory)
                store.import_observations(123, {
                    "sources": [{
                        "source": "kept",
                        "generated_at": now.isoformat(),
                        "range_start": (now - timedelta(days=120)).isoformat(),
                        "range_end": now.isoformat(),
                        "complete": True,
                    }],
                    "observations": {
                        "1": {"first_joined_at": (now - timedelta(days=91)).isoformat(), "imported": True},
                    },
                })
                store.record_first_join(123, 2, now - timedelta(days=91))
                store.record_first_join(123, 3, now - timedelta(hours=23))
                guild = SimpleNamespace(id=123)
                cog = SimpleNamespace(config=SimpleNamespace(guild=lambda _: SimpleNamespace()), _case_store=store)
                owner = groups.JoinwatchGroups(cog)
                before = store.all_observations(123)["import_revision"]
                self.assertEqual(await owner.prune(guild, now=now), 1)
                self.assertEqual(set(store.all_observations(123)["observations"]), {"1", "3"})
                await owner.delete_user(guild, 1)
                document = store.all_observations(123)
                self.assertEqual(set(document["observations"]), {"3"})
                self.assertEqual(document["import_revision"], before + 1)

    async def test_observed_joins_survive_owner_restart_and_rejoin_never_inflates_cohort(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                now = datetime(2026, 10, 3, tzinfo=timezone.utc)
                created = now - timedelta(days=60)
                base = (int(created.timestamp() * 1000) - 1420070400000) << 22
                cfg = SimpleNamespace(joinwatch_groups_enabled=_Value(True),
                                      joinwatch_groups_minimum_accounts=_Value(3),
                                      joinwatch_groups_join_window_minutes=_Value(15),
                                      joinwatch_groups_creation_distance_hours=_Value(6))
                guild = SimpleNamespace(id=123)
                store = _history_store(directory)
                store.record_first_join(guild.id, base + 9, now - timedelta(days=2))
                cog = SimpleNamespace(config=SimpleNamespace(guild=lambda _: cfg), _case_store=store)
                owner = groups.JoinwatchGroups(cog)
                first = SimpleNamespace(id=base, guild=guild)
                second = SimpleNamespace(id=base + 1, guild=guild)
                third = SimpleNamespace(id=base + 2, guild=guild)
                self.assertEqual(await owner.observe(first, now=now), ())
                self.assertEqual(await owner.observe(second, now=now + timedelta(minutes=3)), ())
                restarted = groups.JoinwatchGroups(cog)
                self.assertEqual(await restarted.observe(first, now=now + timedelta(minutes=4)), ())
                matched = await restarted.observe(third, now=now + timedelta(minutes=5))
                self.assertEqual(matched, (base, base + 1, base + 2))
                self.assertNotIn(base + 9, matched)
                self.assertEqual(
                    store.observations_since(guild.id, now - timedelta(minutes=15)),
                    (
                        (base, now.isoformat()),
                        (base + 1, (now + timedelta(minutes=3)).isoformat()),
                        (base + 2, (now + timedelta(minutes=5)).isoformat()),
                    ),
                )
                self.assertEqual(store.get_joinwatch_observation(guild.id, base)["first_joined_at"], now.isoformat())
                self.assertEqual((await restarted.read_history(guild))["import_revision"], 0)

    async def test_observed_join_matches_when_the_clock_has_microseconds(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                groups = importlib.import_module("NHCogs.honeypot.joinwatch_groups")
                now = datetime(2026, 10, 3, 12, 0, 7, 654321, tzinfo=timezone.utc)
                stored = now.replace(microsecond=0)
                created = stored - timedelta(days=60)
                base = (int(created.timestamp() * 1000) - 1420070400000) << 22
                cfg = SimpleNamespace(joinwatch_groups_enabled=_Value(True),
                                      joinwatch_groups_minimum_accounts=_Value(3),
                                      joinwatch_groups_join_window_minutes=_Value(15),
                                      joinwatch_groups_creation_distance_hours=_Value(6))
                guild = SimpleNamespace(id=123)
                store = _history_store(directory)
                store.record_first_join(guild.id, base, stored)
                store.record_first_join(guild.id, base + 1, stored + timedelta(minutes=3))
                cog = SimpleNamespace(config=SimpleNamespace(guild=lambda _: cfg), _case_store=store)
                owner = groups.JoinwatchGroups(cog)
                trigger = SimpleNamespace(id=base + 2, guild=guild)
                matched = await owner.observe(
                    trigger, now=stored + timedelta(minutes=5, microseconds=80)
                )
                self.assertEqual(matched, (base, base + 1, base + 2))
                self.assertEqual(
                    store.get_joinwatch_observation(guild.id, base + 2)["first_joined_at"],
                    (stored + timedelta(minutes=5)).isoformat(),
                )
