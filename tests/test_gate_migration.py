import gzip
import hashlib
import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "NHMisc" / "gate_migration.py"
SPEC = importlib.util.spec_from_file_location("nhmisc_gate_migration_test", MODULE_PATH)
gate_migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate_migration
SPEC.loader.exec_module(gate_migration)


class MemberMigrationPlannerTests(unittest.TestCase):
    def test_only_highest_role_in_each_legacy_category_contributes(self):
        snapshot = gate_migration.MemberSnapshot(
            user_id=42,
            username="Gate Tester",
            role_ids=(
                1348078501986828461,  # SP 1
                1348078483384958986,  # SP 3
                798700443979087892,  # MP 1
                1004822424921055233,  # MP 2
                987654321,
            ),
        )

        plan = gate_migration.plan_member(snapshot)

        self.assertEqual(plan.sp_count, 3)
        self.assertEqual(plan.mp_count, 2)
        self.assertEqual(plan.target_tier, 5)
        self.assertEqual(
            plan.target_gate_role_ids,
            frozenset(
                {
                    1437811360208781406,  # Tier 5
                    1442208051212976158,  # Singleplayer completed
                }
            ),
        )
        self.assertEqual(
            plan.duplicate_sp_role_ids,
            (1348078501986828461, 1348078483384958986),
        )
        self.assertEqual(
            plan.duplicate_mp_role_ids,
            (798700443979087892, 1004822424921055233),
        )

    def test_migration_plan_is_sorted_and_marks_only_real_changes(self):
        snapshots = (
            gate_migration.MemberSnapshot(
                user_id=20,
                username="Unexpected tier",
                role_ids=(1522017144878137385, 444),
            ),
            gate_migration.MemberSnapshot(
                user_id=10,
                username="Already tier one",
                role_ids=(798700443979087892, 333),
            ),
            gate_migration.MemberSnapshot(
                user_id=30,
                username="No gates",
                role_ids=(555,),
            ),
        )

        migration = gate_migration.plan_migration(snapshots)

        self.assertEqual(
            tuple(member.snapshot.user_id for member in migration.members),
            (10, 20, 30),
        )
        self.assertFalse(migration.members[0].changed)
        self.assertEqual(
            migration.members[0].original_gate_role_ids,
            frozenset({798700443979087892}),
        )
        self.assertTrue(migration.members[1].changed)
        self.assertEqual(
            migration.members[1].unexpected_role_ids,
            (1522017144878137385,),
        )
        self.assertFalse(migration.members[2].changed)

    def test_every_legacy_tier_pair_maps_to_the_linear_target_ladder(self):
        for sp_count in range(6):
            for mp_count in range(6):
                with self.subTest(sp_count=sp_count, mp_count=mp_count):
                    role_ids = []
                    if sp_count:
                        role_ids.append(gate_migration.LEGACY_SP_ROLE_IDS[sp_count - 1])
                    if mp_count:
                        role_ids.append(gate_migration.LEGACY_MP_ROLE_IDS[mp_count - 1])
                    snapshot = gate_migration.MemberSnapshot(
                        user_id=sp_count * 10 + mp_count,
                        username="Tier pair",
                        role_ids=tuple(role_ids),
                    )

                    plan = gate_migration.plan_member(snapshot)

                    expected_roles = set()
                    target_tier = sp_count + mp_count
                    if target_tier:
                        expected_roles.add(
                            gate_migration.TARGET_TIER_ROLE_IDS[target_tier - 1]
                        )
                    if sp_count:
                        expected_roles.add(
                            gate_migration.SINGLEPLAYER_COMPLETED_ROLE_ID
                        )
                    self.assertEqual(plan.target_tier, target_tier)
                    self.assertEqual(
                        plan.target_gate_role_ids,
                        frozenset(expected_roles),
                    )

    def test_summary_counts_memberships_changes_and_tier_distributions(self):
        migration = gate_migration.plan_migration(
            (
                gate_migration.MemberSnapshot(
                    user_id=10,
                    username="MP one",
                    role_ids=(798700443979087892, 111),
                ),
                gate_migration.MemberSnapshot(
                    user_id=20,
                    username="SP duplicates",
                    role_ids=(
                        1348078501986828461,
                        1348078483384958986,
                        222,
                    ),
                ),
                gate_migration.MemberSnapshot(
                    user_id=30,
                    username="Unexpected tier",
                    role_ids=(1522017144878137385,),
                ),
                gate_migration.MemberSnapshot(
                    user_id=40,
                    username="No gates",
                    role_ids=(),
                ),
            )
        )

        summary = gate_migration.summarize_plan(migration)

        self.assertEqual(summary.total_members, 4)
        self.assertEqual(summary.role_memberships, 6)
        self.assertEqual(summary.legacy_members, 2)
        self.assertEqual(summary.changed_members, 2)
        self.assertEqual(summary.unchanged_members, 2)
        self.assertEqual(summary.singleplayer_completed_members, 1)
        self.assertEqual(summary.duplicate_sp_members, 1)
        self.assertEqual(summary.duplicate_mp_members, 0)
        self.assertEqual(summary.unexpected_members, 1)
        self.assertEqual(summary.source_sp_tiers, (0, 0, 1, 0, 0))
        self.assertEqual(summary.source_mp_tiers, (1, 0, 0, 0, 0))
        self.assertEqual(summary.target_tiers, (1, 0, 1, 0, 0, 0, 0, 0, 0, 0))


