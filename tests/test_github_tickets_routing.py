from __future__ import annotations

import importlib
import sys
import types
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "NHCogs"
PACKAGE_PATH = ROOT / PACKAGE_NAME
GITHUBTICKETS_PACKAGE_NAME = f"{PACKAGE_NAME}.githubtickets"
GITHUBTICKETS_PACKAGE_PATH = PACKAGE_PATH / "githubtickets"


def _load_routing_modules():
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package

    githubtickets_package = sys.modules.get(GITHUBTICKETS_PACKAGE_NAME)
    if githubtickets_package is None:
        githubtickets_package = types.ModuleType(GITHUBTICKETS_PACKAGE_NAME)
        githubtickets_package.__path__ = [str(GITHUBTICKETS_PACKAGE_PATH)]
        sys.modules[GITHUBTICKETS_PACKAGE_NAME] = githubtickets_package

    try:
        models = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.models")
        routing = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.routing")
    except (ImportError, ModuleNotFoundError):
        return None, None
    return models, routing


models, routing_module = _load_routing_modules()


@unittest.skipIf(routing_module is None, "GitHub Tickets routing is not implemented yet")
class ReviewerSelectionTests(unittest.TestCase):
    def candidate(self, user_id: int = 1, **changes):
        facts = routing_module.CandidateFacts(
            user_id=user_id,
            is_cached_member=True,
            has_participant_role=True,
            can_manage_messages=False,
            has_profile=True,
            allows_automatic_pings=True,
            matching_category_count=1,
            was_pinged=False,
            timed_out=False,
            declined=False,
            unassigned=False,
            presence_tier=models.PresenceTier.ONLINE,
            active_assignment_count=0,
            last_ping_at=None,
        )
        return replace(facts, **changes)

    def test_rejects_each_ineligible_candidate_fact(self):
        ineligible_changes = (
            {"is_cached_member": False},
            {"has_participant_role": False, "can_manage_messages": False},
            {"has_profile": False},
            {"allows_automatic_pings": False},
            {"matching_category_count": 0},
            {"was_pinged": True},
            {"timed_out": True},
            {"declined": True},
            {"unassigned": True},
        )

        for changes in ineligible_changes:
            with self.subTest(changes=changes):
                self.assertIsNone(routing_module.select_reviewer([self.candidate(**changes)]))

        staff = self.candidate(has_participant_role=False, can_manage_messages=True)
        self.assertEqual(routing_module.select_reviewer([staff]), staff)

    def test_presence_tiers_are_ordered_online_idle_dnd_offline(self):
        candidates = (
            self.candidate(4, presence_tier=models.PresenceTier.OFFLINE),
            self.candidate(3, presence_tier=models.PresenceTier.DO_NOT_DISTURB),
            self.candidate(2, presence_tier=models.PresenceTier.IDLE),
            self.candidate(1, presence_tier=models.PresenceTier.ONLINE),
        )

        self.assertEqual(routing_module.select_reviewer(candidates).user_id, 1)

    def test_within_tier_priorities_are_lexicographic(self):
        recent = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
        old = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

        with self.subTest(priority="fewest active assignments"):
            selected = routing_module.select_reviewer(
                [
                    self.candidate(1, active_assignment_count=2, matching_category_count=3),
                    self.candidate(2, active_assignment_count=1, matching_category_count=1),
                ]
            )
            self.assertEqual(selected.user_id, 2)

        with self.subTest(priority="most category matches"):
            selected = routing_module.select_reviewer(
                [
                    self.candidate(1, matching_category_count=1),
                    self.candidate(2, matching_category_count=2),
                ]
            )
            self.assertEqual(selected.user_id, 2)

        with self.subTest(priority="longest time since last ping"):
            selected = routing_module.select_reviewer(
                [
                    self.candidate(1, last_ping_at=recent),
                    self.candidate(2, last_ping_at=old),
                ]
            )
            self.assertEqual(selected.user_id, 2)

        with self.subTest(priority="never pinged"):
            selected = routing_module.select_reviewer(
                [
                    self.candidate(1, last_ping_at=old),
                    self.candidate(2, last_ping_at=None),
                ]
            )
            self.assertEqual(selected.user_id, 2)

    def test_exact_final_ties_use_only_the_injected_chooser(self):
        choices = []

        def choose(candidates):
            choices.append(tuple(candidate.user_id for candidate in candidates))
            return candidates[-1]

        selected = routing_module.select_reviewer(
            [self.candidate(1), self.candidate(2)],
            chooser=choose,
        )

        self.assertEqual(selected.user_id, 2)
        self.assertEqual(choices, [(1, 2)])

        def unexpected_choice(_candidates):
            self.fail("chooser was called for candidates with different priority")

        selected = routing_module.select_reviewer(
            [self.candidate(1, matching_category_count=1), self.candidate(2, matching_category_count=2)],
            chooser=unexpected_choice,
        )
        self.assertEqual(selected.user_id, 2)
