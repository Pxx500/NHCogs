import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.test_github_tickets_store import models, store_module

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class TicketMinimizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tickets.sqlite"
        self.store = store_module.GitHubTicketsStore(self.path)
        await self.store.initialize()

    async def _open(self, author_id=100, category_ids=()):
        ticket = await self.store.create_ticket(models.NewTicket(
            guild_id=10, channel_id=20, author_id=author_id,
            pr_title="Private PR title", pr_url="https://example.test/pull/123",
            category_display="python", routing_mode=models.RoutingMode.AUTOMATIC,
            direct_target_id=None, category_ids=category_ids, created_at=NOW,
        ))
        await self.store.activate_ticket(
            ticket.ticket_id, message_id=ticket.ticket_id + 1000,
            thread_id=ticket.ticket_id + 2000, protection_until=NOW,
            next_action=models.NextAction.AUTOMATIC_PING, next_action_at=NOW, updated_at=NOW,
        )
        return await self.store.get_ticket(ticket.ticket_id)

    async def _reserve(self, ticket_id, target_id=200):
        return await self.store.reserve_ping(
            ticket_id, target_user_id=target_id, presence_tier=models.PresenceTier.ONLINE,
            automatic=True, reserved_at=NOW, response_deadline=NOW + timedelta(minutes=10),
            maximum_pings=3,
        )

    async def test_pending_and_historical_presence_never_written_or_returned(self):
        ticket = await self._open()
        reservation = await self._reserve(ticket.ticket_id)
        self.assertIsNone(reservation.presence_tier)
        self.assertIsNone((await self.store.get_ticket(ticket.ticket_id)).pending_presence_tier)
        repeated = await self.store.reserve_ping(
            ticket.ticket_id, target_user_id=201, presence_tier=models.PresenceTier.OFFLINE,
            automatic=True, reserved_at=NOW + timedelta(minutes=1),
            response_deadline=NOW + timedelta(hours=1), maximum_pings=3,
        )
        self.assertEqual(repeated, reservation)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT pending_presence_tier FROM tickets WHERE ticket_id = ?", (ticket.ticket_id,),
            ).fetchone()[0])
        ping = await self.store.acknowledge_ping(ticket.ticket_id, NOW)
        self.assertIsNone(ping.presence_tier)
        self.assertEqual(ping.response_deadline, reservation.response_deadline)
        self.assertIsNone((await self.store.list_pings(ticket.ticket_id))[0].presence_tier)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT presence_tier FROM ticket_pings WHERE ticket_id = ?", (ticket.ticket_id,),
            ).fetchone()[0])

    async def test_version_one_migration_scrubs_all_presence_and_old_finishing_records(self):
        legacy_path = Path(self.directory.name) / "legacy.sqlite"
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            store_module._create_schema(connection)
            connection.execute("PRAGMA user_version = 1")
        self.path = legacy_path
        self.store = store_module.GitHubTicketsStore(legacy_path)
        category = await self.store.add_category(10, "python", NOW)
        profile = await self.store.save_profile(
            guild_id=10, user_id=100, github_username="developer",
            category_ids=(category.category_id,), automatic_pings=False, updated_at=NOW,
        )
        active = await self._open(category_ids=(category.category_id,))
        finishing = await self._open(author_id=101, category_ids=(category.category_id,))
        history = await self._open(author_id=102, category_ids=(category.category_id,))
        await self._reserve(active.ticket_id)
        await self._reserve(finishing.ticket_id, target_id=201)
        await self.store.acknowledge_ping(finishing.ticket_id, NOW)
        await self._reserve(history.ticket_id, target_id=203)
        await self.store.acknowledge_ping(history.ticket_id, NOW)
        await self.store.decline(finishing.ticket_id, 202, NOW)
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.execute("UPDATE tickets SET pending_presence_tier = 'online' WHERE ticket_id = ?",
                               (active.ticket_id,))
            connection.execute("UPDATE ticket_pings SET presence_tier = 'offline'")
            connection.execute("UPDATE tickets SET state = 'finishing' WHERE ticket_id = ?",
                               (finishing.ticket_id,))
        pending_before = await self.store.get_ticket(active.ticket_id)
        await self.store.initialize()
        await self.store.initialize()
        self.assertEqual(await self.store.get_profile(10, 100), profile)
        active_after = await self.store.get_ticket(active.ticket_id)
        self.assertEqual(active_after.pending_target_id, pending_before.pending_target_id)
        self.assertEqual(active_after.pending_ping_reserved_at, pending_before.pending_ping_reserved_at)
        self.assertEqual(active_after.pending_response_deadline, pending_before.pending_response_deadline)
        self.assertEqual(active_after.pr_title, active.pr_title)
        self.assertIsNone(active_after.pending_presence_tier)
        history_after = await self.store.list_pings(history.ticket_id)
        self.assertEqual(len(history_after), 1)
        self.assertEqual(history_after[0].target_user_id, 203)
        self.assertEqual(history_after[0].sent_at, NOW)
        self.assertEqual(history_after[0].response_deadline, NOW + timedelta(minutes=10))
        self.assertIsNone(history_after[0].presence_tier)
        with closing(sqlite3.connect(legacy_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM tickets WHERE pending_presence_tier IS NOT NULL"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM ticket_pings WHERE presence_tier IS NOT NULL"
            ).fetchone()[0], 0)
        await self._assert_minimal_finishing(finishing)

    async def _assert_minimal_finishing(self, original):
        ticket = await self.store.get_ticket(original.ticket_id)
        self.assertEqual(ticket.state, models.TicketState.FINISHING)
        self.assertEqual((ticket.author_id, ticket.pr_title, ticket.pr_url, ticket.category_display),
                         (0, "", "", ""))
        self.assertEqual(ticket.routing_mode, models.RoutingMode.NONE)
        self.assertEqual(ticket.category_ids, ())
        self.assertEqual(ticket.ping_count, 0)
        for value in (ticket.direct_target_id, ticket.current_target_id, ticket.assignee_id,
                      ticket.pending_target_id, ticket.pending_ping_reserved_at,
                      ticket.pending_response_deadline, ticket.pending_ping_automatic,
                      ticket.pending_presence_tier, ticket.protection_until, ticket.next_action_at):
            self.assertIsNone(value)
        self.assertEqual((ticket.guild_id, ticket.channel_id, ticket.message_id, ticket.thread_id,
                          ticket.public_token),
                         (original.guild_id, original.channel_id, original.message_id,
                          original.thread_id, original.public_token))
        self.assertEqual(ticket.created_at, datetime(1970, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(await self.store.list_pings(ticket.ticket_id), ())
        self.assertEqual(await self.store.list_exclusions(ticket.ticket_id), ())

    async def test_finishing_clears_ticket_data_before_remote_cleanup_and_preserves_profiles(self):
        category = await self.store.add_category(10, "python", NOW)
        profile = await self.store.save_profile(
            guild_id=10, user_id=100, github_username="developer",
            category_ids=(category.category_id,), automatic_pings=False, updated_at=NOW,
        )
        ticket = await self._open(category_ids=(category.category_id,))
        other = await self._open(author_id=101, category_ids=(category.category_id,))
        await self._reserve(ticket.ticket_id)
        await self.store.acknowledge_ping(ticket.ticket_id, NOW)
        await self.store.decline(ticket.ticket_id, 201, NOW)
        await self._reserve(ticket.ticket_id, target_id=202)
        self.assertTrue(await self.store.begin_finishing(ticket.ticket_id, NOW))
        await self._assert_minimal_finishing(ticket)
        retry_at = NOW + timedelta(minutes=5)
        finishing = await self.store.get_ticket(ticket.ticket_id)
        self.assertTrue(await self.store.defer_projection_sync(
            ticket.ticket_id, finishing.transition_version, retry_at,
        ))
        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        self.assertEqual(await reopened.get_profile(10, 100), profile)
        self.assertEqual(await reopened.get_ticket(other.ticket_id), other)
        cleanup = await reopened.list_projection_cleanup_tickets()
        self.assertEqual([row.ticket_id for row in cleanup], [ticket.ticket_id])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT projection_sync_at FROM tickets WHERE ticket_id = ?", (ticket.ticket_id,),
            ).fetchone()[0], retry_at.isoformat())
        self.assertTrue(await reopened.delete_ticket(ticket.ticket_id))
        self.assertIsNone(await reopened.get_ticket(ticket.ticket_id))
        self.assertEqual(await reopened.get_profile(10, 100), profile)
        self.assertEqual(await reopened.list_categories(10), (category,))

    async def test_minimization_failure_rolls_back_finishing_transition_and_child_deletions(self):
        category = await self.store.add_category(10, "python", NOW)
        ticket = await self._open(category_ids=(category.category_id,))
        await self._reserve(ticket.ticket_id)
        ping = await self.store.acknowledge_ping(ticket.ticket_id, NOW)
        before = await self.store.get_ticket(ticket.ticket_id)
        real_minimize = store_module._minimize_finishing_tickets

        def minimize_then_fail(connection, ticket_id):
            real_minimize(connection, ticket_id)
            raise RuntimeError("write failed")

        with mock.patch.object(store_module, "_minimize_finishing_tickets", side_effect=minimize_then_fail):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                await self.store.begin_finishing(ticket.ticket_id, NOW)
        self.assertEqual(await self.store.get_ticket(ticket.ticket_id), before)
        self.assertEqual(await self.store.list_pings(ticket.ticket_id), (ping,))
