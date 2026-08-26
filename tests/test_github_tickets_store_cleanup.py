from __future__ import annotations

import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_github_tickets_store import models, store_module


class GitHubTicketsStoreCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "githubtickets.sqlite"
        self.store = store_module.GitHubTicketsStore(self.path)
        await self.store.initialize()
        self.now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)

    async def create_open_ticket(
        self,
        *,
        guild_id: int,
        channel_id: int,
        author_id: int,
        routing_mode=None,
        direct_target_id: int | None = None,
        category_ids: tuple[int, ...] = (),
        category_display: str = "",
    ):
        selected_mode = routing_mode or models.RoutingMode.AUTOMATIC
        ticket = await self.store.create_ticket(
            models.NewTicket(
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=author_id,
                pr_title=f"Ticket {guild_id}-{channel_id}-{author_id}",
                pr_url="https://example.test/pull/1",
                category_display=category_display,
                routing_mode=selected_mode,
                direct_target_id=direct_target_id,
                category_ids=category_ids,
                created_at=self.now,
            )
        )
        await self.store.activate_ticket(
            ticket.ticket_id,
            message_id=1000 + ticket.ticket_id,
            thread_id=2000 + ticket.ticket_id,
            protection_until=self.now,
            next_action=None,
            next_action_at=None,
            updated_at=self.now,
        )
        return await self.store.get_ticket(ticket.ticket_id)

    async def target_ticket(self, ticket, user_id: int, *, acknowledge: bool) -> None:
        await self.store.reserve_ping(
            ticket.ticket_id,
            target_user_id=user_id,
            presence_tier=models.PresenceTier.ONLINE,
            automatic=ticket.routing_mode is not models.RoutingMode.DIRECT_WAIT,
            reserved_at=self.now,
            response_deadline=self.now + timedelta(hours=1),
            maximum_pings=3,
        )
        if acknowledge:
            await self.store.acknowledge_ping(ticket.ticket_id, self.now)

    async def test_channel_and_guild_cleanup_are_isolated_idempotent_and_cascading(self):
        category_10 = await self.store.add_category(10, "rendering", self.now)
        category_20 = await self.store.add_category(20, "rendering", self.now)
        await self.store.save_profile(
            guild_id=10,
            user_id=100,
            github_username=None,
            category_ids=(category_10.category_id,),
            automatic_pings=True,
            updated_at=self.now,
        )
        await self.store.save_profile(
            guild_id=20,
            user_id=200,
            github_username=None,
            category_ids=(category_20.category_id,),
            automatic_pings=True,
            updated_at=self.now,
        )
        removed = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=101,
            category_ids=(category_10.category_id,),
            category_display="rendering",
        )
        kept_same_guild = await self.create_open_ticket(
            guild_id=10,
            channel_id=31,
            author_id=102,
        )
        kept_other_guild = await self.create_open_ticket(
            guild_id=20,
            channel_id=30,
            author_id=201,
        )
        await self.store.decline(removed.ticket_id, 999, self.now)

        deleted = await self.store.delete_tickets_for_channel(10, 30)

        self.assertEqual(tuple(ticket.ticket_id for ticket in deleted), (removed.ticket_id,))
        self.assertEqual(await self.store.delete_tickets_for_channel(10, 30), ())
        self.assertIsNone(await self.store.get_ticket(removed.ticket_id))
        self.assertIsNotNone(await self.store.get_ticket(kept_same_guild.ticket_id))
        self.assertIsNotNone(await self.store.get_ticket(kept_other_guild.ticket_id))

        self.assertTrue(await self.store.delete_guild_state(10))
        self.assertFalse(await self.store.delete_guild_state(10))
        self.assertIsNone(await self.store.get_ticket(kept_same_guild.ticket_id))
        self.assertIsNone(await self.store.get_profile(10, 100))
        self.assertEqual(await self.store.list_categories(10), ())
        self.assertIsNotNone(await self.store.get_ticket(kept_other_guild.ticket_id))
        self.assertIsNotNone(await self.store.get_profile(20, 200))
        with closing(store_module.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_projection_cleanup_query_returns_only_creating_and_finishing_tickets(self):
        creating = await self.store.create_ticket(
            models.NewTicket(
                guild_id=10,
                channel_id=30,
                author_id=100,
                pr_title="Creating",
                pr_url="https://example.test/pull/creating",
                category_display="",
                routing_mode=models.RoutingMode.NONE,
                direct_target_id=None,
                category_ids=(),
                created_at=self.now,
            )
        )
        open_ticket = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=101,
        )
        finishing = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=102,
        )
        await self.store.begin_finishing(finishing.ticket_id, self.now)

        pending = await self.store.list_projection_cleanup_tickets()

        self.assertEqual(
            tuple(ticket.ticket_id for ticket in pending),
            (creating.ticket_id, finishing.ticket_id),
        )
        self.assertNotIn(open_ticket.ticket_id, {ticket.ticket_id for ticket in pending})

    async def test_authored_ticket_cleanup_redacts_content_before_projection_deletion(self):
        user_id = 500
        category = await self.store.add_category(10, "rendering", self.now)
        ticket = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=user_id,
            routing_mode=models.RoutingMode.DIRECT_AUTOMATIC,
            direct_target_id=600,
            category_ids=(category.category_id,),
            category_display="private selection",
        )
        await self.target_ticket(ticket, 600, acknowledge=True)

        cleanup = await self.store.begin_authored_ticket_cleanup(
            ticket.ticket_id,
            author_id=user_id,
            updated_at=self.now,
        )

        self.assertIsNotNone(cleanup)
        self.assertEqual(cleanup.state, models.TicketState.FINISHING)
        self.assertEqual(cleanup.author_id, 0)
        self.assertEqual(cleanup.pr_title, "")
        self.assertEqual(cleanup.pr_url, "")
        self.assertEqual(cleanup.category_display, "")
        self.assertEqual(cleanup.routing_mode, models.RoutingMode.NONE)
        self.assertEqual(cleanup.category_ids, ())
        self.assertIsNone(cleanup.direct_target_id)
        self.assertIsNone(cleanup.current_target_id)
        self.assertIsNone(cleanup.assignee_id)
        self.assertEqual(cleanup.ping_count, 0)
        self.assertEqual(cleanup.message_id, ticket.message_id)
        self.assertEqual(cleanup.thread_id, ticket.thread_id)
        self.assertEqual(await self.store.list_pings(ticket.ticket_id), ())
        self.assertEqual(await self.store.list_exclusions(ticket.ticket_id), ())
        self.assertIn(
            ticket.ticket_id,
            {
                item.ticket_id
                for item in await self.store.list_projection_cleanup_tickets()
            },
        )

        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        persisted = await reopened.get_ticket(ticket.ticket_id)
        self.assertEqual(persisted.state, models.TicketState.FINISHING)
        self.assertEqual(persisted.author_id, 0)
        self.assertEqual(persisted.message_id, ticket.message_id)
        self.assertEqual(persisted.thread_id, ticket.thread_id)

    async def test_authored_ticket_cleanup_requires_matching_active_author(self):
        ticket = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=500,
        )

        cleanup = await self.store.begin_authored_ticket_cleanup(
            ticket.ticket_id,
            author_id=501,
            updated_at=self.now,
        )

        self.assertIsNone(cleanup)
        self.assertEqual((await self.store.get_ticket(ticket.ticket_id)).author_id, 500)

    async def test_authored_ticket_cleanup_redacts_existing_finishing_state(self):
        user_id = 500
        ticket = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=user_id,
        )
        self.assertTrue(await self.store.begin_finishing(ticket.ticket_id, self.now))

        cleanup = await self.store.begin_authored_ticket_cleanup(
            ticket.ticket_id,
            author_id=user_id,
            updated_at=self.now,
        )

        self.assertIsNotNone(cleanup)
        self.assertEqual(cleanup.state, models.TicketState.FINISHING)
        self.assertEqual(cleanup.author_id, 0)
        self.assertEqual(cleanup.pr_title, "")
        self.assertEqual(cleanup.pr_url, "")
        self.assertEqual(cleanup.message_id, ticket.message_id)
        self.assertEqual(cleanup.thread_id, ticket.thread_id)

    async def test_user_redaction_is_atomic_complete_and_persists_after_restart(self):
        user_id = 500
        category_10 = await self.store.add_category(10, "rendering", self.now)
        category_20 = await self.store.add_category(20, "rendering", self.now)
        await self.store.save_profile(
            guild_id=10,
            user_id=user_id,
            github_username="private-name",
            category_ids=(category_10.category_id,),
            automatic_pings=True,
            updated_at=self.now,
        )

        authored_10 = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=user_id,
        )
        authored_20 = await self.create_open_ticket(
            guild_id=20,
            channel_id=40,
            author_id=user_id,
        )
        assigned = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=101,
            routing_mode=models.RoutingMode.AUTOMATIC,
        )
        await self.store.claim(assigned.ticket_id, user_id, self.now, self.now)
        direct_automatic = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=102,
            routing_mode=models.RoutingMode.DIRECT_AUTOMATIC,
            direct_target_id=user_id,
        )
        await self.target_ticket(direct_automatic, user_id, acknowledge=True)
        direct_wait = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=103,
            routing_mode=models.RoutingMode.DIRECT_WAIT,
            direct_target_id=user_id,
        )
        await self.target_ticket(direct_wait, user_id, acknowledge=True)
        pending_other_guild = await self.create_open_ticket(
            guild_id=20,
            channel_id=40,
            author_id=201,
            routing_mode=models.RoutingMode.AUTOMATIC,
            category_ids=(category_20.category_id,),
            category_display="rendering",
        )
        await self.target_ticket(pending_other_guild, user_id, acknowledge=False)
        direct_reference_only = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=104,
            routing_mode=models.RoutingMode.DIRECT_WAIT,
            direct_target_id=user_id,
        )
        history_only = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=105,
        )
        await self.store.decline(history_only.ticket_id, user_id, self.now)

        authored = await self.store.list_authored_tickets(user_id)
        reference_guild_ids = await self.store.user_reference_guild_ids(user_id)

        self.assertEqual(
            tuple(ticket.ticket_id for ticket in authored),
            (authored_10.ticket_id, authored_20.ticket_id),
        )
        self.assertEqual(reference_guild_ids, (10, 20))

        protection_10 = self.now + timedelta(seconds=10)
        protection_20 = self.now + timedelta(seconds=20)
        affected = await self.store.redact_user(
            user_id,
            protection_until_by_guild={10: protection_10, 20: protection_20},
            updated_at=self.now,
        )

        self.assertEqual(
            tuple(ticket.ticket_id for ticket in affected),
            (
                assigned.ticket_id,
                direct_automatic.ticket_id,
                direct_wait.ticket_id,
                pending_other_guild.ticket_id,
                direct_reference_only.ticket_id,
            ),
        )
        self.assertIsNone(await self.store.get_ticket(authored_10.ticket_id))
        self.assertIsNone(await self.store.get_ticket(authored_20.ticket_id))
        self.assertIsNone(await self.store.get_profile(10, user_id))
        self.assertEqual(await self.store.user_reference_guild_ids(user_id), ())
        self.assertEqual(await self.store.list_exclusions(history_only.ticket_id), ())

        expected = {
            assigned.ticket_id: (models.NextAction.AUTOMATIC_PING, protection_10),
            direct_automatic.ticket_id: (
                models.NextAction.AUTOMATIC_PING,
                protection_10,
            ),
            direct_wait.ticket_id: (None, None),
            pending_other_guild.ticket_id: (
                models.NextAction.AUTOMATIC_PING,
                protection_20,
            ),
            direct_reference_only.ticket_id: (None, None),
        }
        for ticket in affected:
            with self.subTest(ticket_id=ticket.ticket_id):
                self.assertEqual(ticket.state, models.TicketState.OPEN)
                self.assertIsNone(ticket.current_target_id)
                self.assertIsNone(ticket.assignee_id)
                self.assertIsNone(ticket.pending_target_id)
                self.assertEqual(
                    (ticket.next_action, ticket.next_action_at),
                    expected[ticket.ticket_id],
                )
        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        self.assertEqual(await reopened.user_reference_guild_ids(user_id), ())
        self.assertIsNone(await reopened.get_ticket(authored_10.ticket_id))
        self.assertEqual(
            (await reopened.get_ticket(assigned.ticket_id)).next_action_at,
            protection_10,
        )
        with closing(store_module.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_redacting_obsolete_direct_target_preserves_current_assignee(self):
        deleted_user_id = 500
        assignee_id = 600
        ticket = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=100,
            routing_mode=models.RoutingMode.DIRECT_WAIT,
            direct_target_id=deleted_user_id,
        )
        self.assertTrue(
            await self.store.claim(
                ticket.ticket_id,
                assignee_id,
                self.now + timedelta(minutes=1),
                self.now,
            )
        )

        affected = await self.store.redact_user(
            deleted_user_id,
            protection_until_by_guild={
                10: self.now + timedelta(seconds=10),
            },
            updated_at=self.now,
        )

        self.assertEqual(
            tuple(item.ticket_id for item in affected),
            (ticket.ticket_id,),
        )
        preserved = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(preserved.state, models.TicketState.CLAIMED)
        self.assertEqual(preserved.assignee_id, assignee_id)
        self.assertIsNone(preserved.direct_target_id)
        self.assertIsNone(preserved.next_action)
        self.assertIsNone(preserved.next_action_at)

    async def test_user_redaction_requires_every_affected_guild_deadline_before_mutation(self):
        user_id = 500
        assigned = await self.create_open_ticket(
            guild_id=10,
            channel_id=30,
            author_id=101,
        )
        await self.store.claim(assigned.ticket_id, user_id, self.now, self.now)

        with self.assertRaisesRegex(ValueError, "protection deadline"):
            await self.store.redact_user(
                user_id,
                protection_until_by_guild={},
                updated_at=self.now,
            )

        unchanged = await self.store.get_ticket(assigned.ticket_id)
        self.assertEqual(unchanged.state, models.TicketState.CLAIMED)
        self.assertEqual(unchanged.assignee_id, user_id)
