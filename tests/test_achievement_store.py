from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from contextlib import closing
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
        await self.store.bootstrap_guild(
            1,
            gate_tiers={},
            boolean_definitions=(
                achievement_store.AchievementDefinition(
                    key="solo_gater",
                    display_name="Solo Gater",
                    kind=achievement_store.AchievementKind.BOOLEAN,
                    display_order=0,
                ),
                achievement_store.AchievementDefinition(
                    key="all_quests",
                    display_name="All Quests",
                    kind=achievement_store.AchievementKind.BOOLEAN,
                    display_order=1,
                ),
            ),
            boolean_users={},
        )

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

    async def test_database_backup_contains_committed_achievement_state(self):
        await self.store.grant_boolean(1, 2, "solo_gater")

        backup_bytes = await self.store.backup_database()

        backup_path = Path(self.temp_dir.name) / "backup.sqlite"
        backup_path.write_bytes(backup_bytes)
        with closing(sqlite3.connect(backup_path)) as connection:
            award_count = connection.execute("SELECT COUNT(*) FROM achievement_awards").fetchone()[
                0
            ]
        self.assertTrue(backup_bytes.startswith(b"SQLite format 3"))
        self.assertEqual(award_count, 1)

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

    async def test_historical_proof_can_fill_different_ordinals_from_one_message(self):
        await self.store.import_gate_progress(1, 2, 3)
        await self.store.import_gate_progress(1, 3, 2)

        attached = await self.store.attach_stargate_proofs(
            1,
            {2: 3, 3: 1},
            source_channel_id=50,
            source_message_id=60,
        )

        first_profile = await self.store.get_profile(1, 2)
        second_profile = await self.store.get_profile(1, 3)
        self.assertEqual(
            tuple((proof.ordinal, proof.source_message_id) for proof in attached),
            ((3, 60), (1, 60)),
        )
        self.assertEqual(first_profile.stargate_count, 3)
        self.assertEqual(first_profile.stargate_proofs, (attached[0],))
        self.assertEqual(second_profile.stargate_count, 2)
        self.assertEqual(second_profile.stargate_proofs, (attached[1],))

    async def test_historical_proof_conflict_rolls_back_every_assignment(self):
        await self.store.grant_stargate(
            1,
            2,
            source_channel_id=10,
            source_message_id=20,
        )
        await self.store.import_gate_progress(1, 3, 1)

        with self.assertRaises(achievement_store.GateProofConflict):
            await self.store.attach_stargate_proofs(
                1,
                {3: 1, 2: 1},
                source_channel_id=50,
                source_message_id=60,
            )

        self.assertEqual(
            (await self.store.get_profile(1, 3)).stargate_proofs,
            (),
        )

    async def test_missing_historical_proofs_are_loaded_for_all_requested_users(self):
        await self.store.import_gate_progress(1, 2, 3)
        await self.store.attach_stargate_proofs(
            1,
            {2: 2},
            source_channel_id=50,
            source_message_id=60,
        )
        await self.store.grant_stargate(
            1,
            3,
            source_channel_id=70,
            source_message_id=80,
        )

        missing = await self.store.missing_stargate_proofs(1, (2, 3, 4))

        self.assertEqual(missing, {2: (1, 3), 3: (), 4: ()})

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
            boolean_definitions=(
                achievement_store.AchievementDefinition(
                    key="solo_gater",
                    display_name="Solo Gater",
                    kind=achievement_store.AchievementKind.BOOLEAN,
                    role_id=99,
                ),
            ),
            boolean_users={"solo_gater": (10,)},
        )

        first = await self.store.get_profile(50, 10)
        second = await self.store.get_profile(50, 11)
        repeated = await self.store.bootstrap_guild(
            50,
            gate_tiers={10: 6},
            boolean_definitions=(),
            boolean_users={},
        )

        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first.stargate_count, 3)
        self.assertEqual(first.boolean_keys, ("solo_gater",))
        self.assertEqual(second.stargate_count, 1)
        self.assertTrue(await self.store.is_bootstrapped(50))

    async def test_unbinding_seeded_role_keeps_awards_and_survives_restart(self):
        solo = achievement_store.AchievementDefinition(
            key="solo_gater",
            display_name="Solo Gater",
            kind=achievement_store.AchievementKind.BOOLEAN,
            role_id=99,
            display_order=0,
        )
        await self.store.bootstrap_guild(
            50,
            gate_tiers={},
            boolean_definitions=(solo,),
            boolean_users={"solo_gater": (10,)},
        )

        unbound = await self.store.unbind_role(50, 99)
        reopened = achievement_store.AchievementStore(self.path)
        await reopened.initialize()

        self.assertEqual(unbound.key, "solo_gater")
        self.assertIsNone(unbound.role_id)
        self.assertEqual(
            (await reopened.get_profile(50, 10)).boolean_keys,
            ("solo_gater",),
        )
        self.assertIsNone((await reopened.list_definitions(50))[0].role_id)

    async def test_binding_role_imports_current_holders_without_proofs(self):
        await self.store.mark_bootstrapped(50)
        definition = await self.store.create_boolean_definition(50, "All Quests")

        result = await self.store.bind_role(
            50,
            definition.key,
            role_id=123,
            user_ids=(10, 11),
        )

        self.assertEqual(result.definition.role_id, 123)
        self.assertEqual(result.imported_count, 2)
        self.assertEqual(
            (await self.store.get_profile(50, 10)).boolean_keys,
            (definition.key,),
        )
        connection = sqlite3.connect(self.path)
        try:
            proof = connection.execute(
                """
                SELECT source_channel_id, source_message_id
                FROM achievement_awards
                WHERE guild_id = 50 AND user_id = 10
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(proof, (None, None))

    async def test_one_role_cannot_bind_to_two_achievements(self):
        first = await self.store.create_boolean_definition(50, "First")
        second = await self.store.create_boolean_definition(50, "Second")
        await self.store.bind_role(
            50,
            first.key,
            role_id=123,
            user_ids=(),
        )

        with self.assertRaisesRegex(ValueError, "already bound"):
            await self.store.bind_role(
                50,
                second.key,
                role_id=123,
                user_ids=(),
            )

    async def test_role_replacement_rejects_an_already_bound_target(self):
        first = await self.store.create_boolean_definition(50, "First")
        second = await self.store.create_boolean_definition(50, "Second")
        await self.store.bind_role(50, first.key, role_id=123, user_ids=())
        await self.store.bind_role(50, second.key, role_id=456, user_ids=())

        with self.assertRaisesRegex(ValueError, "already bound"):
            await self.store.replace_role(
                50,
                achievement_key=first.key,
                old_role_id=123,
                new_role_id=456,
                user_ids=(),
            )

    async def test_role_replacement_rejects_a_rebound_source_role(self):
        first = await self.store.create_boolean_definition(50, "First")
        second = await self.store.create_boolean_definition(50, "Second")
        await self.store.bind_role(50, first.key, role_id=123, user_ids=())
        await self.store.unbind_role(50, 123)
        await self.store.bind_role(50, second.key, role_id=123, user_ids=())

        with self.assertRaisesRegex(LookupError, "not bound"):
            await self.store.replace_role(
                50,
                achievement_key=first.key,
                old_role_id=123,
                new_role_id=456,
                user_ids=(),
            )

        definitions = await self.store.list_definitions(50)
        rebound = next(item for item in definitions if item.key == second.key)
        self.assertEqual(rebound.role_id, 123)

    async def test_replacing_role_preserves_owners_and_imports_new_holders(self):
        await self.store.mark_bootstrapped(50)
        definition = await self.store.create_boolean_definition(50, "All Quests")
        await self.store.bind_role(
            50,
            definition.key,
            role_id=123,
            user_ids=(10,),
        )

        result = await self.store.replace_role(
            50,
            achievement_key=definition.key,
            old_role_id=123,
            new_role_id=456,
            user_ids=(11,),
        )

        self.assertEqual(result.definition.role_id, 456)
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(
            await self.store.active_users_for_boolean(50, definition.key),
            (10, 11),
        )

    async def test_discord_snapshot_replaces_active_role_projection_atomically(self):
        solo = achievement_store.AchievementDefinition(
            key="solo_gater",
            display_name="Solo Gater",
            kind=achievement_store.AchievementKind.BOOLEAN,
            role_id=99,
        )
        await self.store.bootstrap_guild(
            50,
            gate_tiers={10: 3},
            boolean_definitions=(solo,),
            boolean_users={"solo_gater": (10,)},
        )

        result = await self.store.apply_discord_snapshot(
            50,
            gate_tiers={10: 1, 11: 2},
            boolean_users={"solo_gater": (11,)},
        )

        self.assertEqual(await self.store.get_gate_projection(50, 10), 1)
        self.assertEqual(await self.store.get_gate_projection(50, 11), 2)
        self.assertEqual((await self.store.get_profile(50, 10)).boolean_keys, ())
        self.assertEqual(
            (await self.store.get_profile(50, 11)).boolean_keys,
            ("solo_gater",),
        )
        self.assertEqual(result.changed_users, 2)

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

    async def test_latest_gate_delete_preserves_history_and_reuses_ordinal(self):
        first = await self.store.grant_stargate(
            1,
            2,
            source_channel_id=10,
            source_message_id=100,
        )
        latest = await self.store.grant_stargate(
            1,
            2,
            source_channel_id=20,
            source_message_id=200,
        )

        self.assertEqual(await self.store.get_latest_stargate(1, 2), latest.award)

        deleted = await self.store.delete_latest_stargate(
            1,
            2,
            expected_award_id=latest.award.award_id,
        )

        self.assertEqual(deleted, latest.award)
        profile = await self.store.get_profile(1, 2)
        self.assertEqual(profile.stargate_count, 1)
        self.assertEqual(profile.stargate_proofs[0].ordinal, 1)
        self.assertEqual(profile.stargate_proofs[0].source_message_id, 100)
        replacement = await self.store.grant_stargate(
            1,
            2,
            source_channel_id=30,
            source_message_id=300,
        )
        self.assertEqual(replacement.award.ordinal, 2)
        self.assertEqual(first.award.ordinal, 1)

    async def test_latest_gate_delete_rejects_stale_award(self):
        stale = await self.store.grant_stargate(1, 2)
        await self.store.grant_stargate(1, 2)

        deleted = await self.store.delete_latest_stargate(
            1,
            2,
            expected_award_id=stale.award.award_id,
        )

        self.assertIsNone(deleted)
        self.assertEqual(await self.store.get_gate_projection(1, 2), 2)

    async def test_latest_gate_delete_rejects_pending_increment(self):
        active = await self.store.grant_stargate(1, 2)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, ordinal,
                    awarded_at, state
                ) VALUES (1, 2, 'stargate_completed', 2, 'now', 'pending')
                """
            )
            connection.commit()
        finally:
            connection.close()

        deleted = await self.store.delete_latest_stargate(
            1,
            2,
            expected_award_id=active.award.award_id,
        )

        self.assertIsNone(deleted)
        self.assertEqual(await self.store.get_latest_stargate(1, 2), active.award)

    async def test_boolean_projection_counts_pending_reserved_awards(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, awarded_at, state
                ) VALUES (1, 2, 'solo_gater', 'now', 'pending')
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(
            await self.store.projected_users_for_boolean(1, "solo_gater"),
            (2,),
        )

    async def test_rename_changes_only_the_display_name(self):
        original = await self.store.create_boolean_definition(50, "All Quest")
        await self.store.grant_boolean(
            50,
            10,
            original.key,
            source_channel_id=20,
            source_message_id=30,
        )

        renamed = await self.store.rename_definition(
            50,
            original.key,
            "All Quests",
        )

        self.assertEqual(renamed.key, original.key)
        self.assertEqual(renamed.display_name, "All Quests")
        self.assertEqual(
            (await self.store.get_profile(50, 10)).boolean_keys,
            (original.key,),
        )

    async def test_delete_removes_definition_and_every_award_state_atomically(self):
        definition = await self.store.create_boolean_definition(50, "Obsolete")
        await self.store.grant_boolean(50, 10, definition.key)
        await self.store.revoke_booleans(50, (10,), (definition.key,))
        await self.store.grant_boolean(50, 11, definition.key)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, awarded_at, state
                ) VALUES (50, 12, ?, 'now', 'pending')
                """,
                (definition.key,),
            )
            connection.commit()
        finally:
            connection.close()

        preview = await self.store.prepare_definition_deletion(50, definition.key)
        deleted = await self.store.delete_definition(
            50,
            definition.key,
            expected_award_count=preview.award_count,
        )

        self.assertEqual(deleted, preview)
        self.assertNotIn(definition, await self.store.list_definitions(50))
        connection = sqlite3.connect(self.path)
        try:
            remaining = connection.execute(
                """
                SELECT COUNT(*) FROM achievement_awards
                WHERE guild_id = 50 AND achievement_key = ?
                """,
                (definition.key,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(remaining, 0)

    async def test_delete_requires_role_unbinding_first(self):
        definition = await self.store.create_boolean_definition(50, "All Quests")
        await self.store.bind_role(50, definition.key, role_id=123, user_ids=())

        with self.assertRaisesRegex(ValueError, "Unbind"):
            await self.store.prepare_definition_deletion(50, definition.key)

        self.assertEqual(len(await self.store.list_definitions(50)), 1)

    async def test_delete_rejects_a_review_when_awards_changed(self):
        definition = await self.store.create_boolean_definition(50, "Obsolete")
        preview = await self.store.prepare_definition_deletion(50, definition.key)
        await self.store.grant_boolean(50, 10, definition.key)

        with self.assertRaisesRegex(RuntimeError, "changed"):
            await self.store.delete_definition(
                50,
                definition.key,
                expected_award_count=preview.award_count,
            )

        self.assertIn(definition, await self.store.list_definitions(50))
        self.assertEqual(
            (await self.store.get_profile(50, 10)).boolean_keys,
            (definition.key,),
        )

    async def test_system_achievement_cannot_be_deleted_after_unbinding(self):
        solo = achievement_store.AchievementDefinition(
            key="solo_gater",
            display_name="Solo Gater",
            kind=achievement_store.AchievementKind.BOOLEAN,
            role_id=None,
        )
        await self.store.bootstrap_guild(
            50,
            gate_tiers={},
            boolean_definitions=(solo,),
            boolean_users={},
        )

        with self.assertRaisesRegex(ValueError, "System"):
            await self.store.prepare_definition_deletion(50, solo.key)

    async def test_deleted_achievement_cannot_be_restored_by_a_stale_grant(self):
        definition = await self.store.create_boolean_definition(50, "Obsolete")
        preview = await self.store.prepare_definition_deletion(50, definition.key)
        await self.store.delete_definition(
            50,
            definition.key,
            expected_award_count=preview.award_count,
        )

        with self.assertRaisesRegex(LookupError, "does not exist"):
            await self.store.grant_boolean(50, 10, definition.key)

        self.assertEqual((await self.store.get_profile(50, 10)).boolean_keys, ())

    async def test_rename_rejects_an_empty_display_name(self):
        definition = await self.store.create_boolean_definition(50, "Keep Me")

        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            await self.store.rename_definition(50, definition.key, "   ")

        self.assertIn(definition, await self.store.list_definitions(50))


if __name__ == "__main__":
    unittest.main()
