from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.githubtickets_loader import isolated_githubtickets_modules


class _Receiver:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.closed = asyncio.Event()

    async def start(self, host: str, port: int) -> int:
        self.events.append(("start", host, port))
        return port

    async def close(self) -> None:
        self.events.append("close")
        self.closed.set()


class _Client:
    def __init__(self, pages: dict[int, tuple[object, ...]] | None = None) -> None:
        self.pages = pages or {}
        self.listed_pages: list[int] = []
        self.redelivered: list[int] = []
        self.mutations: list[tuple[str, str, str, int, str]] = []

    async def list_deliveries(self, *, page: int = 1) -> tuple[object, ...]:
        self.listed_pages.append(page)
        return self.pages.get(page, ())

    async def redeliver(self, delivery_id: int) -> None:
        self.redelivered.append(delivery_id)

    async def add_assignee(
        self,
        owner: str,
        repository: str,
        number: int,
        login: str,
    ) -> None:
        self.mutations.append(("add", owner, repository, number, login))

    async def remove_assignee(
        self,
        owner: str,
        repository: str,
        number: int,
        login: str,
    ) -> None:
        self.mutations.append(("remove", owner, repository, number, login))


class _FailingAddClient(_Client):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def add_assignee(
        self,
        owner: str,
        repository: str,
        number: int,
        login: str,
    ) -> None:
        raise self.error


class _SerializingClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.add_started = asyncio.Event()
        self.release_add = asyncio.Event()
        self.active_mutations = 0
        self.max_active_mutations = 0

    async def add_assignee(
        self,
        owner: str,
        repository: str,
        number: int,
        login: str,
    ) -> None:
        self.active_mutations += 1
        self.max_active_mutations = max(
            self.max_active_mutations,
            self.active_mutations,
        )
        self.add_started.set()
        try:
            await self.release_add.wait()
            await super().add_assignee(owner, repository, number, login)
        finally:
            self.active_mutations -= 1

    async def redeliver(self, delivery_id: int) -> None:
        self.active_mutations += 1
        self.max_active_mutations = max(
            self.max_active_mutations,
            self.active_mutations,
        )
        try:
            await asyncio.sleep(0)
            await super().redeliver(delivery_id)
        finally:
            self.active_mutations -= 1


class _Reporter:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []

    async def report(self, **kwargs) -> None:
        self.reports.append(kwargs)


class _Bot:
    def __init__(self, reporter: _Reporter | None = None) -> None:
        self.reporter = reporter

    def get_cog(self, name: str) -> _Reporter | None:
        return self.reporter


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


class GitHubIntegrationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.modules_context = isolated_githubtickets_modules(Path(self.directory.name))
        self.modules = self.modules_context.__enter__()
        self.store = self.modules.store.GitHubTicketsStore(
            Path(self.directory.name) / "githubtickets.sqlite"
        )
        await self.store.initialize()
        self.now = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
        self.runtime = None

    async def asyncTearDown(self) -> None:
        if self.runtime is not None:
            await self.runtime.close()
        self.modules_context.__exit__(None, None, None)
        self.directory.cleanup()

    async def _accept_delivery(self, guid: str) -> None:
        self.assertTrue(
            await self.store.accept_delivery(
                delivery_guid=guid,
                github_delivery_id=None,
                event="pull_request",
                action="labeled",
                installation_id=123,
                repository_id=100,
                pr_number=7,
                received_at=self.now,
                raw_body=b'{"pull_request":{"number":7}}',
            )
        )

    async def _create_add_outbox_intent(self) -> int:
        pull_request = self.modules.models.GitHubPullRequest(
            repository_id=100,
            pr_number=7,
            github_pr_id=700,
            github_author_id=900,
            repository_full_name=" NewHorizons/NHCogs ",
            url="https://github.com/NewHorizons/NHCogs/pull/7",
            title="Add GitHub App integration",
            github_author_login="author",
            draft=False,
            open=True,
            labels=("discord-ticket",),
            github_updated_at=self.now,
        )
        ticket = await self.store.create_ticket_for_pull_request(
            self.modules.models.NewTicket(
                guild_id=10,
                channel_id=20,
                author_id=30,
                pr_title=pull_request.title,
                pr_url=pull_request.url,
                category_display="",
                routing_mode=self.modules.models.RoutingMode.NONE,
                direct_target_id=None,
                category_ids=(),
                created_at=self.now,
                origin=self.modules.models.TicketOrigin.GITHUB,
            ),
            pull_request,
        )
        await self.store.activate_ticket(
            ticket.ticket_id,
            message_id=40,
            thread_id=50,
            protection_until=self.now,
            next_action=None,
            next_action_at=None,
            updated_at=self.now,
        )
        self.assertTrue(
            await self.store.claim_with_github_outbox(
                ticket.ticket_id,
                assignee_id=60,
                github_login=" Reviewer ",
                protection_until=self.now,
                updated_at=self.now,
            )
        )
        return ticket.ticket_id

    async def _create_add_and_remove_outbox_intents(self) -> None:
        ticket_id = await self._create_add_outbox_intent()
        self.assertEqual(
            await self.store.unassign_with_github_outbox(
                ticket_id,
                github_login="REVIEWER",
                protection_until=self.now,
                next_action=None,
                next_action_at=None,
                updated_at=self.now + timedelta(seconds=1),
            ),
            60,
        )

    async def test_delivery_handler_disposition_completes_processed_and_ignored_work(
        self,
    ) -> None:
        await self._accept_delivery("processed-delivery")
        await self._accept_delivery("ignored-delivery")
        handled: list[str] = []

        async def handle(delivery):
            handled.append(delivery.delivery_guid)
            if delivery.delivery_guid == "ignored-delivery":
                return self.modules.runtime.DeliveryDisposition.IGNORED
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=_Client(),
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0,
        )
        self.assertEqual(await self.runtime.start("127.0.0.1", 8080), 8080)

        async def both_handled() -> bool:
            processed = await self.store.get_delivery("processed-delivery")
            ignored = await self.store.get_delivery("ignored-delivery")
            return (
                len(handled) == 2
                and processed.state is self.modules.models.GitHubDeliveryState.PROCESSED
                and ignored.state is self.modules.models.GitHubDeliveryState.IGNORED
            )

        await _wait_until(both_handled)
        processed = await self.store.get_delivery("processed-delivery")
        ignored = await self.store.get_delivery("ignored-delivery")
        self.assertEqual(
            processed.state,
            self.modules.models.GitHubDeliveryState.PROCESSED,
        )
        self.assertEqual(
            ignored.state,
            self.modules.models.GitHubDeliveryState.IGNORED,
        )
        self.assertIsNone(processed.raw_body)
        self.assertIsNone(ignored.raw_body)

    async def test_delivery_failure_is_reported_and_deferred_without_blocking_later_work(
        self,
    ) -> None:
        await self._accept_delivery("a-failing-delivery")
        await self._accept_delivery("b-successful-delivery")
        reporter = _Reporter()

        async def handle(delivery):
            if delivery.delivery_guid == "a-failing-delivery":
                raise RuntimeError("handler failed")
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=_Client(),
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(reporter),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0,
            retry_base=timedelta(seconds=30),
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def later_work_completed() -> bool:
            delivery = await self.store.get_delivery("b-successful-delivery")
            return (
                delivery is not None
                and delivery.state is self.modules.models.GitHubDeliveryState.PROCESSED
            )

        await _wait_until(later_work_completed)
        failed = await self.store.get_delivery("a-failing-delivery")
        self.assertEqual(failed.state, self.modules.models.GitHubDeliveryState.RETRY)
        self.assertEqual(failed.attempts, 1)
        self.assertGreaterEqual(
            failed.next_attempt_at,
            self.now + timedelta(seconds=15),
        )
        self.assertLessEqual(
            failed.next_attempt_at,
            self.now + timedelta(seconds=45),
        )
        self.assertEqual(len(reporter.reports), 1)
        self.assertEqual(reporter.reports[0]["source"], "GitHubTickets")
        self.assertIsInstance(reporter.reports[0]["error"], RuntimeError)

    async def test_stale_processing_delivery_is_reclaimed_after_restart(self) -> None:
        await self._accept_delivery("stale-delivery")
        claimed = await self.store.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(claimed.attempts, 1)

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=_Client(),
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now + timedelta(minutes=10),
            poll_interval=0.001,
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def reclaimed() -> bool:
            stored = await self.store.get_delivery("stale-delivery")
            return stored.state is self.modules.models.GitHubDeliveryState.PROCESSED

        await _wait_until(reclaimed)
        stored = await self.store.get_delivery("stale-delivery")
        self.assertEqual(stored.attempts, 2)

    async def test_delivery_failure_terminally_fails_at_attempt_limit(self) -> None:
        await self._accept_delivery("attempt-limited-delivery")
        claimed = await self.store.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertTrue(
            await self.store.defer_delivery(
                claimed.delivery_guid,
                next_attempt_at=self.now,
                error_summary="prepare attempt limit",
            )
        )
        reporter = _Reporter()

        async def handle(delivery):
            raise RuntimeError("still failing")

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=_Client(),
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(reporter),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0.001,
            max_delivery_attempts=2,
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def terminally_failed() -> bool:
            stored = await self.store.get_delivery("attempt-limited-delivery")
            return stored.state is self.modules.models.GitHubDeliveryState.FAILED

        await _wait_until(terminally_failed)
        stored = await self.store.get_delivery("attempt-limited-delivery")
        self.assertEqual(stored.attempts, 2)
        self.assertEqual(len(reporter.reports), 1)

    async def test_recovery_paginates_and_redelivers_only_recent_missing_deliveries(
        self,
    ) -> None:
        for guid in ("failed-local", "successful-local"):
            await self._accept_delivery(guid)
        summary = self.modules.github_app.GitHubDeliverySummary
        delivered_at = self.now - timedelta(minutes=1)
        page_one = [
            summary(1, "missing-success", delivered_at, False, 200, "ping", None),
            summary(2, "failed-local", delivered_at, False, 500, "ping", None),
            summary(3, "successful-local", delivered_at, False, 200, "ping", None),
            summary(4, "redelivery-missing", delivered_at, True, 500, "ping", None),
            summary(
                5,
                "expired-missing",
                self.now - timedelta(days=8),
                False,
                500,
                "ping",
                None,
            ),
        ]
        page_one.extend(
            summary(
                10 + index,
                f"redelivery-{index}",
                delivered_at,
                True,
                500,
                "ping",
                None,
            )
            for index in range(95)
        )
        page_two = (summary(200, "later-failure", delivered_at, False, 502, "ping", None),)
        client = _Client({1: tuple(page_one), 2: page_two})
        handled: list[str] = []

        async def handle(delivery):
            handled.append(delivery.delivery_guid)
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0,
        )

        await self.runtime.run_recovery()

        self.assertEqual(client.listed_pages, [1, 2])
        self.assertEqual(client.redelivered, [1, 200])
        self.assertEqual(handled, [])

    async def test_recovery_applies_delivery_retention(self) -> None:
        await self._accept_delivery("retained-failure")
        await self.store.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertTrue(
            await self.store.fail_delivery(
                "retained-failure",
                completed_at=self.now,
                error_summary="failed",
            )
        )

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=_Client(),
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now + timedelta(days=4),
            poll_interval=0.001,
        )
        await self.runtime.run_recovery()

        retained = await self.store.get_delivery("retained-failure")
        self.assertIsNotNone(retained)
        self.assertIsNone(retained.raw_body)

    async def test_periodic_recovery_runs_and_close_stops_receiver_first(self) -> None:
        await self._accept_delivery("close-order-delivery")
        receiver = _Receiver()
        client = _Client()
        handler_started = asyncio.Event()
        cancelled_after_receiver_close: list[bool] = []

        async def handle(delivery):
            handler_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_after_receiver_close.append(receiver.closed.is_set())
                raise

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=receiver,
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0.001,
            recovery_interval=timedelta(milliseconds=1),
        )
        await self.runtime.start("127.0.0.1", 8080)
        await asyncio.wait_for(handler_started.wait(), timeout=1)

        async def recovered_twice() -> bool:
            return len(client.listed_pages) >= 2

        await _wait_until(recovered_twice)
        await self.runtime.close()
        self.runtime = None
        stored = await self.store.get_delivery("close-order-delivery")
        self.assertEqual(receiver.events[0], ("start", "127.0.0.1", 8080))
        self.assertEqual(receiver.events[-1], "close")
        self.assertEqual(cancelled_after_receiver_close, [True])
        self.assertEqual(stored.state, self.modules.models.GitHubDeliveryState.PROCESSING)

    async def test_recovery_request_wakes_recovery_before_the_interval(self) -> None:
        client = _Client()

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0.001,
            recovery_interval=timedelta(hours=1),
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def recovered_once() -> bool:
            return len(client.listed_pages) >= 1

        await _wait_until(recovered_once)
        self.runtime.request_recovery()

        async def recovered_twice() -> bool:
            return len(client.listed_pages) >= 2

        await _wait_until(recovered_twice)

    async def test_missing_credentials_leave_runtime_dormant_and_half_configuration_is_rejected(
        self,
    ) -> None:
        await self._accept_delivery("dormant-delivery")
        handled: list[str] = []

        async def handle(delivery):
            handled.append(delivery.delivery_guid)
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=None,
            receiver=None,
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0,
        )
        self.assertIsNone(await self.runtime.start("127.0.0.1", 8080))
        await asyncio.sleep(0)
        stored = await self.store.get_delivery("dormant-delivery")
        self.assertEqual(stored.state, self.modules.models.GitHubDeliveryState.PENDING)
        self.assertEqual(handled, [])
        with self.assertRaisesRegex(ValueError, "configured together"):
            self.modules.runtime.GitHubIntegrationRuntime(
                self.store,
                client=_Client(),
                receiver=None,
                delivery_handler=handle,
                bot=_Bot(),
                guild_id=10,
                clock=lambda: self.now,
            )

    async def test_outbox_executes_add_and_remove_assignee_intents_in_order(
        self,
    ) -> None:
        await self._create_add_and_remove_outbox_intents()
        client = _Client()

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now + timedelta(minutes=1),
            poll_interval=0.001,
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def both_mutations_completed() -> bool:
            return len(client.mutations) == 2

        await _wait_until(both_mutations_completed)
        self.assertEqual(
            client.mutations,
            [
                ("add", "NewHorizons", "NHCogs", 7, "reviewer"),
                ("remove", "NewHorizons", "NHCogs", 7, "reviewer"),
            ],
        )

    async def test_outbox_and_recovery_share_one_github_mutation_lock(self) -> None:
        await self._create_add_outbox_intent()
        client = _SerializingClient()

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0.001,
        )
        await self.runtime.start("127.0.0.1", 8080)
        await asyncio.wait_for(client.add_started.wait(), timeout=1)
        summary = self.modules.github_app.GitHubDeliverySummary(
            99,
            "missing-during-add",
            self.now,
            False,
            200,
            "ping",
            None,
        )
        client.pages[1] = (summary,)
        recovery = asyncio.create_task(self.runtime.run_recovery())
        await asyncio.sleep(0)
        self.assertEqual(client.redelivered, [])
        self.assertEqual(client.max_active_mutations, 1)

        client.release_add.set()
        await asyncio.wait_for(recovery, timeout=1)
        self.assertEqual(client.redelivered, [99])
        self.assertEqual(client.max_active_mutations, 1)

    async def test_outbox_intent_survives_guild_cleanup_and_executes_without_lookup(
        self,
    ) -> None:
        await self._create_add_outbox_intent()
        claimed = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertTrue(await self.store.delete_guild_state(10))
        self.assertIsNone(await self.store.get_pull_request(100, 7))
        preserved = await self.store.get_outbox_item(claimed.outbox_id)
        self.assertEqual(preserved.repository_full_name, "NewHorizons/NHCogs")
        client = _Client()

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(),
            guild_id=10,
            clock=lambda: self.now + timedelta(minutes=10),
            poll_interval=0.001,
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def mutation_completed() -> bool:
            stored = await self.store.get_outbox_item(claimed.outbox_id)
            return (
                len(client.mutations) == 1
                and stored.state is self.modules.models.GitHubOutboxState.SUCCEEDED
            )

        await _wait_until(mutation_completed)
        self.assertEqual(
            client.mutations,
            [("add", "NewHorizons", "NHCogs", 7, "reviewer")],
        )

    async def test_unavailable_assignee_is_reported_and_terminally_fails_outbox(
        self,
    ) -> None:
        await self._create_add_outbox_intent()
        item = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertIsNotNone(item)
        assert item is not None
        reporter = _Reporter()
        client = _FailingAddClient(self.modules.github_app.GitHubAssigneeUnavailable("reviewer"))

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(reporter),
            guild_id=10,
            clock=lambda: self.now + timedelta(minutes=10),
            poll_interval=0.001,
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def terminally_failed() -> bool:
            stored = await self.store.get_outbox_item(item.outbox_id)
            return stored.state is self.modules.models.GitHubOutboxState.FAILED

        await _wait_until(terminally_failed)
        stored = await self.store.get_outbox_item(item.outbox_id)
        self.assertEqual(stored.attempts, 2)
        self.assertEqual(len(reporter.reports), 1)
        self.assertIsInstance(
            reporter.reports[0]["error"],
            self.modules.github_app.GitHubAssigneeUnavailable,
        )

    async def test_retryable_github_failure_honors_retry_at_and_keeps_local_transition(
        self,
    ) -> None:
        ticket_id = await self._create_add_outbox_intent()
        item = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertTrue(
            await self.store.defer_outbox(
                item.outbox_id,
                next_attempt_at=self.now,
                error_summary="prepare retry test",
            )
        )
        retry_at = self.now + timedelta(hours=1)
        reporter = _Reporter()
        client = _FailingAddClient(
            self.modules.github_app.GitHubRequestError(
                "add assignee",
                429,
                retryable=True,
                rate_limited=True,
                retry_at=retry_at,
            )
        )

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(reporter),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0.001,
            max_outbox_attempts=3,
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def deferred() -> bool:
            stored = await self.store.get_outbox_item(item.outbox_id)
            return (
                stored.state is self.modules.models.GitHubOutboxState.RETRY and stored.attempts == 2
            )

        await _wait_until(deferred)
        stored = await self.store.get_outbox_item(item.outbox_id)
        ticket = await self.store.get_ticket(ticket_id)
        self.assertEqual(stored.next_attempt_at, retry_at)
        self.assertEqual(ticket.state, self.modules.models.TicketState.CLAIMED)
        self.assertEqual(ticket.assignee_id, 60)
        self.assertEqual(len(reporter.reports), 1)

    async def test_retryable_github_failure_terminally_fails_at_attempt_limit(
        self,
    ) -> None:
        await self._create_add_outbox_intent()
        item = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertTrue(
            await self.store.defer_outbox(
                item.outbox_id,
                next_attempt_at=self.now,
                error_summary="prepare attempt limit",
            )
        )
        client = _FailingAddClient(
            self.modules.github_app.GitHubRequestError(
                "add assignee",
                503,
                retryable=True,
            )
        )

        async def handle(delivery):
            return self.modules.runtime.DeliveryDisposition.PROCESSED

        self.runtime = self.modules.runtime.GitHubIntegrationRuntime(
            self.store,
            client=client,
            receiver=_Receiver(),
            delivery_handler=handle,
            bot=_Bot(_Reporter()),
            guild_id=10,
            clock=lambda: self.now,
            poll_interval=0.001,
            max_outbox_attempts=2,
        )
        await self.runtime.start("127.0.0.1", 8080)

        async def terminally_failed() -> bool:
            stored = await self.store.get_outbox_item(item.outbox_id)
            return stored.state is self.modules.models.GitHubOutboxState.FAILED

        await _wait_until(terminally_failed)
        stored = await self.store.get_outbox_item(item.outbox_id)
        self.assertEqual(stored.attempts, 2)


if __name__ == "__main__":
    unittest.main()
