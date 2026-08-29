from __future__ import annotations

import importlib
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.githubtickets_loader import isolated_githubtickets_modules


class _Reporter:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []

    async def report(self, **kwargs) -> None:
        self.reports.append(kwargs)


class _Member:
    def __init__(
        self,
        user_id: int,
        *,
        role_ids: tuple[int, ...] = (),
        manage_messages: bool = False,
    ) -> None:
        self.id = user_id
        self.roles = tuple(SimpleNamespace(id=role_id) for role_id in role_ids)
        self.guild_permissions = SimpleNamespace(manage_messages=manage_messages)


class _Guild:
    def __init__(self, members: tuple[_Member, ...]) -> None:
        self.members = {member.id: member for member in members}

    def get_member(self, user_id: int) -> _Member | None:
        return self.members.get(user_id)


def _member_is_eligible(member: _Member) -> bool:
    return member.guild_permissions.manage_messages or any(
        role.id == 99 for role in member.roles
    )


class _Bot:
    def __init__(self, guild: _Guild, reporter: _Reporter) -> None:
        self.guild = guild
        self.reporter = reporter

    def get_guild(self, guild_id: int) -> _Guild | None:
        return self.guild if guild_id == 10 else None

    def get_cog(self, name: str) -> _Reporter | None:
        return self.reporter if name == "OperationalErrors" else None


class _Store:
    def __init__(self) -> None:
        self.observed: list[object] = []
        self.observation_state = None
        self.profiles: dict[str, tuple[object, ...]] = {}

    async def observe_pull_request(self, pull_request, *, authoritative: bool = False):
        self.observed.append(pull_request)
        return SimpleNamespace(
            state=(
                self.observation_state
                if not authoritative
                else self._applied_observation_state
            ),
            pull_request=pull_request,
        )

    async def list_profiles_by_github_username(
        self,
        guild_id: int,
        github_username: str,
    ) -> tuple[object, ...]:
        if guild_id != 10:
            return ()
        return self.profiles.get(github_username.casefold(), ())


class _Coordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.claim_success = True
        self.unassign_success = True
        self.finished_ticket = None

    async def create_ticket_from_github(
        self,
        guild_id: int,
        pull_request,
        *,
        author_id: int | None,
    ) -> object:
        self.calls.append(("create", guild_id, pull_request, author_id))
        return SimpleNamespace(success=True)

    async def claim_ticket_from_github(
        self,
        repository_id: int,
        pr_number: int,
        *,
        user_id: int,
        ensure_assigned_login: str | None,
    ) -> object:
        self.calls.append(
            (
                "claim",
                repository_id,
                pr_number,
                user_id,
                ensure_assigned_login,
            )
        )
        return SimpleNamespace(success=self.claim_success)

    async def unassign_ticket_from_github(
        self,
        repository_id: int,
        pr_number: int,
        *,
        user_id: int,
    ) -> object:
        self.calls.append(("unassign", repository_id, pr_number, user_id))
        return SimpleNamespace(success=self.unassign_success)

    async def finish_ticket_from_github(
        self,
        repository_id: int,
        pr_number: int,
    ) -> object:
        self.calls.append(("finish", repository_id, pr_number))
        return SimpleNamespace(
            success=True,
            finished_ticket=self.finished_ticket,
        )

    async def prompt_draft_decision_from_github(
        self,
        repository_id: int,
        pr_number: int,
    ) -> object:
        self.calls.append(("draft", repository_id, pr_number))
        return SimpleNamespace(success=True)

    async def update_title_from_github(
        self,
        repository_id: int,
        pr_number: int,
        *,
        title: str,
    ) -> object:
        self.calls.append(("title", repository_id, pr_number, title))
        return SimpleNamespace(success=True)


class GitHubEventHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.modules_context = isolated_githubtickets_modules(Path(self.directory.name))
        self.modules = self.modules_context.__enter__()
        self.module_name = "NHCogs.githubtickets.event_handler"
        self.previous_handler_module = sys.modules.pop(self.module_name, None)
        self.event_handler = importlib.import_module(self.module_name)
        self.reporter = _Reporter()
        self.guild = _Guild(())
        self.bot = _Bot(self.guild, self.reporter)
        self.store = _Store()
        self.store.observation_state = self.modules.models.PullRequestObservationState.APPLIED
        self.store._applied_observation_state = (
            self.modules.models.PullRequestObservationState.APPLIED
        )
        self.coordinator = _Coordinator()
        self.handler = self.event_handler.GitHubEventHandler(
            self.store,
            self.coordinator,
            bot=self.bot,
            guild_id=10,
            member_is_eligible=_member_is_eligible,
        )

    async def asyncTearDown(self) -> None:
        sys.modules.pop(self.module_name, None)
        if self.previous_handler_module is not None:
            sys.modules[self.module_name] = self.previous_handler_module
        self.modules_context.__exit__(None, None, None)
        self.directory.cleanup()

    @staticmethod
    def payload(
        *,
        action: str,
        draft: bool = False,
        state: str = "open",
        labels: tuple[str, ...] = ("discord-ticket",),
        assignees: tuple[str, ...] = (),
        author_login: str = "author",
        merged: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": action,
            "repository": {"id": 100, "full_name": "NewHorizons/NHCogs"},
            "pull_request": {
                "id": 700,
                "number": 7,
                "title": "GitHub ticket",
                "html_url": "https://github.com/NewHorizons/NHCogs/pull/7",
                "state": state,
                "draft": draft,
                "merged": merged,
                "updated_at": "2026-08-29T10:20:30Z",
                "user": {"id": 900, "login": author_login},
                "labels": [{"name": label} for label in labels],
                "assignees": [{"login": login} for login in assignees],
            },
        }
        if action in {"labeled", "unlabeled"}:
            payload["label"] = {"name": "discord-ticket"}
        return payload

    def delivery(
        self,
        *,
        event: str,
        action: str,
        payload: dict[str, object] | None = None,
        raw_body: bytes | None = None,
    ):
        body = raw_body
        if body is None:
            body = json.dumps(payload or {}).encode()
        return self.modules.models.GitHubDelivery(
            delivery_guid=f"{event}-{action}",
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

    def map_profile(self, login: str, *user_ids: int) -> None:
        self.store.profiles[login.casefold()] = tuple(
            SimpleNamespace(user_id=user_id) for user_id in user_ids
        )

    async def test_unknown_event_is_ignored_without_side_effects(self) -> None:
        disposition = await self.handler(
            self.delivery(
                event="issues",
                action="opened",
                raw_body=b'{"malformed":',
            )
        )

        self.assertIs(disposition, self.modules.runtime.DeliveryDisposition.IGNORED)
        self.assertEqual(self.store.observed, [])
        self.assertEqual(self.coordinator.calls, [])

    async def test_stale_pull_request_event_is_observed_without_domain_transition(self) -> None:
        self.store.observation_state = self.modules.models.PullRequestObservationState.STALE
        self.map_profile("participant", 2)
        self.guild.members[2] = _Member(2, role_ids=(99,))

        disposition = await self.handler(
            self.delivery(
                event="pull_request",
                action="assigned",
                payload=self.payload(
                    action="assigned",
                    assignees=("participant",),
                ),
            )
        )

        self.assertIs(disposition, self.modules.runtime.DeliveryDisposition.PROCESSED)
        self.assertEqual(self.coordinator.calls, [])

    async def test_equal_timestamp_conflict_refreshes_once_before_transition(self) -> None:
        delivery = self.delivery(
            event="pull_request",
            action="edited",
            payload={
                **self.payload(action="edited"),
                "changes": {"title": {"from": "Old title"}},
            },
        )
        parsed = self.modules.events.parse_delivery(delivery)
        authoritative = replace(
            parsed.pull_request,
            title="Authoritative title",
        )
        refresh_calls: list[object] = []

        async def refresh_pull_request(pull_request):
            refresh_calls.append(pull_request)
            return authoritative

        self.store.observation_state = self.modules.models.PullRequestObservationState.CONFLICT
        handler = self.event_handler.GitHubEventHandler(
            self.store,
            self.coordinator,
            bot=self.bot,
            guild_id=10,
            member_is_eligible=_member_is_eligible,
            refresh_pull_request=refresh_pull_request,
        )

        await handler(delivery)

        self.assertEqual(refresh_calls, [parsed.pull_request])
        self.assertEqual(len(self.store.observed), 2)
        self.assertEqual(
            self.coordinator.calls,
            [("title", 100, 7, "Authoritative title")],
        )

    async def test_discord_ticket_label_creates_only_for_ready_pull_request(self) -> None:
        self.map_profile("author", 300)
        self.guild.members[300] = _Member(300, role_ids=(99,))

        ready = await self.handler(
            self.delivery(
                event="pull_request",
                action="labeled",
                payload=self.payload(action="labeled"),
            )
        )
        draft = await self.handler(
            self.delivery(
                event="pull_request",
                action="labeled",
                payload=self.payload(action="labeled", draft=True),
            )
        )

        self.assertIs(ready, self.modules.runtime.DeliveryDisposition.PROCESSED)
        self.assertIs(draft, self.modules.runtime.DeliveryDisposition.PROCESSED)
        self.assertEqual(len(self.store.observed), 2)
        self.assertEqual(len(self.coordinator.calls), 1)
        kind, guild_id, pull_request, author_id = self.coordinator.calls[0]
        self.assertEqual(kind, "create")
        self.assertEqual(guild_id, 10)
        self.assertEqual(pull_request.pr_number, 7)
        self.assertEqual(author_id, 300)

    async def test_ineligible_profile_is_not_used_as_github_ticket_author(self) -> None:
        self.map_profile("author", 300)
        self.guild.members[300] = _Member(300)

        await self.handler(
            self.delivery(
                event="pull_request",
                action="labeled",
                payload=self.payload(action="labeled"),
            )
        )

        self.assertEqual(len(self.coordinator.calls), 1)
        self.assertEqual(self.coordinator.calls[0][0], "create")
        self.assertIsNone(self.coordinator.calls[0][3])

    async def test_ready_and_closed_actions_use_explicit_lifecycle_methods(self) -> None:
        await self.handler(
            self.delivery(
                event="pull_request",
                action="ready_for_review",
                payload=self.payload(action="ready_for_review"),
            )
        )
        await self.handler(
            self.delivery(
                event="pull_request",
                action="ready_for_review",
                payload=self.payload(
                    action="ready_for_review",
                    labels=(),
                ),
            )
        )
        for merged in (False, True):
            with self.subTest(merged=merged):
                await self.handler(
                    self.delivery(
                        event="pull_request",
                        action="closed",
                        payload=self.payload(
                            action="closed",
                            state="closed",
                            merged=merged,
                        ),
                    )
                )

        self.assertEqual(len(self.store.observed), 4)
        self.assertEqual(
            [call[0] for call in self.coordinator.calls],
            ["create", "finish", "finish"],
        )
        self.assertEqual(self.coordinator.calls[0][3], None)
        self.assertEqual(
            self.coordinator.calls[1:],
            [("finish", 100, 7), ("finish", 100, 7)],
        )

    async def test_converted_draft_prompts_and_closed_finish_logging_is_best_effort(
        self,
    ) -> None:
        finished_ticket = object()
        self.coordinator.finished_ticket = finished_ticket

        async def failed_log(ticket) -> None:
            self.assertIs(ticket, finished_ticket)
            raise RuntimeError("log unavailable")

        handler = self.event_handler.GitHubEventHandler(
            self.store,
            self.coordinator,
            bot=self.bot,
            guild_id=10,
            member_is_eligible=_member_is_eligible,
            ticket_finished=failed_log,
        )

        draft_result = await handler(
            self.delivery(
                event="pull_request",
                action="converted_to_draft",
                payload=self.payload(
                    action="converted_to_draft",
                    draft=True,
                ),
            )
        )
        closed_result = await handler(
            self.delivery(
                event="pull_request",
                action="closed",
                payload=self.payload(
                    action="closed",
                    state="closed",
                ),
            )
        )

        self.assertEqual(
            [call[0] for call in self.coordinator.calls],
            ["draft", "finish"],
        )
        self.assertIs(draft_result, self.modules.runtime.DeliveryDisposition.PROCESSED)
        self.assertIs(closed_result, self.modules.runtime.DeliveryDisposition.PROCESSED)

    async def test_explicit_title_edit_updates_the_bound_ticket_once(self) -> None:
        payload = self.payload(action="edited")
        payload["changes"] = {"title": {"from": "Old title"}}

        await self.handler(
            self.delivery(
                event="pull_request",
                action="edited",
                payload=payload,
            )
        )

        self.assertEqual(
            self.coordinator.calls,
            [("title", 100, 7, "GitHub ticket")],
        )

    async def test_assigned_claims_first_eligible_non_author_without_outbound_echo(
        self,
    ) -> None:
        self.map_profile("outsider", 1)
        self.map_profile("participant", 2)
        self.map_profile("staff", 3)
        self.map_profile("author", 4)
        self.guild.members.update(
            {
                1: _Member(1),
                2: _Member(2, role_ids=(99,)),
                3: _Member(3, manage_messages=True),
                4: _Member(4, role_ids=(99,)),
            }
        )
        cases = (
            (("outsider", "participant", "staff"), 2),
            (("outsider", "staff"), 3),
            (("author", "participant"), 2),
        )
        for assignees, expected_user_id in cases:
            with self.subTest(assignees=assignees):
                payload = self.payload(
                    action="assigned",
                    assignees=assignees,
                )
                payload["assignee"] = {"login": assignees[-1]}
                await self.handler(
                    self.delivery(
                        event="pull_request",
                        action="assigned",
                        payload=payload,
                    )
                )
                self.assertEqual(
                    self.coordinator.calls[-1],
                    ("claim", 100, 7, expected_user_id, None),
                )

        self.assertEqual(len(self.coordinator.calls), 3)

    async def test_unsettled_assignment_is_deferred_instead_of_acknowledged(self) -> None:
        self.map_profile("participant", 2)
        self.guild.members[2] = _Member(2, role_ids=(99,))
        self.coordinator.claim_success = False

        with self.assertRaises(self.event_handler.GitHubEventTransitionDeferred):
            await self.handler(
                self.delivery(
                    event="pull_request",
                    action="assigned",
                    payload=self.payload(
                        action="assigned",
                        assignees=("participant",),
                    ),
                )
            )

    async def test_unassigned_releases_matching_claimant_then_considers_remaining_assignees(
        self,
    ) -> None:
        self.map_profile("removed", 5)
        self.map_profile("participant", 2)
        self.guild.members.update(
            {
                2: _Member(2, role_ids=(99,)),
                5: _Member(5),
            }
        )
        payload = self.payload(
            action="unassigned",
            assignees=("participant",),
        )
        payload["assignee"] = {"login": "removed"}

        await self.handler(
            self.delivery(
                event="pull_request",
                action="unassigned",
                payload=payload,
            )
        )

        self.assertEqual(
            self.coordinator.calls,
            [
                ("unassign", 100, 7, 5),
                ("claim", 100, 7, 2, None),
            ],
        )

    async def test_actionable_reviews_claim_and_only_request_missing_github_assignment(
        self,
    ) -> None:
        self.map_profile("reviewer", 2)
        self.map_profile("staff", 3)
        self.map_profile("author", 4)
        self.guild.members.update(
            {
                2: _Member(2, role_ids=(99,)),
                3: _Member(3, manage_messages=True),
                4: _Member(4, role_ids=(99,)),
            }
        )
        cases = (
            ("approved", "reviewer", (), ("claim", 100, 7, 2, "reviewer")),
            (
                "changes_requested",
                "staff",
                ("staff",),
                ("claim", 100, 7, 3, None),
            ),
            ("commented", "reviewer", (), None),
            ("approved", "author", (), None),
            ("approved", None, (), None),
            ("approved", "unmapped", (), None),
        )
        for state, reviewer, assignees, expected in cases:
            with self.subTest(state=state, reviewer=reviewer):
                payload = self.payload(
                    action="submitted",
                    assignees=assignees,
                )
                payload["review"] = {
                    "state": state,
                    "user": {"login": reviewer} if reviewer is not None else None,
                }
                before = len(self.coordinator.calls)
                await self.handler(
                    self.delivery(
                        event="pull_request_review",
                        action="submitted",
                        payload=payload,
                    )
                )
                if expected is None:
                    self.assertEqual(len(self.coordinator.calls), before)
                else:
                    self.assertEqual(self.coordinator.calls[-1], expected)

        self.assertEqual(len(self.coordinator.calls), 2)
        self.assertEqual(self.reporter.reports, [])

    async def test_ambiguous_cached_mapping_reports_and_makes_no_ownership_change(
        self,
    ) -> None:
        self.map_profile("ambiguous", 10, 11)
        self.map_profile("participant", 2)
        self.guild.members.update(
            {
                2: _Member(2, role_ids=(99,)),
                10: _Member(10, role_ids=(99,)),
                11: _Member(11, manage_messages=True),
            }
        )
        payload = self.payload(
            action="assigned",
            assignees=("ambiguous", "participant"),
        )
        payload["assignee"] = {"login": "participant"}

        await self.handler(
            self.delivery(
                event="pull_request",
                action="assigned",
                payload=payload,
            )
        )

        self.assertEqual(self.coordinator.calls, [])
        self.assertEqual(len(self.reporter.reports), 1)
        self.assertEqual(self.reporter.reports[0]["source"], "GitHubTickets")
        self.assertEqual(
            self.reporter.reports[0]["action"],
            "resolve GitHub identity",
        )
        self.assertIn("ambiguous", str(self.reporter.reports[0]["error"]))


if __name__ == "__main__":
    unittest.main()
