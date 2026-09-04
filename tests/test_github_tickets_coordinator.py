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
from unittest import mock

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
        self.recovered_message_id: int | None = None
        self.recovered_thread_id: int | None = None
        self.recovered_ping_at: datetime | None = None
        self.send_started: asyncio.Event | None = None
        self.allow_send: asyncio.Event | None = None

    async def send_ticket(self, ticket, *, reviewer_github=None):
        self.calls.append(("send_ticket", ticket.ticket_id, reviewer_github))
        if self.send_started is not None:
            self.send_started.set()
        if self.allow_send is not None:
            await self.allow_send.wait()
        message_id = self.next_message_id
        self.next_message_id += 1
        return message_id

    async def find_ticket_message(self, ticket):
        self.calls.append(("find_ticket_message", ticket.public_token))
        return self.recovered_message_id

    async def find_ticket_thread(self, ticket):
        self.calls.append(("find_ticket_thread", ticket.message_id))
        if error := self.errors.get("find_ticket_thread"):
            raise error
        return self.recovered_thread_id

    async def find_ping(self, thread_id, target_user_id, automatic, reserved_at):
        self.calls.append(
            ("find_ping", thread_id, target_user_id, automatic, reserved_at)
        )
        if error := self.errors.get("find_ping"):
            raise error
        return self.recovered_ping_at

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
        if error := self.errors.get("delete_thread"):
            raise error


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

        self.support = mock.Mock(report_operational_error=mock.AsyncMock())
        self.coordinator = coordinator_module.TicketCoordinator(
            self.store,
            self.projection,
            support=self.support,
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

    async def test_projection_failure_reports_once_and_keeps_retry(self):
        support = self.support
        ticket = await self.create_active()
        failure = RuntimeError("Discord publication failed")
        self.projection.errors["edit_ticket"] = failure

        result = await self.coordinator.sync_projection(ticket.ticket_id)

        self.assertFalse(result.success)
        support.report_operational_error.assert_awaited_once()
        report = support.report_operational_error.await_args.kwargs
        self.assertEqual(report["guild_id"], ticket.guild_id)
        self.assertEqual(report["source"], "GitHubTickets")
        self.assertIs(report["error"], failure)
        current = await self.store.get_ticket(ticket.ticket_id)
        self.assertIsNotNone(current)
        self.assertIsNotNone(await self.store.nearest_deadline())

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
            matches_profile=True,
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

    async def test_create_rejects_direct_self_review_without_writing_ticket(self):
        result = await self.coordinator.create_ticket(
            self.request(models.RoutingMode.DIRECT_WAIT, direct_target_id=100),
            self.actor(user_id=100),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.response, "You cannot select yourself as the reviewer")
        self.assertIsNone(await self.store.get_ticket(1))
        self.assertEqual(self.projection.calls, [])

    async def test_create_ignores_direct_target_for_non_direct_routing(self):
        result = await self.coordinator.create_ticket(
            self.request(models.RoutingMode.AUTOMATIC, direct_target_id=100),
            self.actor(user_id=100),
        )

        self.assertTrue(result.success)
        ticket = await self.store.get_ticket(1)
        self.assertIsNotNone(ticket)
        self.assertIsNone(ticket.direct_target_id)

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
        retry_at = await self.store.nearest_deadline()
        self.assertGreaterEqual(
            retry_at,
            self.now + timedelta(seconds=coordinator_module.PROJECTION_RETRY_SECONDS),
        )
        self.assertGreater(self.wake_count, 0)

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
            support=mock.Mock(report_operational_error=mock.AsyncMock()),
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
            support=mock.Mock(report_operational_error=mock.AsyncMock()),
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
                    None,
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

    async def test_restart_reconciles_existing_ping_before_sending_again(self):
        ticket = await self.create_active()
        self.now = ticket.next_action_at
        reserved_at = self.now
        reservation = await self.store.reserve_ping(
            ticket.ticket_id,
            target_user_id=500,
            presence_tier=models.PresenceTier.ONLINE,
            automatic=True,
            reserved_at=reserved_at,
            response_deadline=reserved_at + timedelta(minutes=5),
            maximum_pings=3,
        )
        sent_at = reserved_at + timedelta(seconds=1)
        self.projection.recovered_ping_at = sent_at
        self.projection.calls.clear()
        restarted = coordinator_module.TicketCoordinator(
            self.store,
            self.projection,
            support=mock.Mock(report_operational_error=mock.AsyncMock()),
            get_settings=self.get_settings,
            get_candidates=self.get_candidates,
            wake_deadlines=self.wake_deadlines,
            clock=lambda: self.now,
        )

        result = await restarted.process_due(ticket.ticket_id)

        self.assertTrue(result.success)
        self.assertEqual(
            self.projection.calls[0],
            ("find_ping", ticket.thread_id, 500, True, reservation.reserved_at),
        )
        self.assertEqual(
            [call for call in self.projection.calls if call[0] == "ping_reviewer"],
            [],
        )
        pings = await self.store.list_pings(ticket.ticket_id)
        self.assertEqual(pings[0].sent_at, sent_at)

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

    async def test_creating_recovery_reconciles_main_message_before_sending(self):
        ticket = await self.store.create_ticket(
            models.NewTicket(
                guild_id=10,
                channel_id=20,
                author_id=100,
                pr_title="Recovered ticket",
                pr_url="https://example.test/pull/recovered",
                category_display="",
                routing_mode=models.RoutingMode.NONE,
                direct_target_id=None,
                category_ids=(),
                created_at=self.now,
            )
        )
        self.projection.recovered_message_id = 999

        result = await self.coordinator.recover_projection_cleanup(ticket.ticket_id)

        self.assertTrue(result.success)
        recovered = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(recovered.state, models.TicketState.OPEN)
        self.assertEqual(recovered.message_id, 999)
        self.assertIsNotNone(recovered.thread_id)
        self.assertEqual(
            self.projection.calls,
            [
                ("find_ticket_message", ticket.public_token),
                ("find_ticket_thread", 999),
                ("create_thread", ticket.ticket_id, 999),
            ],
        )

    async def test_creating_recovery_reconciles_existing_thread_before_creation(self):
        ticket = await self.store.create_ticket(
            models.NewTicket(
                guild_id=10,
                channel_id=20,
                author_id=100,
                pr_title="Recovered thread",
                pr_url="https://example.test/pull/thread",
                category_display="",
                routing_mode=models.RoutingMode.NONE,
                direct_target_id=None,
                category_ids=(),
                created_at=self.now,
            )
        )
        self.assertTrue(
            await self.store.record_ticket_message(ticket.ticket_id, 999, self.now)
        )
        self.projection.recovered_thread_id = 888

        result = await self.coordinator.recover_projection_cleanup(ticket.ticket_id)

        self.assertTrue(result.success)
        recovered = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(recovered.state, models.TicketState.OPEN)
        self.assertEqual(recovered.thread_id, 888)
        self.assertIn(("find_ticket_thread", 999), self.projection.calls)
        self.assertEqual(
            [call for call in self.projection.calls if call[0] == "create_thread"],
            [],
        )

    async def test_missing_creating_message_is_terminal_during_thread_recovery(self):
        ticket = await self.store.create_ticket(
            models.NewTicket(
                guild_id=10,
                channel_id=20,
                author_id=100,
                pr_title="Missing main message",
                pr_url="https://example.test/pull/missing",
                category_display="",
                routing_mode=models.RoutingMode.NONE,
                direct_target_id=None,
                category_ids=(),
                created_at=self.now,
            )
        )
        await self.store.record_ticket_message(ticket.ticket_id, 999, self.now)
        self.projection.errors["find_ticket_thread"] = (
            projection_module.ProjectionNotFound()
        )

        result = await self.coordinator.recover_projection_cleanup(ticket.ticket_id)

        self.assertTrue(result.success)
        self.assertIsNone(await self.store.get_ticket(ticket.ticket_id))
        self.assertNotIn(ticket.ticket_id, await self.store.due_ticket_ids(self.now))

    async def test_missing_creating_message_cleanup_failure_does_not_refetch(self):
        ticket = await self.store.create_ticket(
            models.NewTicket(
                guild_id=10,
                channel_id=20,
                author_id=100,
                pr_title="Missing main message",
                pr_url="https://example.test/pull/missing-retry",
                category_display="",
                routing_mode=models.RoutingMode.NONE,
                direct_target_id=None,
                category_ids=(),
                created_at=self.now,
            )
        )
        await self.store.record_ticket_message(ticket.ticket_id, 999, self.now)
        self.projection.errors["find_ticket_thread"] = (
            projection_module.ProjectionNotFound()
        )
        original_delete_ticket = self.store.delete_ticket
        delete_attempts = 0

        async def fail_delete_once(ticket_id):
            nonlocal delete_attempts
            delete_attempts += 1
            if delete_attempts == 1:
                raise RuntimeError("controlled store failure")
            await original_delete_ticket(ticket_id)

        self.store.delete_ticket = fail_delete_once

        failed = await self.coordinator.recover_projection_cleanup(ticket.ticket_id)

        self.assertFalse(failed.success)
        finishing = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(finishing.state, models.TicketState.FINISHING)
        self.assertIsNone(finishing.message_id)
        retry_at = await self.store.nearest_deadline()
        self.assertGreater(retry_at, self.now)

        self.now = retry_at
        recovered = await self.coordinator.process_due(ticket.ticket_id)

        self.assertTrue(recovered.success)
        self.assertIsNone(await self.store.get_ticket(ticket.ticket_id))
        find_calls = [
            call for call in self.projection.calls if call[0] == "find_ticket_thread"
        ]
        self.assertEqual(len(find_calls), 1)

    async def test_create_started_before_privacy_redaction_is_cleaned_by_redaction(self):
        self.projection.send_started = asyncio.Event()
        self.projection.allow_send = asyncio.Event()
        create_task = asyncio.create_task(
            self.coordinator.create_ticket(self.request(models.RoutingMode.NONE), self.actor())
        )
        await self.projection.send_started.wait()

        redact_task = asyncio.create_task(
            self.coordinator.redact_user(
                100,
                updated_at=self.now,
            )
        )
        await asyncio.sleep(0.01)

        self.assertFalse(redact_task.done())
        self.projection.allow_send.set()
        self.assertTrue((await create_task).success)
        await redact_task

        self.assertEqual(await self.store.list_authored_tickets(100), ())
        self.assertEqual(
            [call[0] for call in self.projection.calls],
            [
                "send_ticket",
                "create_thread",
                "delete_thread",
                "delete_message",
            ],
        )

    async def test_failed_mark_finished_cleanup_defers_and_wakes_recovery(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.projection.errors["delete_message"] = RuntimeError("controlled cleanup")
        wake_before = self.wake_count

        result = await self.coordinator.mark_finished(
            ticket.ticket_id,
            self.actor(ticket.author_id, participant=False),
        )

        self.assertFalse(result.success)
        retry_at = await self.store.nearest_deadline()
        self.assertGreaterEqual(
            retry_at,
            self.now + timedelta(seconds=coordinator_module.PROJECTION_RETRY_SECONDS),
        )
        self.assertGreater(self.wake_count, wake_before)

    async def test_failed_deletion_event_cleanup_defers_and_wakes_recovery(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.projection.errors["delete_thread"] = RuntimeError("controlled cleanup")
        wake_before = self.wake_count

        with self.assertRaisesRegex(RuntimeError, "controlled cleanup"):
            await self.coordinator.handle_message_deleted(ticket.message_id)

        retry_at = await self.store.nearest_deadline()
        self.assertGreaterEqual(
            retry_at,
            self.now + timedelta(seconds=coordinator_module.PROJECTION_RETRY_SECONDS),
        )
        self.assertGreater(self.wake_count, wake_before)

    async def test_failed_finishing_cleanup_gets_restart_safe_retry_deadline(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        self.assertTrue(await self.store.begin_finishing(ticket.ticket_id, self.now))
        self.projection.errors["delete_message"] = RuntimeError("controlled cleanup")

        failed = await self.coordinator.recover_projection_cleanup(ticket.ticket_id)

        self.assertFalse(failed.success)
        retry_at = await self.store.nearest_deadline()
        self.assertGreater(retry_at, self.now)
        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        self.assertIn(ticket.ticket_id, await reopened.due_ticket_ids(retry_at))

    async def test_pending_ping_not_found_uses_terminal_cleanup(self):
        ticket = await self.create_active()
        self.now = ticket.next_action_at
        await self.store.reserve_ping(
            ticket.ticket_id,
            target_user_id=500,
            presence_tier=models.PresenceTier.ONLINE,
            automatic=True,
            reserved_at=self.now,
            response_deadline=self.now + timedelta(minutes=5),
            maximum_pings=3,
        )
        self.projection.errors["find_ping"] = projection_module.ProjectionNotFound()
        restarted = coordinator_module.TicketCoordinator(
            self.store,
            self.projection,
            support=mock.Mock(report_operational_error=mock.AsyncMock()),
            get_settings=self.get_settings,
            get_candidates=self.get_candidates,
            wake_deadlines=self.wake_deadlines,
            clock=lambda: self.now,
        )

        result = await restarted.process_due(ticket.ticket_id)

        self.assertTrue(result.success)
        self.assertIsNone(await self.store.get_ticket(ticket.ticket_id))
        self.assertNotIn(ticket.ticket_id, await self.store.due_ticket_ids(self.now))

    async def test_pending_ping_not_found_cleanup_failure_uses_durable_retry(self):
        ticket = await self.create_active()
        self.now = ticket.next_action_at
        await self.store.reserve_ping(
            ticket.ticket_id,
            target_user_id=500,
            presence_tier=models.PresenceTier.ONLINE,
            automatic=True,
            reserved_at=self.now,
            response_deadline=self.now + timedelta(minutes=5),
            maximum_pings=3,
        )
        self.projection.errors["find_ping"] = projection_module.ProjectionNotFound()
        self.projection.errors["delete_message"] = RuntimeError("controlled cleanup")
        restarted = coordinator_module.TicketCoordinator(
            self.store,
            self.projection,
            support=mock.Mock(report_operational_error=mock.AsyncMock()),
            get_settings=self.get_settings,
            get_candidates=self.get_candidates,
            wake_deadlines=self.wake_deadlines,
            clock=lambda: self.now,
        )

        failed = await restarted.process_due(ticket.ticket_id)

        self.assertFalse(failed.success)
        finishing = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(finishing.state, models.TicketState.FINISHING)
        retry_at = await self.store.nearest_deadline()
        self.assertGreater(retry_at, self.now)

        self.projection.errors.clear()
        self.now = retry_at
        recovered = await restarted.process_due(ticket.ticket_id)

        self.assertTrue(recovered.success)
        self.assertIsNone(await self.store.get_ticket(ticket.ticket_id))
        find_calls = [call for call in self.projection.calls if call[0] == "find_ping"]
        self.assertEqual(len(find_calls), 1)

    async def test_privacy_redaction_waits_for_inflight_claim_before_snapshot(self):
        ticket = await self.create_active(routing_mode=models.RoutingMode.NONE)
        original_claim = self.store.claim
        original_reference_ids = self.store.user_reference_ticket_ids
        claim_started = asyncio.Event()
        allow_claim = asyncio.Event()
        reference_snapshot_taken = asyncio.Event()

        async def delayed_claim(*args, **kwargs):
            claim_started.set()
            await allow_claim.wait()
            return await original_claim(*args, **kwargs)

        async def observed_reference_ids(user_id):
            result = await original_reference_ids(user_id)
            reference_snapshot_taken.set()
            return result

        self.store.claim = delayed_claim
        self.store.user_reference_ticket_ids = observed_reference_ids
        claim_task = asyncio.create_task(
            self.coordinator.claim(ticket.ticket_id, self.actor(500))
        )
        await claim_started.wait()
        redact_task = asyncio.create_task(
            self.coordinator.redact_user(
                500,
                updated_at=self.now,
            )
        )
        await reference_snapshot_taken.wait()
        await asyncio.sleep(0.01)

        self.assertFalse(redact_task.done())
        allow_claim.set()
        await claim_task
        await redact_task

        redacted = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(redacted.state, models.TicketState.OPEN)
        self.assertIsNone(redacted.assignee_id)

    async def test_privacy_redaction_loads_current_guild_protection_after_creation(self):
        ticket = await self.create_active(
            routing_mode=models.RoutingMode.DIRECT_AUTOMATIC,
            direct_target_id=500,
        )

        affected = await self.coordinator.redact_user(
            500,
            updated_at=self.now,
        )

        self.assertEqual([current.ticket_id for current in affected], [ticket.ticket_id])
        redacted = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(redacted.state, models.TicketState.OPEN)
        self.assertIsNone(redacted.direct_target_id)
        self.assertEqual(
            redacted.protection_until,
            self.now + timedelta(seconds=self.settings.protection_seconds),
        )

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