class BackupArtifactTests(unittest.TestCase):
    def test_single_part_backup_is_deterministic_and_preserves_exact_snapshot(self):
        snapshots = (
            gate_migration.MemberSnapshot(
                user_id=20,
                username="Żółw",
                role_ids=(999, 111),
            ),
            gate_migration.MemberSnapshot(
                user_id=10,
                username="Comma, Quote \" User",
                role_ids=(333, 222),
            ),
        )

        first = gate_migration.build_backup(
            snapshots,
            guild_id=123,
            run_id="run-1",
            max_part_size=10_000,
        )
        second = gate_migration.build_backup(
            tuple(reversed(snapshots)),
            guild_id=123,
            run_id="run-1",
            max_part_size=10_000,
        )

        self.assertNotEqual(first.snapshot_sha256, "")
        self.assertEqual(first, second)
        self.assertEqual(len(first.parts), 1)
        self.assertIsNone(first.manifest)
        part = first.parts[0]
        self.assertEqual(hashlib.sha256(part.data).hexdigest(), part.sha256)
        rows = [
            json.loads(line)
            for line in gzip.decompress(part.data).decode("utf-8").splitlines()
        ]
        self.assertEqual(
            rows,
            [
                {
                    "role_ids": ["222", "333"],
                    "user_id": "10",
                    "username": "Comma, Quote \" User",
                },
                {
                    "role_ids": ["111", "999"],
                    "user_id": "20",
                    "username": "Żółw",
                },
            ],
        )

    def test_oversized_backup_is_split_with_a_verified_manifest(self):
        snapshots = tuple(
            gate_migration.MemberSnapshot(
                user_id=user_id,
                username=f"User {user_id:02d} unique payload {user_id * 7919:08x}",
                role_ids=(user_id * 101, user_id * 103, user_id * 107),
            )
            for user_id in range(1, 21)
        )

        bundle = gate_migration.build_backup(
            snapshots,
            guild_id=987,
            run_id="run-split",
            max_part_size=220,
        )

        self.assertGreater(len(bundle.parts), 1)
        self.assertTrue(all(len(part.data) <= 220 for part in bundle.parts))
        self.assertIsNotNone(bundle.manifest)
        manifest = json.loads(bundle.manifest.data)
        self.assertEqual(manifest["guild_id"], "987")
        self.assertEqual(manifest["run_id"], "run-split")
        self.assertEqual(manifest["snapshot_sha256"], bundle.snapshot_sha256)
        self.assertEqual(
            manifest["parts"],
            [
                {
                    "filename": part.filename,
                    "sha256": part.sha256,
                    "size": len(part.data),
                }
                for part in bundle.parts
            ],
        )
        self.assertEqual(
            hashlib.sha256(bundle.manifest.data).hexdigest(),
            bundle.manifest.sha256,
        )
        restored_rows = [
            json.loads(line)
            for part in bundle.parts
            for line in gzip.decompress(part.data).decode("utf-8").splitlines()
        ]
        self.assertEqual(
            [int(row["user_id"]) for row in restored_rows],
            list(range(1, 21)),
        )

    def test_backup_verification_rejects_changed_attachment_bytes(self):
        bundle = gate_migration.build_backup(
            (
                gate_migration.MemberSnapshot(
                    user_id=10,
                    username="Original",
                    role_ids=(111, 222),
                ),
            ),
            guild_id=123,
            run_id="run-verify",
            max_part_size=10_000,
        )
        corrupted_part = replace(
            bundle.parts[0],
            data=bundle.parts[0].data[:-1] + b"x",
        )
        corrupted_bundle = replace(bundle, parts=(corrupted_part,))

        with self.assertRaises(gate_migration.BackupVerificationError):
            gate_migration.verify_backup(corrupted_bundle)


if __name__ == "__main__":
    unittest.main()
