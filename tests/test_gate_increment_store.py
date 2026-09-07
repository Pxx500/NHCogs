from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "NHCogs" / "nhmisc" / "gate_increment_store.py"
)
SPEC = importlib.util.spec_from_file_location("_gate_increment_store", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Gate increment store")
gate_increment_store = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate_increment_store
SPEC.loader.exec_module(gate_increment_store)


class GateIncrementStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "gate_increment.sqlite"
        self.first_store = gate_increment_store.GateIncrementStore(self.path)
        self.second_store = gate_increment_store.GateIncrementStore(self.path)
        await self.first_store.initialize()
        await self.second_store.initialize()

    def _insert_definitions(self, *definitions):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS achievement_definitions (
                    guild_id INTEGER NOT NULL,
                    achievement_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    role_id INTEGER,
                    grantable INTEGER NOT NULL,
                    revocable INTEGER NOT NULL,
                    display_order INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, achievement_key)
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO achievement_definitions VALUES (
                    20, ?, ?, 'boolean', ?, 1, 1, ?
                )
                """,
                (
                    (key, display_name, role_id, position)
                    for position, (key, display_name, role_id) in enumerate(definitions)
                ),
            )
            connection.commit()
        finally:
            connection.close()

    async def test_concurrent_claims_consume_source_message_once(self):
        key = gate_increment_store.SourceMessageKey(1, 2, 3)
        plans = (
            gate_increment_store.GateIncrementMemberPlan(4, (), 5),
        )

        first, second = await asyncio.gather(
            self.first_store.claim(key, 10, plans),
            self.second_store.claim(key, 11, plans),
        )

        self.assertEqual(sum(result.created for result in (first, second)), 1)
        self.assertEqual(first.operation.operation_id, second.operation.operation_id)
        self.assertIn(first.operation.moderator_id, (10, 11))
        self.assertEqual(
            first.operation.moderator_id,
            second.operation.moderator_id,
        )

    async def test_different_messages_cannot_reserve_same_member_ordinal(self):
        first_key = gate_increment_store.SourceMessageKey(1, 2, 30)
        second_key = gate_increment_store.SourceMessageKey(1, 2, 31)
        plan = gate_increment_store.GateIncrementMemberPlan(
            4,
            (),
            5,
            target_ordinal=1,
        )

        results = await asyncio.gather(
            self.first_store.claim(first_key, 10, (plan,)),
            self.second_store.claim(second_key, 11, (plan,)),
            return_exceptions=True,
        )

        self.assertEqual(
            sum(isinstance(result, gate_increment_store.ClaimResult) for result in results),
            1,
        )
        self.assertEqual(
            sum(
                isinstance(result, gate_increment_store.GateProgressConflict)
                for result in results
            ),
            1,
        )

    async def test_claim_fills_the_lowest_missing_gate_ordinal(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.executemany(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, ordinal,
                    awarded_at, state
                ) VALUES (1, 4, 'stargate_completed', ?, 'now', 'active')
                """,
                ((1,), (3,)),
            )
            connection.commit()
        finally:
            connection.close()
        key = gate_increment_store.SourceMessageKey(1, 2, 32)
        plan = gate_increment_store.GateIncrementMemberPlan(
            4,
            (50,),
            60,
            target_ordinal=2,
        )

        result = await self.first_store.claim(key, 10, (plan,))

        self.assertTrue(result.created)
        connection = sqlite3.connect(self.path)
        try:
            row = connection.execute(
                """
                SELECT ordinal
                FROM achievement_awards
                WHERE gate_operation_id = ?
                """,
                (result.operation.operation_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row[0], 2)

    async def test_completed_member_activates_reserved_gate_and_solo_awards(self):
        key = gate_increment_store.SourceMessageKey(20, 21, 22)
        plan = gate_increment_store.GateIncrementMemberPlan(
            23,
            (),
            24,
            target_ordinal=1,
            grant_solo=True,
        )
        await self.first_store.claim(key, 25, (plan,))

        await self.first_store.mark_member_completed(key, 0)

        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                """
                SELECT achievement_key, ordinal, state, source_channel_id,
                    source_message_id
                FROM achievement_awards
                ORDER BY award_id
                """
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            rows,
            [
                ("stargate_completed", 1, "active", 21, 22),
                ("solo_gater", None, "active", 21, 22),
            ],
        )

    async def test_claim_persists_custom_definitions_and_only_new_member_awards(self):
        self._insert_definitions(
            ("garden_of_grind", "Garden of Grind", 30),
            ("flawless", "Flawless", None),
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.executemany(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, awarded_at, state
                ) VALUES (20, 23, ?, 'earlier', 'active')
                """,
                (("garden_of_grind",), ("solo_gater",)),
            )
            connection.commit()
        finally:
            connection.close()
        key = gate_increment_store.SourceMessageKey(20, 21, 22)
        plans = (
            gate_increment_store.GateIncrementMemberPlan(
                23, (), 24, grant_solo=True
            ),
            gate_increment_store.GateIncrementMemberPlan(25, (), 24),
        )
        achievements = (
            gate_increment_store.GateIncrementAchievementPlan(
                "garden_of_grind", "Garden of Grind", 30
            ),
            gate_increment_store.GateIncrementAchievementPlan(
                "flawless", "Flawless"
            ),
        )

        await self.first_store.claim(key, 26, plans, achievements)
        reopened = gate_increment_store.GateIncrementStore(self.path)
        await reopened.initialize()

        snapshot = await reopened.get_operation(key)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.custom_achievements, achievements)
        self.assertFalse(snapshot.members[0].solo_awarded)
        self.assertEqual(snapshot.members[0].custom_achievement_keys, ("flawless",))
        self.assertEqual(
            snapshot.members[1].custom_achievement_keys,
            ("garden_of_grind", "flawless"),
        )

    async def test_completed_member_activates_reserved_custom_awards(self):
        self._insert_definitions(
            ("garden_of_grind", "Garden of Grind", 30),
            ("flawless", "Flawless", None),
        )
        key = gate_increment_store.SourceMessageKey(20, 21, 22)
        plan = gate_increment_store.GateIncrementMemberPlan(23, (), 24)
        achievements = (
            gate_increment_store.GateIncrementAchievementPlan(
                "garden_of_grind", "Garden of Grind", 30
            ),
            gate_increment_store.GateIncrementAchievementPlan(
                "flawless", "Flawless"
            ),
        )
        await self.first_store.claim(key, 25, (plan,), achievements)

        await self.first_store.mark_member_completed(key, 0)

        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                """
                SELECT achievement_key, state
                FROM achievement_awards
                ORDER BY award_id
                """
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            rows,
            [
                ("stargate_completed", "active"),
                ("garden_of_grind", "active"),
                ("flawless", "active"),
            ],
        )

    async def test_claim_rejects_a_changed_custom_definition_atomically(self):
        self._insert_definitions(("garden_of_grind", "Garden of Grind", 31))
        key = gate_increment_store.SourceMessageKey(20, 21, 22)

        with self.assertRaises(gate_increment_store.AchievementDefinitionConflict):
            await self.first_store.claim(
                key,
                25,
                (gate_increment_store.GateIncrementMemberPlan(23, (), 24),),
                (
                    gate_increment_store.GateIncrementAchievementPlan(
                        "garden_of_grind", "Garden of Grind", 30
                    ),
                ),
            )

        self.assertIsNone(await self.first_store.get_operation(key))

    async def test_moderation_log_delivery_survives_store_reopen(self):
        key = gate_increment_store.SourceMessageKey(20, 21, 22)
        await self.first_store.claim(
            key,
            25,
            (gate_increment_store.GateIncrementMemberPlan(23, (), 24),),
        )
        await self.first_store.mark_member_completed(key, 0)

        await self.first_store.mark_moderation_logged(key, (0,))
        reopened = gate_increment_store.GateIncrementStore(self.path)
        await reopened.initialize()
        snapshot = await reopened.get_operation(key)

        self.assertTrue(snapshot.members[0].moderation_logged)

    async def test_schema_upgrade_marks_existing_deliveries_as_settled(self):
        legacy_path = Path(self.temp_dir.name) / "legacy-gate-increment.sqlite"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE gate_increment_operations (
                    operation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    moderator_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    selected_count INTEGER NOT NULL,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    conflict_count INTEGER NOT NULL DEFAULT 0,
                    result_channel_id INTEGER,
                    result_message_id INTEGER,
                    lease_token TEXT,
                    publication_token TEXT,
                    UNIQUE (guild_id, channel_id, source_message_id)
                );
                CREATE TABLE gate_increment_members (
                    operation_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    user_id INTEGER,
                    expected_gate_role_ids TEXT NOT NULL,
                    target_role_id INTEGER,
                    state TEXT NOT NULL,
                    failure_code TEXT,
                    grant_solo INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (operation_id, position),
                    UNIQUE (operation_id, user_id)
                );
                INSERT INTO gate_increment_operations VALUES (
                    1, 20, 21, 22, 25, 'now', 'now', 'completed',
                    1, 1, 0, 0, 21, 30, NULL, NULL
                );
                INSERT INTO gate_increment_members VALUES (
                    1, 0, 23, '[]', 24, 'completed', NULL, 0
                );
                """
            )
            connection.commit()
        finally:
            connection.close()
        store = gate_increment_store.GateIncrementStore(legacy_path)

        await store.initialize()
        snapshot = await store.get_operation(
            gate_increment_store.SourceMessageKey(20, 21, 22)
        )

        self.assertEqual(snapshot.operation.published_completed_count, 1)
        self.assertTrue(snapshot.members[0].moderation_logged)
        self.assertEqual(await store.list_interrupted_operations(), ())

    async def test_claimed_targets_survive_store_reopen(self):
        key = gate_increment_store.SourceMessageKey(10, 20, 30)
        plans = (
            gate_increment_store.GateIncrementMemberPlan(40, (50, 60), 70),
            gate_increment_store.GateIncrementMemberPlan(41, (), 50),
        )
        claim = await self.first_store.claim(key, 99, plans)
        reopened = gate_increment_store.GateIncrementStore(self.path)
        await reopened.initialize()

        snapshot = await reopened.get_operation(key)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.operation.operation_id, claim.operation.operation_id)
        self.assertEqual(snapshot.operation.moderator_id, 99)
        self.assertEqual(snapshot.operation.state.value, "applying")
        self.assertEqual(snapshot.operation.selected_count, 2)
        self.assertEqual(
            snapshot.members,
            (
                gate_increment_store.StoredGateIncrementMember(
                    0,
                    40,
                    (50, 60),
                    70,
                    gate_increment_store.MemberState.PENDING,
                    None,
                ),
                gate_increment_store.StoredGateIncrementMember(
                    1,
                    41,
                    (),
                    50,
                    gate_increment_store.MemberState.PENDING,
                    None,
                ),
            ),
        )

    async def test_only_one_executor_can_hold_the_operation_lease(self):
        key = gate_increment_store.SourceMessageKey(100, 200, 300)
        plans = (gate_increment_store.GateIncrementMemberPlan(400, (), 500),)
        await self.first_store.claim(key, 600, plans)

        first, second = await asyncio.gather(
            self.first_store.acquire_execution_lease(key, "first"),
            self.second_store.acquire_execution_lease(key, "second"),
        )

        self.assertEqual(sum((first, second)), 1)
        winner_store, winner_token = (
            (self.first_store, "first")
            if first
            else (self.second_store, "second")
        )
        loser_store, loser_token = (
            (self.second_store, "second")
            if first
            else (self.first_store, "first")
        )
        await winner_store.release_execution_lease(key, winner_token)
        self.assertTrue(
            await loser_store.acquire_execution_lease(key, loser_token)
        )

    async def test_only_one_publisher_can_hold_the_publication_lease(self):
        key = gate_increment_store.SourceMessageKey(101, 201, 301)
        plans = (gate_increment_store.GateIncrementMemberPlan(401, (), 501),)
        await self.first_store.claim(key, 601, plans)
        await self.first_store.mark_member_completed(key, 0)
        await self.first_store.finalize_operation(key)

        first, second = await asyncio.gather(
            self.first_store.acquire_publication_lease(key, "first"),
            self.second_store.acquire_publication_lease(key, "second"),
        )

        self.assertEqual(sum((first, second)), 1)

    async def test_finalization_persists_partial_member_results_and_counts(self):
        key = gate_increment_store.SourceMessageKey(1000, 2000, 3000)
        plans = tuple(
            gate_increment_store.GateIncrementMemberPlan(user_id, (), 5000)
            for user_id in (4000, 4001, 4002)
        )
        await self.first_store.claim(key, 6000, plans)

        await self.first_store.mark_member_in_progress(key, 0)
        await self.first_store.mark_member_completed(key, 0)
        await self.first_store.mark_member_failed(key, 1, "forbidden")
        await self.first_store.mark_member_conflict(key, 2, "roles_changed")
        snapshot = await self.first_store.finalize_operation(key)

        self.assertEqual(snapshot.operation.state.value, "partial")
        self.assertEqual(snapshot.operation.completed_count, 1)
        self.assertEqual(snapshot.operation.failed_count, 1)
        self.assertEqual(snapshot.operation.conflict_count, 1)
        self.assertEqual(
            tuple(member.state.value for member in snapshot.members),
            ("completed", "failed", "conflict"),
        )
        self.assertEqual(snapshot.members[1].failure_code, "forbidden")
        self.assertEqual(snapshot.members[2].failure_code, "roles_changed")

    def test_recovery_classifies_completed_retryable_and_conflicting_roles(self):
        member = gate_increment_store.StoredGateIncrementMember(
            0,
            4000,
            (5000, 5001),
            5002,
            gate_increment_store.MemberState.IN_PROGRESS,
            None,
        )

        self.assertEqual(
            gate_increment_store.classify_member_recovery(member, (5002,)),
            gate_increment_store.RecoveryAction.COMPLETE,
        )
        self.assertEqual(
            gate_increment_store.classify_member_recovery(member, (5000, 5001)),
            gate_increment_store.RecoveryAction.RETRY,
        )
        self.assertEqual(
            gate_increment_store.classify_member_recovery(member, (5001, 5002)),
            gate_increment_store.RecoveryAction.CONFLICT,
        )

    async def test_finalization_completes_operation_when_every_member_succeeds(self):
        key = gate_increment_store.SourceMessageKey(7000, 7001, 7002)
        plans = (
            gate_increment_store.GateIncrementMemberPlan(7003, (), 7004),
            gate_increment_store.GateIncrementMemberPlan(7005, (7004,), 7006),
        )
        await self.first_store.claim(key, 7007, plans)
        await self.first_store.mark_member_completed(key, 0)
        await self.first_store.mark_member_completed(key, 1)

        snapshot = await self.first_store.finalize_operation(key)

        self.assertEqual(
            snapshot.operation.state,
            gate_increment_store.OperationState.COMPLETED,
        )
        self.assertEqual(snapshot.operation.completed_count, 2)
        self.assertFalse(
            await self.second_store.acquire_execution_lease(key, "too-late")
        )

    async def test_result_message_persists_without_reopening_role_execution(self):
        key = gate_increment_store.SourceMessageKey(8000, 8001, 8002)
        await self.first_store.claim(
            key,
            8003,
            (gate_increment_store.GateIncrementMemberPlan(8004, (), 8005),),
        )
        await self.first_store.mark_member_completed(key, 0)
        await self.first_store.finalize_operation(key)

        self.assertTrue(
            await self.first_store.acquire_publication_lease(key, "publisher")
        )
        snapshot = await self.first_store.record_result_message(
            key,
            "publisher",
            8001,
            8006,
            1,
        )

        self.assertEqual(snapshot.operation.result_channel_id, 8001)
        self.assertEqual(snapshot.operation.result_message_id, 8006)
        reopened = gate_increment_store.GateIncrementStore(self.path)
        await reopened.initialize()
        persisted = await reopened.get_operation(key)
        self.assertEqual(persisted.operation.result_message_id, 8006)
        self.assertFalse(await reopened.acquire_execution_lease(key, "publish-only"))

    async def test_startup_releases_abandoned_applying_lease_for_recovery(self):
        key = gate_increment_store.SourceMessageKey(9000, 9001, 9002)
        await self.first_store.claim(
            key,
            9003,
            (gate_increment_store.GateIncrementMemberPlan(9004, (), 9005),),
        )
        self.assertTrue(
            await self.first_store.acquire_execution_lease(key, "dead-process")
        )

        restarted = gate_increment_store.GateIncrementStore(self.path)
        await restarted.initialize()

        self.assertTrue(await restarted.acquire_execution_lease(key, "recovery"))

    async def test_startup_releases_abandoned_publication_lease(self):
        key = gate_increment_store.SourceMessageKey(9100, 9101, 9102)
        await self.first_store.claim(
            key,
            9103,
            (gate_increment_store.GateIncrementMemberPlan(9104, (), 9105),),
        )
        await self.first_store.mark_member_completed(key, 0)
        await self.first_store.finalize_operation(key)
        self.assertTrue(
            await self.first_store.acquire_publication_lease(key, "abandoned")
        )

        restarted = gate_increment_store.GateIncrementStore(self.path)
        await restarted.initialize()

        self.assertTrue(
            await restarted.acquire_publication_lease(key, "recovery")
        )

    async def test_redaction_preserves_source_lock_and_active_recovery_data(self):
        completed_key = gate_increment_store.SourceMessageKey(10000, 10001, 10002)
        active_key = gate_increment_store.SourceMessageKey(10000, 10001, 10003)
        completed_plan = gate_increment_store.GateIncrementMemberPlan(
            10004, (10005,), 10006
        )
        await self.first_store.claim(completed_key, 10004, (completed_plan,))
        await self.first_store.mark_member_completed(completed_key, 0)
        await self.first_store.finalize_operation(completed_key)
        await self.first_store.mark_moderation_logged(completed_key, (0,))
        self.assertTrue(
            await self.first_store.acquire_publication_lease(
                completed_key,
                "publisher",
            )
        )
        await self.first_store.record_result_message(
            completed_key,
            "publisher",
            10001,
            10007,
            1,
        )
        await self.first_store.claim(active_key, 10004, (completed_plan,))

        await self.first_store.redact_user_data(10004)

        completed = await self.first_store.get_operation(completed_key)
        active = await self.first_store.get_operation(active_key)
        self.assertIsNone(completed.operation.moderator_id)
        self.assertIsNone(completed.members[0].user_id)
        self.assertEqual(completed.members[0].expected_gate_role_ids, ())
        self.assertIsNone(completed.members[0].target_role_id)
        self.assertIsNone(active.operation.moderator_id)
        self.assertEqual(active.members[0].user_id, 10004)
        self.assertEqual(active.members[0].target_role_id, 10006)
        duplicate = await self.second_store.claim(completed_key, 10008, ())
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.operation.operation_id, completed.operation.operation_id)

    async def test_interrupted_operations_are_listed_for_startup_recovery(self):
        interrupted_key = gate_increment_store.SourceMessageKey(11000, 11001, 11002)
        completed_key = gate_increment_store.SourceMessageKey(11000, 11001, 11003)
        plan = gate_increment_store.GateIncrementMemberPlan(11004, (), 11005)
        await self.first_store.claim(interrupted_key, 11006, (plan,))
        await self.first_store.claim(completed_key, 11006, (plan,))
        await self.first_store.mark_member_completed(completed_key, 0)
        await self.first_store.finalize_operation(completed_key)
        await self.first_store.mark_moderation_logged(completed_key, (0,))
        self.assertTrue(
            await self.first_store.acquire_publication_lease(
                completed_key, "publisher"
            )
        )
        await self.first_store.record_result_message(
            completed_key, "publisher", 11001, 11007, 1
        )

        interrupted = await self.first_store.list_interrupted_operations()

        self.assertEqual(
            tuple(snapshot.operation.key for snapshot in interrupted),
            (interrupted_key,),
        )


if __name__ == "__main__":
    unittest.main()
