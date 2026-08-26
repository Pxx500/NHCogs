from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "NHCogs"
PACKAGE_PATH = ROOT / PACKAGE_NAME
GITHUBTICKETS_PACKAGE_NAME = f"{PACKAGE_NAME}.githubtickets"
GITHUBTICKETS_PACKAGE_PATH = PACKAGE_PATH / "githubtickets"


def _load_modules():
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

    modules = {}
    for name in ("models", "settings", "store", "projection", "coordinator"):
        try:
            modules[name] = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.{name}")
        except (ImportError, ModuleNotFoundError):
            modules[name] = None
    return modules


modules = _load_modules()
models = modules["models"]
settings_module = modules["settings"]
store_module = modules["store"]
projection_module = modules["projection"]
coordinator_module = modules["coordinator"]


class FakeProjection:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.next_message_id = 300
        self.next_thread_id = 400
        self.ping_error: Exception | None = None
        self.not_found_operations: set[str] = set()
        self.errors: dict[str, Exception] = {}

    async def send_ticket(self, ticket, *, reviewer_github=None):
        self.calls.append(("send_ticket", ticket.ticket_id, reviewer_github))
        message_id = self.next_message_id
        self.next_message_id += 1
        return message_id

    async def create_thread(self, ticket, message_id):
        self.calls.append(("create_thread", ticket.ticket_id, message_id))
        if error := self.errors.get("create_thread"):
            raise error
        thread_id = self.next_thread_id
        self.next_thread_id += 1
        return thread_id

    async def edit_ticket(self, ticket, *, reviewer_github=None):
        self.calls.append(
            ("edit_ticket", ticket.ticket_id, ticket.state, reviewer_github)
        )
        if "edit_ticket" in self.not_found_operations:
            raise projection_module.ProjectionNotFound
        if error := self.errors.get("edit_ticket"):
            raise error

    async def ping_reviewer(self, thread_id, target_user_id, automatic):
        self.calls.append(("ping_reviewer", thread_id, target_user_id, automatic))
        if self.ping_error is not None:
            raise self.ping_error

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("delete_message", channel_id, message_id))
        if "delete_message" in self.not_found_operations:
            raise projection_module.ProjectionNotFound
        if error := self.errors.get("delete_message"):
            raise error

    async def delete_thread(self, thread_id):
        self.calls.append(("delete_thread", thread_id))
        if "delete_thread" in self.not_found_operations:
            raise projection_module.ProjectionNotFound


class TicketCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(
            coordinator_module,
            "the command-oriented TicketCoordinator interface is missing",
        )
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "githubtickets.sqlite"
        self.store = store_module.GitHubTicketsStore(self.path)
        await self.store.initialize()
        self.now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
        self.category = await self.store.add_category(10, "rendering", self.now)
        self.projection = FakeProjection()
        self.settings = settings_module.GuildSettings.from_mapping(
            {
                "ticket_channel_id": 20,
                "participant_role_ids": [99],
                "protection_seconds": 10,
                "volunteer_seconds": 120,
                "max_pings": 3,
            }
        )

        async def get_settings(_guild_id):
            return self.settings

        self.get_settings = get_settings

        self.candidates = ()

        async def get_candidates(_ticket):
            return self.candidates

        self.get_candidates = get_candidates

        self.wake_count = 0

        def wake_deadlines():
            self.wake_count += 1

        self.wake_deadlines = wake_deadlines

        self.coordinator = coordinator_module.TicketCoordinator(
            self.store,
            self.projection,
            get_settings=self.get_settings,
            get_candidates=self.get_candidates,
            wake_deadlines=self.wake_deadlines,
            clock=lambda: self.now,
        )

    def actor(self, user_id=100, *, participant=True, staff=False):
        return coordinator_module.TicketActor(
            user_id=user_id,
            is_participant=participant,
            can_manage_messages=staff,
        )

    def request(self, routing_mode=None, direct_target_id=None):
        return coordinator_module.TicketRequest(
            guild_id=10,
            pr_title="Improve rendering",
            pr_url="https://example.test/pull/1",
            category_display="rendering",
            routing_mode=routing_mode or models.RoutingMode.AUTOMATIC,
            direct_target_id=direct_target_id,
            category_ids=(self.category.category_id,),
        )

    async def create_active(self, *, routing_mode=None, direct_target_id=None):
        result = await self.coordinator.create_ticket(
            self.request(routing_mode, direct_target_id),
            self.actor(),
        )
        self.assertTrue(result.success)
        return (await self.store.list_active_tickets())[-1]

    def candidate(self, user_id=500, *, presence=None):
        routing_module = importlib.import_module(
            f"{GITHUBTICKETS_PACKAGE_NAME}.routing"
        )
        return routing_module.CandidateFacts(
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
            presence_tier=presence or models.PresenceTier.ONLINE,
            active_assignment_count=0,
            last_ping_at=None,
        )

    async def test_create_enforces_participant_permission_and_publishes_once(self):
        rejected = await self.coordinator.create_ticket(
            self.request(), self.actor(participant=False)
        )

        self.assertFalse(rejected.success)
        self.assertEqual(rejected.response, "You cannot use this action")
        self.assertEqual(self.projection.calls, [])
        self.assertEqual(await self.store.list_active_tickets(), ())

        accepted = await self.coordinator.create_ticket(self.request(), self.actor(staff=True))

        self.assertTrue(accepted.success)
        self.assertIsNone(accepted.response)
        tickets = await self.store.list_active_tickets()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].message_id, 300)
        self.assertEqual(tickets[0].thread_id, 400)
        self.assertEqual(tickets[0].next_action, models.NextAction.AUTOMATIC_PING)
        self.assertEqual(
            self.projection.calls,
            [("send_ticket", 1, None), ("create_thread", 1, 300)],
        )
        self.assertEqual(self.wake_count, 1)

    async def test_creation_retains_known_message_when_cleanup_cannot_settle(self):
        self.projection.errors["create_thread"] = RuntimeError("controlled thread failure")
        self.projection.errors["delete_message"] = RuntimeError("controlled cleanup failure")

        result = await self.coordinator.create_ticket(self.request(), self.actor())

        self.assertFalse(result.success)
        self.assertEqual(result.response, "Could not create the ticket")
        creating = await self.store.get_ticket(1)
        self.assertEqual(creating.state, models.TicketState.CREATING)
        self.assertEqual(creating.message_id, 300)
        self.assertIsNone(creating.thread_id)

    async def test_creation_returns_accepted_failure_for_whitespace_native_fields(self):
        request = replace(self.request(), pr_title="   ")

        result = await self.coordinator.create_ticket(request, self.actor())

        self.assertFalse(result.success)
        self.assertEqual(result.response, "Could not create the ticket")
        self.assertEqual(await self.store.list_active_tickets(), ())
        self.assertEqual(self.projection.calls, [])

    async def test_claim_permissions_and_first_claim_wins(self):
        await self.store.save_profile(
            guild_id=10,
            user_id=200,
            github_username="reviewer-login",
            category_ids=(self.category.category_id,),
            automatic_pings=True,
            updated_at=self.now,
        )
        ticket = await self.create_active(
            routing_mode=models.RoutingMode.DIRECT_WAIT,
            direct_target_id=200,
        )
        self.projection.calls.clear()

        rejected = await self.coordinator.claim(
            ticket.ticket_id,
            self.actor(201, participant=False),
        )
        self.assertEqual(rejected.response, "You cannot use this action")

        first, second = await asyncio.gather(
            self.coordinator.claim(
                ticket.ticket_id,
                self.actor(200, participant=False),
            ),
            self.coordinator.claim(ticket.ticket_id, self.actor(202)),
        )

        self.assertTrue(first.success)
        self.assertFalse(second.success)
        self.assertEqual(second.response, "This ticket has already been claimed")
        claimed = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(claimed.assignee_id, 200)
        self.assertEqual(
            self.projection.calls,
            [
                (
                    "edit_ticket",
                    ticket.ticket_id,
                    models.TicketState.CLAIMED,
                    "reviewer-login",
                )
            ],
        )

    async def test_non_target_decline_is_silent_sql_only_and_idempotent(self):
        ticket = await self.create_active()
        self.projection.calls.clear()

        first = await self.coordinator.decline(ticket.ticket_id, self.actor(300))
        repeated = await self.coordinator.decline(ticket.ticket_id, self.actor(300))

        self.assertTrue(first.success)
        self.assertTrue(repeated.success)
        self.assertEqual(self.projection.calls, [])
        exclusions = await self.store.list_exclusions(ticket.ticket_id)
        self.assertEqual(
            [(item.user_id, item.reason) for item in exclusions],
            [(300, models.ExclusionReason.DECLINED)],
        )

    async def test_assignee_keeps_unassign_permission_after_role_loss(self):
        ticket = await self.create_active()
        await self.coordinator.claim(ticket.ticket_id, self.actor(200))
        self.projection.calls.clear()

        result = await self.coordinator.unassign(
            ticket.ticket_id,
            self.actor(200, participant=False),
        )

        self.assertTrue(result.success)
        reopened = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(reopened.state, models.TicketState.OPEN)
        self.assertIsNone(reopened.assignee_id)
        self.assertEqual(reopened.next_action, models.NextAction.AUTOMATIC_PING)
        self.assertEqual(reopened.next_action_at, self.now.replace(second=10))
        self.assertEqual(
            self.projection.calls,
            [("edit_ticket", ticket.ticket_id, models.TicketState.OPEN, None)],
        )

    async def test_claim_projection_recovers_durably_after_transient_edit_failure(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.projection.calls.clear()
        self.projection.errors["edit_ticket"] = RuntimeError("controlled edit failure")

        failed = await self.coordinator.claim(ticket.ticket_id, self.actor(200))

        self.assertFalse(failed.success)
        claimed = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(claimed.state, models.TicketState.CLAIMED)
        retry_at = await self.store.nearest_deadline()
        self.assertGreater(retry_at, self.now)

        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        self.assertIn(ticket.ticket_id, await reopened.due_ticket_ids(retry_at))
        self.now = retry_at
        self.projection.errors.pop("edit_ticket")
        restarted = coordinator_module.TicketCoordinator(
            reopened,
            self.projection,
            get_settings=self.get_settings,
            get_candidates=self.get_candidates,
            wake_deadlines=self.wake_deadlines,
            clock=lambda: self.now,
        )

        recovered = await restarted.process_due(ticket.ticket_id)

        self.assertTrue(recovered.success)
        self.assertNotIn(ticket.ticket_id, await reopened.due_ticket_ids(self.now))
        self.assertEqual(
            self.projection.calls,
            [
                ("edit_ticket", ticket.ticket_id, models.TicketState.CLAIMED, None),
                ("edit_ticket", ticket.ticket_id, models.TicketState.CLAIMED, None),
            ],
        )

    async def test_unassign_projection_recovers_durably_after_transient_edit_failure(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        await self.coordinator.claim(ticket.ticket_id, self.actor(200))
        self.projection.calls.clear()
        self.projection.errors["edit_ticket"] = RuntimeError("controlled edit failure")

        failed = await self.coordinator.unassign(ticket.ticket_id, self.actor(200))

        self.assertFalse(failed.success)
        reopened_ticket = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(reopened_ticket.state, models.TicketState.OPEN)
        retry_at = await self.store.nearest_deadline()
        self.assertGreater(retry_at, self.now)

        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        self.assertIn(ticket.ticket_id, await reopened.due_ticket_ids(retry_at))
        self.now = retry_at
        self.projection.errors.pop("edit_ticket")
        restarted = coordinator_module.TicketCoordinator(
            reopened,
            self.projection,
            get_settings=self.get_settings,
            get_candidates=self.get_candidates,
            wake_deadlines=self.wake_deadlines,
            clock=lambda: self.now,
        )

        recovered = await restarted.process_due(ticket.ticket_id)

        self.assertTrue(recovered.success)
        self.assertNotIn(ticket.ticket_id, await reopened.due_ticket_ids(self.now))
        self.assertEqual(
            self.projection.calls,
            [
                ("edit_ticket", ticket.ticket_id, models.TicketState.OPEN, None),
                ("edit_ticket", ticket.ticket_id, models.TicketState.OPEN, None),
            ],
        )

    async def test_mark_finished_authority_deletes_thread_message_and_state(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.projection.calls.clear()

        rejected = await self.coordinator.mark_finished(
            ticket.ticket_id,
            self.actor(999, participant=True),
        )
        accepted = await self.coordinator.mark_finished(
            ticket.ticket_id,
            self.actor(ticket.author_id, participant=False),
        )

        self.assertEqual(rejected.response, "You cannot use this action")
        self.assertTrue(accepted.success)
        self.assertIsNone(await self.store.get_ticket(ticket.ticket_id))
        self.assertEqual(
            self.projection.calls,
            [
                ("delete_thread", ticket.thread_id),
                ("delete_message", ticket.channel_id, ticket.message_id),
            ],
        )

    async def test_direct_due_ping_timeout_then_wait_uses_acknowledged_budget(self):
        await self.store.save_profile(
            guild_id=10,
            user_id=200,
            github_username="direct-reviewer",
            category_ids=(self.category.category_id,),
            automatic_pings=True,
            updated_at=self.now,
        )
        ticket = await self.create_active(
            routing_mode=models.RoutingMode.DIRECT_WAIT,
            direct_target_id=200,
        )
        self.projection.calls.clear()
        self.now += timedelta(seconds=self.settings.protection_seconds)

        pinged = await self.coordinator.process_due(ticket.ticket_id)

        self.assertTrue(pinged.success)
        waiting = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(waiting.current_target_id, 200)
        self.assertEqual(waiting.ping_count, 1)
        self.assertEqual(waiting.next_action, models.NextAction.TARGET_TIMEOUT)
        self.assertEqual(
            self.projection.calls,
            [
                ("ping_reviewer", ticket.thread_id, 200, False),
                (
                    "edit_ticket",
                    ticket.ticket_id,
                    models.TicketState.OPEN,
                    "direct-reviewer",
                ),
            ],
        )

        self.projection.calls.clear()
        self.now = waiting.next_action_at
        timed_out = await self.coordinator.process_due(ticket.ticket_id)

        self.assertTrue(timed_out.success)
        passive = await self.store.get_ticket(ticket.ticket_id)
        self.assertIsNone(passive.current_target_id)
        self.assertIsNone(passive.next_action)
        self.assertEqual(
            [(item.user_id, item.reason) for item in await self.store.list_exclusions(ticket.ticket_id)],
            [(200, models.ExclusionReason.TIMED_OUT)],
        )
        self.assertEqual(
            self.projection.calls,
            [("edit_ticket", ticket.ticket_id, models.TicketState.OPEN, None)],
        )

        claimed = await self.coordinator.claim(
            ticket.ticket_id,
            self.actor(200, participant=False),
        )
        self.assertTrue(claimed.success)

    async def test_automatic_due_uses_presence_deadline_and_failed_send_costs_no_ping(self):
        ticket = await self.create_active()
        self.candidates = (
            self.candidate(500, presence=models.PresenceTier.IDLE),
        )
        self.now = ticket.next_action_at
        expected_response_deadline = self.now + timedelta(
            seconds=self.settings.idle_response_seconds
        )
        self.projection.calls.clear()
        self.projection.ping_error = RuntimeError("controlled send failure")

        failed = await self.coordinator.process_due(ticket.ticket_id)

        self.assertFalse(failed.success)
        reserved = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(reserved.ping_count, 0)
        self.assertEqual(await self.store.list_pings(ticket.ticket_id), ())

        self.projection.ping_error = None
        self.now = reserved.next_action_at
        accepted = await self.coordinator.process_due(ticket.ticket_id)

        self.assertTrue(accepted.success)
        waiting = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(waiting.ping_count, 1)
        self.assertEqual(waiting.current_target_id, 500)
        self.assertEqual(
            waiting.next_action_at,
            expected_response_deadline,
        )

    async def test_successful_ping_is_not_repeated_after_acknowledgement_failure(self):
        ticket = await self.create_active()
        self.candidates = (self.candidate(500),)
        self.now = ticket.next_action_at
        self.projection.calls.clear()
        acknowledge_ping = self.store.acknowledge_ping
        attempts = 0

        async def fail_once(ticket_id, sent_at):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("controlled settlement failure")
            return await acknowledge_ping(ticket_id, sent_at)

        self.store.acknowledge_ping = fail_once

        with self.assertRaisesRegex(RuntimeError, "controlled settlement failure"):
            await self.coordinator.process_due(ticket.ticket_id)
        recovered = await self.coordinator.process_due(ticket.ticket_id)

        self.assertTrue(recovered.success)
        self.assertEqual(attempts, 2)
        self.assertEqual(
            [call for call in self.projection.calls if call[0] == "ping_reviewer"],
            [("ping_reviewer", ticket.thread_id, 500, True)],
        )

    async def test_known_deletion_events_remove_only_the_remaining_projection(self):
        first = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.projection.calls.clear()

        await self.coordinator.handle_message_deleted(first.message_id)

        self.assertIsNone(await self.store.get_ticket(first.ticket_id))
        self.assertEqual(self.projection.calls, [("delete_thread", first.thread_id)])

        second = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.projection.calls.clear()
        self.projection.not_found_operations.add("delete_message")

        await self.coordinator.handle_thread_deleted(second.thread_id)
        await self.coordinator.handle_thread_deleted(9999)

        self.assertIsNone(await self.store.get_ticket(second.ticket_id))
        self.assertEqual(
            self.projection.calls,
            [("delete_message", second.channel_id, second.message_id)],
        )

    async def test_recovery_retries_known_projection_cleanup_without_preflight(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        await self.store.begin_finishing(ticket.ticket_id, self.now)
        self.projection.calls.clear()

        result = await self.coordinator.recover_projection_cleanup(ticket.ticket_id)

        self.assertTrue(result.success)
        self.assertIsNone(await self.store.get_ticket(ticket.ticket_id))
        self.assertEqual(
            self.projection.calls,
            [
                ("delete_thread", ticket.thread_id),
                ("delete_message", ticket.channel_id, ticket.message_id),
            ],
        )

        retained = await self.create_active(routing_mode=models.RoutingMode.NONE)
        await self.store.begin_finishing(retained.ticket_id, self.now)
        self.projection.errors["delete_message"] = RuntimeError("controlled retry")

        failed = await self.coordinator.recover_projection_cleanup(retained.ticket_id)

        self.assertFalse(failed.success)
        self.assertIsNotNone(await self.store.get_ticket(retained.ticket_id))

    async def test_protection_coalesces_due_ping_to_latest_state_change(self):
        ticket = await self.create_active(
            routing_mode=models.RoutingMode.DIRECT_AUTOMATIC,
            direct_target_id=200,
        )
        self.now = ticket.next_action_at
        await self.coordinator.process_due(ticket.ticket_id)
        self.projection.calls.clear()

        await self.coordinator.decline(ticket.ticket_id, self.actor(200, participant=False))
        first_deadline = (await self.store.get_ticket(ticket.ticket_id)).next_action_at
        self.now += timedelta(seconds=5)
        await self.coordinator.decline(ticket.ticket_id, self.actor(300))
        latest_protection = (await self.store.get_ticket(ticket.ticket_id)).protection_until

        self.now = first_deadline
        await self.coordinator.process_due(ticket.ticket_id)

        deferred = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(deferred.next_action_at, latest_protection)
        self.assertFalse(any(call[0] == "ping_reviewer" for call in self.projection.calls))

    async def test_exhausted_budget_or_candidate_pool_leaves_ticket_open_passively(self):
        for max_pings, candidates in ((0, (self.candidate(),)), (3, ())):
            with self.subTest(max_pings=max_pings, has_candidates=bool(candidates)):
                self.settings = replace(self.settings, max_pings=max_pings)
                self.candidates = candidates
                ticket = await self.create_active()
                self.now = ticket.next_action_at
                self.projection.calls.clear()

                result = await self.coordinator.process_due(ticket.ticket_id)

                self.assertTrue(result.success)
                passive = await self.store.get_ticket(ticket.ticket_id)
                self.assertEqual(passive.state, models.TicketState.OPEN)
                self.assertIsNone(passive.next_action)
                self.assertFalse(
                    any(call[0] == "ping_reviewer" for call in self.projection.calls)
                )

    async def test_not_found_from_ping_and_finish_is_successful_absence(self):
        ping_ticket = await self.create_active(
            routing_mode=models.RoutingMode.DIRECT_WAIT,
            direct_target_id=200,
        )
        self.now = ping_ticket.next_action_at
        self.projection.ping_error = projection_module.ProjectionNotFound()
        self.projection.calls.clear()

        ping_result = await self.coordinator.process_due(ping_ticket.ticket_id)

        self.assertTrue(ping_result.success)
        self.assertIsNone(await self.store.get_ticket(ping_ticket.ticket_id))
        self.assertEqual(
            self.projection.calls,
            [
                ("ping_reviewer", ping_ticket.thread_id, 200, False),
                ("delete_message", ping_ticket.channel_id, ping_ticket.message_id),
            ],
        )

        self.projection.ping_error = None
        finished = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.projection.not_found_operations.update({"delete_thread", "delete_message"})
        self.projection.calls.clear()

        finish_result = await self.coordinator.mark_finished(
            finished.ticket_id,
            self.actor(finished.author_id),
        )

        self.assertTrue(finish_result.success)
        self.assertIsNone(await self.store.get_ticket(finished.ticket_id))

    async def test_declining_a_reserved_failed_ping_cancels_that_target(self):
        ticket = await self.create_active(
            routing_mode=models.RoutingMode.DIRECT_WAIT,
            direct_target_id=200,
        )
        self.now = ticket.next_action_at
        self.projection.ping_error = RuntimeError("controlled send failure")
        await self.coordinator.process_due(ticket.ticket_id)

        result = await self.coordinator.decline(
            ticket.ticket_id,
            self.actor(200, participant=False),
        )

        self.assertTrue(result.success)
        declined = await self.store.get_ticket(ticket.ticket_id)
        self.assertIsNone(declined.pending_target_id)
        self.assertIsNone(declined.next_action)
