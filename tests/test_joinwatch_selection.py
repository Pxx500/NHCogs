"""Joinwatch candidate selection: which stored assignments a sweep picks up.
"""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.harness import _isolated_honeypot_modules


class JoinwatchSelectionTests(unittest.TestCase):
    def test_selects_due_joinwatch_work_in_source_order(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
                malformed_assignment = {
                    "role_id": "invalid",
                    "apply_at": "2026-07-15T11:55:00+00:00",
                }
                future_assignment = {
                    "role_id": 502,
                    "apply_at": "2026-07-15T12:05:00+00:00",
                }
                due_assignment = {
                    "role_id": "501",
                    "apply_at": "2026-07-15T11:59:00+00:00",
                    "retry_count": 2,
                }
                due_role = {
                    "role_id": 601,
                    "expires_at": "2026-07-15T11:58:00+00:00",
                }
                malformed_role = {"role_id": 602, "expires_at": "not-a-time"}
                future_role = {
                    "role_id": 603,
                    "expires_at": "2026-07-15T12:10:00+00:00",
                }

                selected = honeypot.select_due_joinwatch_assignments(
                    now=now,
                    assignments_enabled=True,
                    pending_assignments={
                        "broken": malformed_assignment,
                        "202": future_assignment,
                        "201": due_assignment,
                    },
                    pending_roles={
                        "301": due_role,
                        "broken-role": malformed_role,
                        "303": future_role,
                    },
                )

                self.assertFalse(selected.clear_assignments)
                self.assertEqual(
                    tuple(
                        (
                            action.action,
                            action.member_key,
                            action.member_id,
                            action.role_id,
                            action.due_at,
                        )
                        for action in selected.assignment_actions
                    ),
                    (
                        ("discard_assignment", "broken", None, None, None),
                        (
                            "apply_role",
                            "201",
                            201,
                            501,
                            datetime(2026, 7, 15, 11, 59, tzinfo=timezone.utc),
                        ),
                    ),
                )
                self.assertEqual(
                    tuple(
                        (
                            action.action,
                            action.member_key,
                            action.member_id,
                            action.role_id,
                            action.due_at,
                        )
                        for action in selected.role_actions
                    ),
                    (
                        (
                            "expire_role",
                            "301",
                            301,
                            601,
                            datetime(2026, 7, 15, 11, 58, tzinfo=timezone.utc),
                        ),
                        ("discard_role", "broken-role", None, None, None),
                    ),
                )
                self.assertIs(selected.assignment_actions[1].data, due_assignment)
                self.assertIs(selected.role_actions[0].data, due_role)

    def test_disabled_assignment_processing_clears_only_assignment_state(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
                due_role = {
                    "role_id": 601,
                    "expires_at": "2026-07-15T11:58:00+00:00",
                }

                selected = honeypot.select_due_joinwatch_assignments(
                    now=now,
                    assignments_enabled=False,
                    pending_assignments={
                        "201": {
                            "role_id": 501,
                            "apply_at": "2026-07-15T11:59:00+00:00",
                        }
                    },
                    pending_roles={"301": due_role},
                )

                self.assertTrue(selected.clear_assignments)
                self.assertEqual(selected.assignment_actions, ())
                self.assertEqual(
                    (
                        selected.role_actions[0].action,
                        selected.role_actions[0].member_id,
                        selected.role_actions[0].role_id,
                    ),
                    ("expire_role", 301, 601),
                )
