from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MODULE_PATH = Path(__file__).resolve().parents[1] / "NHMisc" / "achievement_store.py"
SPEC = importlib.util.spec_from_file_location("_achievement_store", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load achievement store")
achievement_store = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = achievement_store
SPEC.loader.exec_module(achievement_store)


class AchievementStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "achievements.sqlite"
        self.store = achievement_store.AchievementStore(self.path)
        await self.store.initialize()

    async def test_boolean_award_is_idempotent_and_keeps_original_proof(self):
        first = await self.store.grant_boolean(
            1,
            2,
            "solo_gater",
            source_channel_id=3,
            source_message_id=4,
        )
        second = await self.store.grant_boolean(
            1,
            2,
            "solo_gater",
            source_channel_id=30,
            source_message_id=40,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.award.award_id, first.award.award_id)
        self.assertEqual(second.award.source_channel_id, 3)
        self.assertEqual(second.award.source_message_id, 4)

    async def test_imported_gate_progress_creates_stable_proofless_ordinals(self):
        await self.store.import_gate_progress(1, 2, 3)
        fourth = await self.store.grant_stargate(
            1,
            2,
            source_channel_id=5,
            source_message_id=6,
        )

        profile = await self.store.get_profile(1, 2)

        self.assertEqual(fourth.award.ordinal, 4)
        self.assertEqual(profile.stargate_count, 4)
        self.assertEqual(
            tuple(proof.ordinal for proof in profile.stargate_proofs),
            (4,),
        )
        self.assertEqual(profile.stargate_proofs[0].source_message_id, 6)

    async def test_boolean_revocation_is_historical_and_allows_regrant(self):
        original = await self.store.grant_boolean(1, 2, "solo_gater")

        revoked = await self.store.revoke_booleans(1, (2,), ("solo_gater",))
        replacement = await self.store.grant_boolean(1, 2, "solo_gater")
        profile = await self.store.get_profile(1, 2)

        self.assertEqual(revoked, 1)
        self.assertTrue(replacement.created)
        self.assertNotEqual(replacement.award.award_id, original.award.award_id)
        self.assertEqual(profile.boolean_keys, ("solo_gater",))

    async def test_shared_boolean_keys_are_an_intersection_in_registry_order(self):
        for user_id, key in (
            (10, "solo_gater"),
            (10, "all_quests"),
            (11, "solo_gater"),
        ):
            await self.store.grant_boolean(1, user_id, key)

        shared = await self.store.shared_boolean_keys(1, (10, 11))

        self.assertEqual(shared, ("solo_gater",))

    async def test_bootstrap_completion_is_persisted_per_guild(self):
        self.assertFalse(await self.store.is_bootstrapped(123))

        await self.store.mark_bootstrapped(123)

        reopened = achievement_store.AchievementStore(self.path)
        await reopened.initialize()
        self.assertTrue(await reopened.is_bootstrapped(123))

    async def test_bootstrap_imports_gate_ordinals_and_boolean_roles_atomically(self):
        created = await self.store.bootstrap_guild(
            50,
            gate_tiers={10: 3, 11: 1},
            boolean_users={"solo_gater": (10,)},
        )

        first = await self.store.get_profile(50, 10)
        second = await self.store.get_profile(50, 11)
        repeated = await self.store.bootstrap_guild(
            50,
            gate_tiers={10: 6},
            boolean_users={},
        )

        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first.stargate_count, 3)
        self.assertEqual(first.boolean_keys, ("solo_gater",))
        self.assertEqual(second.stargate_count, 1)
        self.assertTrue(await self.store.is_bootstrapped(50))

    async def test_gate_projection_counts_pending_reserved_awards(self):
        await self.store.import_gate_progress(1, 2, 2)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, ordinal,
                    awarded_at, state
                ) VALUES (1, 2, 'stargate_completed', 3, 'now', 'pending')
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(await self.store.get_gate_projection(1, 2), 3)


if __name__ == "__main__":
    unittest.main()
