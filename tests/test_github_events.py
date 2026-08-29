from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.githubtickets_loader import isolated_githubtickets_modules


class GitHubEventParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.modules_context = isolated_githubtickets_modules(Path(self.directory.name))
        self.modules = self.modules_context.__enter__()

    def tearDown(self) -> None:
        self.modules_context.__exit__(None, None, None)
        self.directory.cleanup()

    @staticmethod
    def pull_request_payload() -> dict[str, object]:
        return {
            "action": "opened",
            "repository": {
                "id": 100,
                "full_name": "NewHorizons/NHCogs",
            },
            "pull_request": {
                "id": 700,
                "number": 7,
                "title": "Add GitHub App integration",
                "html_url": "https://github.com/NewHorizons/NHCogs/pull/7",
                "state": "open",
                "draft": False,
                "merged": False,
                "updated_at": "2026-08-29T10:20:30Z",
                "user": {"id": 900, "login": "octocat"},
                "labels": [
                    {"name": "discord-ticket"},
                    {"name": "type: bug"},
                ],
                "assignees": [{"login": "reviewer"}],
            },
        }

    def delivery(
        self,
        *,
        event: str,
        action: str | None,
        payload: dict[str, object] | None = None,
        raw_body: bytes | None = None,
    ):
        body = raw_body
        if body is None:
            body = json.dumps(payload or {}).encode()
        return self.modules.models.GitHubDelivery(
            delivery_guid="delivery-guid",
            github_delivery_id=123,
            event=event,
            action=action,
            installation_id=456,
            repository_id=100,
            pr_number=7,
            received_at=datetime(2026, 8, 29, 10, 21, tzinfo=timezone.utc),
            state=self.modules.models.GitHubDeliveryState.PROCESSING,
            attempts=1,
            next_attempt_at=None,
            processing_started_at=datetime(
                2026,
                8,
                29,
                10,
                21,
                tzinfo=timezone.utc,
            ),
            completed_at=None,
            error_summary=None,
            raw_body=body,
        )

    def test_supported_pull_request_actions_return_typed_events(self) -> None:
        cases = (
            ("labeled", {"label": {"name": "priority: high"}}, "priority: high", None, False),
            ("ready_for_review", {}, None, None, False),
            ("opened", {}, None, None, False),
            ("reopened", {}, None, None, False),
            ("unlabeled", {"label": {"name": "stale"}}, "stale", None, False),
            ("edited", {"changes": {"title": {"from": "Old title"}}}, None, None, True),
            ("synchronize", {}, None, None, False),
            ("converted_to_draft", {}, None, None, False),
            ("closed", {}, None, None, False),
            ("assigned", {"assignee": {"login": "reviewer"}}, None, "reviewer", False),
            (
                "unassigned",
                {"assignee": {"login": "former-reviewer"}},
                None,
                "former-reviewer",
                False,
            ),
        )
        for action, extra, expected_label, expected_assignee, title_changed in cases:
            with self.subTest(action=action):
                payload = deepcopy(self.pull_request_payload())
                payload["action"] = action
                payload.update(extra)
                pull_request = payload["pull_request"]
                assert isinstance(pull_request, dict)
                if action == "closed":
                    pull_request["state"] = "closed"
                    pull_request["merged"] = True
                elif action == "converted_to_draft":
                    pull_request["draft"] = True

                parsed = self.modules.events.parse_delivery(
                    self.delivery(
                        event="pull_request",
                        action=action,
                        payload=payload,
                    )
                )

                self.assertIsInstance(parsed, self.modules.events.PullRequestEvent)
                self.assertEqual(parsed.action, action)
                self.assertEqual(parsed.label, expected_label)
                self.assertEqual(parsed.assignee_login, expected_assignee)
                self.assertEqual(parsed.title_changed, title_changed)
                snapshot = parsed.pull_request
                self.assertEqual(snapshot.repository_id, 100)
                self.assertEqual(snapshot.pr_number, 7)
                self.assertEqual(snapshot.github_pr_id, 700)
                self.assertEqual(snapshot.github_author_id, 900)
                self.assertEqual(snapshot.repository_full_name, "NewHorizons/NHCogs")
                self.assertEqual(
                    snapshot.url,
                    "https://github.com/NewHorizons/NHCogs/pull/7",
                )
                self.assertEqual(snapshot.title, "Add GitHub App integration")
                self.assertEqual(snapshot.github_author_login, "octocat")
                self.assertEqual(snapshot.draft, action == "converted_to_draft")
                self.assertEqual(snapshot.open, action != "closed")
                self.assertEqual(snapshot.labels, ("discord-ticket", "type: bug"))
                self.assertEqual(parsed.assignee_logins, ("reviewer",))
                self.assertEqual(
                    snapshot.github_updated_at,
                    datetime(2026, 8, 29, 10, 20, 30, tzinfo=timezone.utc),
                )
                self.assertEqual(snapshot.last_processed_action, action)

    def test_submitted_reviews_return_normalized_typed_events(self) -> None:
        cases = (
            (" APPROVED ", "approved"),
            ("CHANGES_REQUESTED", "changes_requested"),
            ("commented", "commented"),
        )
        for raw_state, expected_state in cases:
            with self.subTest(state=raw_state):
                payload = self.pull_request_payload()
                payload["action"] = "submitted"
                payload["review"] = {
                    "state": raw_state,
                    "user": {"login": "ReviewerOne"},
                }

                parsed = self.modules.events.parse_delivery(
                    self.delivery(
                        event="pull_request_review",
                        action="submitted",
                        payload=payload,
                    )
                )

                self.assertIsInstance(
                    parsed,
                    self.modules.events.PullRequestReviewEvent,
                )
                self.assertEqual(parsed.state, expected_state)
                self.assertEqual(parsed.reviewer_login, "ReviewerOne")
                self.assertEqual(parsed.assignee_logins, ("reviewer",))
                self.assertEqual(parsed.pull_request.pr_number, 7)
                self.assertEqual(
                    parsed.pull_request.last_processed_action,
                    "submitted",
                )

    def test_optional_webhook_fields_do_not_break_known_actions(self) -> None:
        cases = (
            ("labeled", "label", None, None, False),
            ("assigned", "assignee", None, None, False),
            ("edited", "changes", None, None, False),
        )
        for action, omitted, expected_label, expected_assignee, title_changed in cases:
            with self.subTest(action=action, omitted=omitted):
                payload = self.pull_request_payload()
                payload["action"] = action
                payload.pop(omitted, None)
                pull_request = payload["pull_request"]
                assert isinstance(pull_request, dict)
                pull_request.pop("draft")
                pull_request.pop("merged")

                parsed = self.modules.events.parse_delivery(
                    self.delivery(
                        event="pull_request",
                        action=action,
                        payload=payload,
                    )
                )

                self.assertEqual(parsed.label, expected_label)
                self.assertEqual(parsed.assignee_login, expected_assignee)
                self.assertEqual(parsed.title_changed, title_changed)
                self.assertFalse(parsed.pull_request.draft)

        payload = self.pull_request_payload()
        payload["action"] = "edited"
        payload["changes"] = {"body": {"from": "old body"}}
        parsed = self.modules.events.parse_delivery(
            self.delivery(
                event="pull_request",
                action="edited",
                payload=payload,
            )
        )
        self.assertFalse(parsed.title_changed)

        payload = self.pull_request_payload()
        payload["action"] = "opened"
        pull_request = payload["pull_request"]
        assert isinstance(pull_request, dict)
        pull_request.pop("assignees")
        parsed = self.modules.events.parse_delivery(
            self.delivery(
                event="pull_request",
                action="opened",
                payload=payload,
            )
        )
        self.assertEqual(parsed.action, "opened")

    def test_submitted_review_without_user_remains_an_ignorable_event(self) -> None:
        payload = self.pull_request_payload()
        payload["action"] = "submitted"
        payload["review"] = {
            "state": "approved",
            "user": None,
        }

        parsed = self.modules.events.parse_delivery(
            self.delivery(
                event="pull_request_review",
                action="submitted",
                payload=payload,
            )
        )

        self.assertIsInstance(parsed, self.modules.events.PullRequestReviewEvent)
        self.assertIsNone(parsed.reviewer_login)

    def test_known_malformed_deliveries_raise_one_safe_error(self) -> None:
        missing_repository = self.pull_request_payload()
        missing_repository.pop("repository")
        missing_author_id = self.pull_request_payload()
        pull_request = missing_author_id["pull_request"]
        assert isinstance(pull_request, dict)
        author = pull_request["user"]
        assert isinstance(author, dict)
        author.pop("id")
        invalid_updated_at = self.pull_request_payload()
        pull_request = invalid_updated_at["pull_request"]
        assert isinstance(pull_request, dict)
        pull_request["updated_at"] = "private-secret"
        invalid_labels = self.pull_request_payload()
        pull_request = invalid_labels["pull_request"]
        assert isinstance(pull_request, dict)
        pull_request["labels"] = "private-secret"
        action_mismatch = self.pull_request_payload()
        action_mismatch["action"] = "closed"
        cases = (
            self.delivery(
                event="pull_request",
                action="opened",
                raw_body=b'{"private-secret":',
            ),
            self.delivery(
                event="pull_request",
                action="opened",
                payload=missing_repository,
            ),
            self.delivery(
                event="pull_request",
                action="opened",
                payload=missing_author_id,
            ),
            self.delivery(
                event="pull_request",
                action="opened",
                payload=invalid_updated_at,
            ),
            self.delivery(
                event="pull_request",
                action="opened",
                payload=invalid_labels,
            ),
            self.delivery(
                event="pull_request",
                action="opened",
                payload=action_mismatch,
            ),
        )
        for delivery in cases:
            with self.subTest(event=delivery.event, body=delivery.raw_body):
                with self.assertRaises(self.modules.events.InvalidGitHubDelivery) as raised:
                    self.modules.events.parse_delivery(delivery)
                self.assertEqual(
                    str(raised.exception),
                    "GitHub delivery payload is invalid",
                )
                self.assertNotIn("private-secret", str(raised.exception))

    def test_unknown_events_and_actions_are_ignored_before_payload_parsing(self) -> None:
        cases = (
            ("issues", "opened"),
            ("pull_request", "auto_merge_enabled"),
            ("pull_request_review", "dismissed"),
        )
        for event, action in cases:
            with self.subTest(event=event, action=action):
                parsed = self.modules.events.parse_delivery(
                    self.delivery(
                        event=event,
                        action=action,
                        raw_body=b'{"private-secret":',
                    )
                )
                self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
