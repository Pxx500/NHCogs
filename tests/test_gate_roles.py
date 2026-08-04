from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "NHMisc" / "gate_roles.py"
SPEC = importlib.util.spec_from_file_location("_gate_roles", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Gate role module")
gate_roles = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate_roles
SPEC.loader.exec_module(gate_roles)


class GateRolePlanningTests(unittest.TestCase):
    def test_member_without_gate_role_advances_to_gate_one(self):
        transition = gate_roles.plan_gate_transition((10, 20))

        self.assertIsNone(transition.current_tier)
        self.assertEqual(transition.target_tier, 1)
        self.assertEqual(
            transition.target_role_id,
            gate_roles.GATE_TIER_ROLE_IDS[0],
        )

    def test_each_non_maximum_gate_role_advances_exactly_one_tier(self):
        for tier, role_id in enumerate(
            gate_roles.GATE_TIER_ROLE_IDS[:-1], start=1
        ):
            with self.subTest(tier=tier):
                transition = gate_roles.plan_gate_transition((role_id,))

                self.assertEqual(transition.current_tier, tier)
                self.assertEqual(transition.target_tier, tier + 1)
                self.assertEqual(
                    transition.target_role_id,
                    gate_roles.GATE_TIER_ROLE_IDS[tier],
                )

    def test_gate_six_is_visible_but_not_incrementable(self):
        transition = gate_roles.plan_gate_transition(
            (gate_roles.GATE_TIER_ROLE_IDS[-1],)
        )

        self.assertEqual(transition.current_tier, 6)
        self.assertIsNone(transition.target_tier)
        self.assertIsNone(transition.target_role_id)

    def test_duplicate_gate_roles_use_the_highest_tier(self):
        transition = gate_roles.plan_gate_transition(
            (
                gate_roles.GATE_TIER_ROLE_IDS[1],
                10,
                gate_roles.GATE_TIER_ROLE_IDS[3],
            )
        )

        self.assertEqual(
            transition.current_role_ids,
            (
                gate_roles.GATE_TIER_ROLE_IDS[1],
                gate_roles.GATE_TIER_ROLE_IDS[3],
            ),
        )
        self.assertEqual(transition.current_tier, 4)
        self.assertEqual(transition.target_tier, 5)
        self.assertTrue(transition.duplicate_roles)

    def test_desired_roles_replace_all_gate_roles_and_preserve_everything_else(self):
        unrelated_role_id = 10
        current_role_ids = (
            1,
            gate_roles.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            gate_roles.GATE_TIER_ROLE_IDS[0],
            unrelated_role_id,
            gate_roles.GATE_TIER_ROLE_IDS[2],
        )
        transition = gate_roles.plan_gate_transition(current_role_ids)

        desired_role_ids = gate_roles.build_desired_role_ids(
            current_role_ids, transition
        )

        self.assertEqual(
            desired_role_ids,
            (
                1,
                gate_roles.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
                unrelated_role_id,
                gate_roles.GATE_TIER_ROLE_IDS[3],
            ),
        )

    def test_fixed_recovery_target_does_not_recalculate_the_next_tier(self):
        unrelated_role_id = 10
        fixed_target = gate_roles.GATE_TIER_ROLE_IDS[4]

        desired_role_ids = gate_roles.build_role_ids_for_target(
            (
                unrelated_role_id,
                gate_roles.GATE_TIER_ROLE_IDS[1],
            ),
            fixed_target,
        )

        self.assertEqual(desired_role_ids, (unrelated_role_id, fixed_target))


if __name__ == "__main__":
    unittest.main()
