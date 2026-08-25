from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
import types
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "NHCogs"
PACKAGE_PATH = ROOT / PACKAGE_NAME
GITHUBTICKETS_PACKAGE_NAME = f"{PACKAGE_NAME}.githubtickets"
GITHUBTICKETS_PACKAGE_PATH = PACKAGE_PATH / "githubtickets"


def _load_store_modules():
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
        store = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.store")
    except (ImportError, ModuleNotFoundError):
        return None, None
    return models, store


models, store_module = _load_store_modules()


class GitHubTicketsStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "githubtickets.sqlite"
        self.store = (
            store_module.GitHubTicketsStore(self.path)
            if store_module is not None
            else None
        )
        self.now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)

    async def test_initialize_creates_versioned_schema_with_foreign_keys(self):
        self.assertIsNotNone(self.store, "the GitHub Tickets store interface is missing")
        await self.store.initialize()

        with closing(store_module.connect(self.path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertEqual(version, 1)
        self.assertEqual(foreign_keys, 1)
        self.assertTrue(
            {
                "categories",
                "profiles",
                "profile_categories",
                "tickets",
                "ticket_categories",
                "ticket_exclusions",
                "ticket_pings",
            }.issubset(tables)
        )

    async def test_initialize_rejects_newer_schema_version(self):
        self.assertIsNotNone(self.store, "the GitHub Tickets store interface is missing")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 2")

        with self.assertRaisesRegex(ValueError, "newer than supported version 1"):
            await self.store.initialize()

    async def test_categories_normalize_validate_and_enforce_guild_limit(self):
        await self.store.initialize()

        category = await self.store.add_category(10, "  ReNDeRiNG  ", self.now)

        self.assertEqual(category.name, "rendering")
        self.assertEqual(await self.store.list_categories(10), (category,))
        self.assertEqual(await self.store.list_categories(20), ())
        with self.assertRaises(models.CategoryAlreadyExists):
            await self.store.add_category(10, "RENDERING", self.now)
        with self.assertRaises(models.InvalidCategoryName):
            await self.store.add_category(10, "   ", self.now)
        with self.assertRaises(models.InvalidCategoryName):
            await self.store.add_category(10, f" {'x' * 101} ", self.now)
        longest = await self.store.add_category(20, f" {'X' * 100} ", self.now)
        self.assertEqual(longest.name, "x" * 100)

        for index in range(24):
            await self.store.add_category(10, f"category-{index}", self.now)
        with self.assertRaises(models.CategoryLimitReached):
            await self.store.add_category(10, "one-too-many", self.now)
        self.assertEqual(len(await self.store.list_categories(10)), 25)

    async def test_empty_profile_is_canonicalized_to_no_row(self):
        await self.store.initialize()
        category = await self.store.add_category(10, "python", self.now)
        saved = await self.store.save_profile(
            guild_id=10,
            user_id=100,
            github_username=" nova ",
            category_ids=(category.category_id,),
            automatic_pings=True,
            updated_at=self.now,
        )

        self.assertEqual(saved.github_username, "nova")
        self.assertEqual(saved.category_ids, (category.category_id,))
        self.assertTrue(saved.automatic_pings)

        cleared = await self.store.save_profile(
            guild_id=10,
            user_id=100,
            github_username="   ",
            category_ids=(),
            automatic_pings=False,
            updated_at=self.now,
        )

        self.assertIsNone(cleared)
        self.assertIsNone(await self.store.get_profile(10, 100))

    async def _create_open_ticket(
        self,
        *,
        author_id: int = 100,
        category_ids: tuple[int, ...] = (),
        category_display: str = "",
        next_action_at: datetime | None = None,
    ):
        ticket = await self.store.create_ticket(
            models.NewTicket(
                guild_id=10,
                channel_id=20,
                author_id=author_id,
                pr_title="Improve rendering",
                pr_url="https://example.test/pull/1",
                category_display=category_display,
                routing_mode=models.RoutingMode.AUTOMATIC,
                direct_target_id=None,
                category_ids=category_ids,
                created_at=self.now,
            )
        )
        self.assertEqual(ticket.state, models.TicketState.CREATING)
        activated = await self.store.activate_ticket(
            ticket.ticket_id,
            message_id=30 + ticket.ticket_id,
            thread_id=40 + ticket.ticket_id,
            protection_until=self.now,
            next_action=(
                models.NextAction.AUTOMATIC_PING if next_action_at is not None else None
            ),
            next_action_at=next_action_at,
            updated_at=self.now,
        )
        self.assertTrue(activated)
        return await self.store.get_ticket(ticket.ticket_id)

    async def test_category_deletion_removes_routing_links_but_preserves_snapshot(self):
        await self.store.initialize()
        rendering = await self.store.add_category(10, "rendering", self.now)
        python = await self.store.add_category(10, "python", self.now)
        await self.store.save_profile(
            guild_id=10,
            user_id=100,
            github_username=None,
            category_ids=(rendering.category_id, python.category_id),
            automatic_pings=True,
            updated_at=self.now,
        )
        await self.store.save_profile(
            guild_id=10,
            user_id=101,
            github_username=None,
            category_ids=(rendering.category_id,),
            automatic_pings=True,
            updated_at=self.now,
        )
        ticket = await self._create_open_ticket(
            category_ids=(rendering.category_id, python.category_id),
            category_display="rendering, python",
        )

        self.assertTrue(await self.store.delete_category(rendering.category_id))

        reloaded = await self.store.get_ticket(ticket.ticket_id)
        profile = await self.store.get_profile(10, 100)
        self.assertEqual(reloaded.category_display, "rendering, python")
        self.assertEqual(reloaded.category_ids, (python.category_id,))
        self.assertEqual(profile.category_ids, (python.category_id,))
        self.assertIsNone(await self.store.get_profile(10, 101))

    async def test_concurrent_claims_have_exactly_one_winner(self):
        await self.store.initialize()
        ticket = await self._create_open_ticket()
        other_store = store_module.GitHubTicketsStore(self.path)
        await other_store.initialize()

        first, second = await asyncio.gather(
            self.store.claim(ticket.ticket_id, 101, self.now, self.now),
            other_store.claim(ticket.ticket_id, 102, self.now, self.now),
        )

        self.assertEqual(sum((first, second)), 1)
        reloaded = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(reloaded.state, models.TicketState.CLAIMED)
        self.assertIn(reloaded.assignee_id, (101, 102))

    async def test_decline_unassign_and_ping_history_are_ticket_scoped(self):
        await self.store.initialize()
        first = await self._create_open_ticket(author_id=100)
        second = await self._create_open_ticket(author_id=200)

        self.assertTrue(await self.store.decline(first.ticket_id, 300, self.now))
        self.assertEqual(
            await self.store.list_exclusions(first.ticket_id),
            (models.TicketExclusion(first.ticket_id, 300, models.ExclusionReason.DECLINED, self.now),),
        )
        self.assertEqual(await self.store.list_exclusions(second.ticket_id), ())

        self.assertTrue(await self.store.claim(first.ticket_id, 301, self.now, self.now))
        former_assignee = await self.store.unassign(
            first.ticket_id,
            protection_until=self.now,
            next_action=None,
            next_action_at=None,
            updated_at=self.now,
        )
        self.assertEqual(former_assignee, 301)
        exclusions = await self.store.list_exclusions(first.ticket_id)
        self.assertEqual(
            {(item.user_id, item.reason) for item in exclusions},
            {
                (300, models.ExclusionReason.DECLINED),
                (301, models.ExclusionReason.UNASSIGNED),
            },
        )

        ping = await self.store.reserve_ping(
            first.ticket_id,
            target_user_id=400,
            presence_tier=models.PresenceTier.ONLINE,
            automatic=True,
            reserved_at=self.now,
            response_deadline=self.now,
            maximum_pings=1,
        )
        self.assertIsNotNone(ping)
        pending = await self.store.get_ticket(first.ticket_id)
        self.assertEqual(pending.ping_count, 0)
        self.assertEqual(await self.store.list_pings(first.ticket_id), ())
        acknowledged = await self.store.acknowledge_ping(first.ticket_id, self.now)
        self.assertIsNotNone(acknowledged)
        self.assertIsNone(
            await self.store.reserve_ping(
                first.ticket_id,
                target_user_id=401,
                presence_tier=models.PresenceTier.IDLE,
                automatic=True,
                reserved_at=self.now,
                response_deadline=self.now,
                maximum_pings=1,
            )
        )
        second_ping = await self.store.reserve_ping(
            second.ticket_id,
            target_user_id=400,
            presence_tier=None,
            automatic=False,
            reserved_at=self.now,
            response_deadline=self.now,
            maximum_pings=1,
        )
        self.assertIsNotNone(second_ping)
        await self.store.acknowledge_ping(second.ticket_id, self.now)
        self.assertEqual(len(await self.store.list_pings(first.ticket_id)), 1)
        self.assertEqual(len(await self.store.list_pings(second.ticket_id)), 1)

    async def test_target_timeout_is_atomic_and_returns_to_scheduled_open_state(self):
        await self.store.initialize()
        ticket = await self._create_open_ticket()
        await self.store.reserve_ping(
            ticket.ticket_id,
            target_user_id=400,
            presence_tier=models.PresenceTier.IDLE,
            automatic=True,
            reserved_at=self.now,
            response_deadline=self.now,
            maximum_pings=3,
        )
        await self.store.acknowledge_ping(ticket.ticket_id, self.now)

        settled = await self.store.settle_target_timeout(
            ticket.ticket_id,
            target_user_id=400,
            protection_until=self.now,
            next_action=models.NextAction.AUTOMATIC_PING,
            next_action_at=self.now,
            updated_at=self.now,
        )

        self.assertTrue(settled)
        reopened = await self.store.get_ticket(ticket.ticket_id)
        self.assertIsNone(reopened.current_target_id)
        self.assertEqual(reopened.next_action, models.NextAction.AUTOMATIC_PING)
        self.assertEqual(
            [(item.user_id, item.reason) for item in await self.store.list_exclusions(ticket.ticket_id)],
            [(400, models.ExclusionReason.TIMED_OUT)],
        )

    async def test_deadlines_and_active_tickets_survive_reopening(self):
        await self.store.initialize()
        deadline = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        ticket = await self._create_open_ticket(next_action_at=deadline)

        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()

        self.assertEqual(await reopened.nearest_deadline(), deadline)
        self.assertEqual(await reopened.due_ticket_ids(deadline), (ticket.ticket_id,))
        self.assertEqual(
            tuple(item.ticket_id for item in await reopened.list_active_tickets()),
            (ticket.ticket_id,),
        )

    async def test_ticket_projection_ids_resolve_without_message_content(self):
        await self.store.initialize()
        ticket = await self._create_open_ticket()

        self.assertEqual(
            await self.store.get_ticket_by_message_id(ticket.message_id),
            ticket,
        )
        self.assertEqual(
            await self.store.get_ticket_by_thread_id(ticket.thread_id),
            ticket,
        )
        self.assertIsNone(await self.store.get_ticket_by_message_id(9999))
        self.assertIsNone(await self.store.get_ticket_by_thread_id(9999))

    async def test_finishing_and_deletion_are_terminal(self):
        await self.store.initialize()
        ticket = await self._create_open_ticket()

        self.assertTrue(await self.store.begin_finishing(ticket.ticket_id, self.now))
        self.assertFalse(await self.store.claim(ticket.ticket_id, 101, self.now, self.now))
        self.assertTrue(await self.store.delete_ticket(ticket.ticket_id))
        self.assertIsNone(await self.store.get_ticket(ticket.ticket_id))
        self.assertFalse(await self.store.delete_ticket(ticket.ticket_id))
