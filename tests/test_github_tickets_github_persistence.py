from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_github_tickets_store import models, store_module


def _create_current_schema_fixture(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (guild_id, name)
        );
        CREATE TABLE profiles (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            github_username TEXT,
            automatic_pings INTEGER NOT NULL CHECK (automatic_pings IN (0, 1)),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE profile_categories (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id, category_id),
            FOREIGN KEY (guild_id, user_id)
                REFERENCES profiles (guild_id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (category_id)
                REFERENCES categories (category_id) ON DELETE CASCADE
        );
        CREATE TABLE tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_token TEXT NOT NULL UNIQUE,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER UNIQUE,
            thread_id INTEGER UNIQUE,
            author_id INTEGER NOT NULL,
            pr_title TEXT NOT NULL,
            pr_url TEXT NOT NULL,
            category_display TEXT NOT NULL,
            routing_mode TEXT NOT NULL CHECK (
                routing_mode IN (
                    'none', 'automatic', 'direct_wait', 'direct_automatic'
                )
            ),
            state TEXT NOT NULL CHECK (
                state IN ('creating', 'open', 'claimed', 'finishing')
            ),
            direct_target_id INTEGER,
            current_target_id INTEGER,
            assignee_id INTEGER,
            ping_count INTEGER NOT NULL DEFAULT 0 CHECK (ping_count >= 0),
            protection_until TEXT,
            next_action TEXT CHECK (
                next_action IS NULL OR next_action IN (
                    'direct_ping', 'automatic_ping', 'target_timeout'
                )
            ),
            next_action_at TEXT,
            pending_target_id INTEGER,
            pending_presence_tier TEXT CHECK (
                pending_presence_tier IS NULL OR pending_presence_tier IN (
                    'online', 'idle', 'do_not_disturb', 'offline'
                )
            ),
            pending_ping_automatic INTEGER CHECK (
                pending_ping_automatic IS NULL OR pending_ping_automatic IN (0, 1)
            ),
            pending_ping_reserved_at TEXT,
            pending_response_deadline TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            projection_sync_at TEXT,
            transition_version INTEGER NOT NULL DEFAULT 0
                CHECK (transition_version >= 0),
            CHECK (
                (next_action IS NULL AND next_action_at IS NULL)
                OR (next_action IS NOT NULL AND next_action_at IS NOT NULL)
            ),
            CHECK (
                (pending_target_id IS NULL
                    AND pending_ping_automatic IS NULL
                    AND pending_ping_reserved_at IS NULL
                    AND pending_response_deadline IS NULL)
                OR (pending_target_id IS NOT NULL
                    AND pending_ping_automatic IS NOT NULL
                    AND pending_ping_reserved_at IS NOT NULL
                    AND pending_response_deadline IS NOT NULL)
            )
        );
        CREATE TABLE ticket_categories (
            ticket_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            PRIMARY KEY (ticket_id, category_id),
            FOREIGN KEY (ticket_id)
                REFERENCES tickets (ticket_id) ON DELETE CASCADE,
            FOREIGN KEY (category_id)
                REFERENCES categories (category_id) ON DELETE CASCADE
        );
        CREATE TABLE ticket_exclusions (
            ticket_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL CHECK (
                reason IN ('declined', 'unassigned', 'timed_out')
            ),
            created_at TEXT NOT NULL,
            PRIMARY KEY (ticket_id, user_id),
            FOREIGN KEY (ticket_id)
                REFERENCES tickets (ticket_id) ON DELETE CASCADE
        );
        CREATE TABLE ticket_pings (
            ticket_id INTEGER NOT NULL,
            sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
            target_user_id INTEGER NOT NULL,
            presence_tier TEXT CHECK (
                presence_tier IS NULL OR presence_tier IN (
                    'online', 'idle', 'do_not_disturb', 'offline'
                )
            ),
            automatic INTEGER NOT NULL CHECK (automatic IN (0, 1)),
            sent_at TEXT NOT NULL,
            response_deadline TEXT NOT NULL,
            PRIMARY KEY (ticket_id, sequence_number),
            FOREIGN KEY (ticket_id)
                REFERENCES tickets (ticket_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_categories_guild ON categories (guild_id, name);
        CREATE INDEX idx_profiles_guild ON profiles (guild_id, user_id);
        CREATE INDEX idx_ticket_deadlines ON tickets (next_action_at, ticket_id)
            WHERE next_action_at IS NOT NULL;
        CREATE INDEX idx_ticket_message ON tickets (message_id)
            WHERE message_id IS NOT NULL;
        CREATE INDEX idx_ticket_thread ON tickets (thread_id)
            WHERE thread_id IS NOT NULL;
        CREATE INDEX idx_ticket_assignee ON tickets (guild_id, assignee_id)
            WHERE assignee_id IS NOT NULL;
        CREATE INDEX idx_ticket_pings_target
            ON ticket_pings (target_user_id, sent_at);
        """
    )


class GitHubTicketsGitHubPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "githubtickets.sqlite"
        self.store = store_module.GitHubTicketsStore(self.path)
        await self.store.initialize()
        self.now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)

    def pull_request(
        self,
        *,
        repository_id: int = 100,
        pr_number: int = 7,
        github_pr_id: int = 700,
        github_author_id: int = 900,
        title: str = "Add GitHub App integration",
        login: str = "octocat",
        assignees: tuple[str, ...] = (),
        updated_at: datetime | None = None,
        last_processed_action: str | None = "labeled",
    ):
        return models.GitHubPullRequest(
            repository_id=repository_id,
            pr_number=pr_number,
            github_pr_id=github_pr_id,
            github_author_id=github_author_id,
            repository_full_name="NewHorizons/NHCogs",
            url=f"https://github.com/NewHorizons/NHCogs/pull/{pr_number}",
            title=title,
            github_author_login=login,
            draft=False,
            open=True,
            labels=("discord-ticket", "python"),
            github_updated_at=updated_at or self.now,
            assignees=assignees,
            last_processed_action=last_processed_action,
        )

    def new_ticket(self, *, author_id: int | None = None):
        return models.NewTicket(
            guild_id=10,
            channel_id=20,
            author_id=author_id,
            pr_title="Add GitHub App integration",
            pr_url="https://github.com/NewHorizons/NHCogs/pull/7",
            category_display="",
            routing_mode=models.RoutingMode.NONE,
            direct_target_id=None,
            category_ids=(),
            created_at=self.now,
            origin=models.TicketOrigin.GITHUB,
        )

    async def create_pending_outbox(
        self,
        *,
        repository_id: int,
        pr_number: int,
        github_pr_id: int,
        assignee_id: int,
        github_login: str,
    ):
        ticket = await self.store.create_ticket_for_pull_request(
            self.new_ticket(author_id=111),
            self.pull_request(
                repository_id=repository_id,
                pr_number=pr_number,
                github_pr_id=github_pr_id,
            ),
        )
        await self.store.activate_ticket(
            ticket.ticket_id,
            message_id=10_000 + ticket.ticket_id,
            thread_id=20_000 + ticket.ticket_id,
            protection_until=self.now,
            next_action=None,
            next_action_at=None,
            updated_at=self.now,
        )
        await self.store.claim_with_github_outbox(
            ticket.ticket_id,
            assignee_id=assignee_id,
            github_login=github_login,
            protection_until=self.now,
            updated_at=self.now,
        )
        return ticket

    async def test_current_schema_migration_preserves_ticket_and_routing_state(self):
        legacy_path = Path(self.directory.name) / "legacy-githubtickets.sqlite"
        timestamp = self.now.isoformat()
        deadline = (self.now + timedelta(hours=1)).isoformat()
        with closing(store_module.connect(legacy_path)) as connection:
            _create_current_schema_fixture(connection)
            connection.execute(
                "INSERT INTO categories VALUES (1, 10, 'python', ?)",
                (timestamp,),
            )
            connection.execute(
                "INSERT INTO profiles VALUES (10, 111, 'octocat', 1, ?)",
                (timestamp,),
            )
            connection.execute(
                "INSERT INTO profile_categories VALUES (10, 111, 1)"
            )
            connection.execute(
                """
                INSERT INTO tickets (
                    ticket_id, public_token, guild_id, channel_id, message_id,
                    thread_id, author_id, pr_title, pr_url, category_display,
                    routing_mode, state, direct_target_id, current_target_id,
                    assignee_id, ping_count, protection_until, next_action,
                    next_action_at, pending_target_id, pending_presence_tier,
                    pending_ping_automatic, pending_ping_reserved_at,
                    pending_response_deadline, created_at, updated_at,
                    projection_sync_at, transition_version
                ) VALUES (
                    1, 'stable-token', 10, 20, 30, 40, 111, 'Legacy title',
                    'https://example.test/pull/1', 'python', 'direct_automatic',
                    'open', 222, 333, NULL, 1, ?, 'target_timeout', ?, 444,
                    'online', 1, ?, ?, ?, ?, ?, 8
                )
                """,
                (timestamp, deadline, timestamp, deadline, timestamp, timestamp, deadline),
            )
            connection.execute("INSERT INTO ticket_categories VALUES (1, 1)")
            connection.execute(
                "INSERT INTO ticket_exclusions VALUES (1, 555, 'declined', ?)",
                (timestamp,),
            )
            connection.execute(
                """
                INSERT INTO ticket_pings VALUES (
                    1, 1, 444, 'online', 1, ?, ?
                )
                """,
                (timestamp, deadline),
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        migrated = store_module.GitHubTicketsStore(legacy_path)
        await migrated.initialize()

        profile = await migrated.get_profile(10, 111)
        ticket = await migrated.get_ticket(1)
        self.assertEqual(profile.github_username, "octocat")
        self.assertEqual(profile.category_ids, (1,))
        self.assertEqual(ticket.author_id, 111)
        self.assertEqual(ticket.origin, models.TicketOrigin.DISCORD)
        self.assertEqual(ticket.public_token, "stable-token")
        self.assertEqual(
            (await migrated.get_ticket_by_public_token("stable-token")).ticket_id,
            1,
        )
        self.assertEqual(ticket.message_id, 30)
        self.assertEqual(ticket.thread_id, 40)
        self.assertEqual(ticket.routing_mode, models.RoutingMode.DIRECT_AUTOMATIC)
        self.assertEqual(ticket.current_target_id, 333)
        self.assertEqual(ticket.pending_target_id, 444)
        self.assertEqual(ticket.next_action, models.NextAction.TARGET_TIMEOUT)
        self.assertEqual(ticket.next_action_at.isoformat(), deadline)
        self.assertEqual(ticket.transition_version, 8)
        self.assertEqual(
            await migrated.due_ticket_ids(self.now + timedelta(hours=2)),
            (1,),
        )
        self.assertEqual(len(await migrated.list_exclusions(1)), 1)
        self.assertEqual(len(await migrated.list_pings(1)), 1)
        with closing(store_module.connect(legacy_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_pull_request_binding_reserves_one_active_ticket_and_keeps_identity_immutable(
        self,
    ):
        self.assertTrue(
            hasattr(self.store, "create_ticket_for_pull_request"),
            "the store must own atomic pull request binding",
        )
        ticket = await self.store.create_ticket_for_pull_request(
            self.new_ticket(),
            self.pull_request(),
        )

        self.assertIsNone(ticket.author_id)
        self.assertEqual(ticket.origin, models.TicketOrigin.GITHUB)
        bound = await self.store.get_pull_request(100, 7)
        self.assertEqual(bound.current_ticket_id, ticket.ticket_id)

        with self.assertRaises(models.ActivePullRequestTicketExists):
            await self.store.create_ticket_for_pull_request(
                self.new_ticket(author_id=111),
                self.pull_request(title="Mutable title"),
            )

        with self.assertRaisesRegex(ValueError, "immutable GitHub identity"):
            await self.store.observe_pull_request(
                self.pull_request(
                    github_pr_id=701,
                    title="Attempted identity replacement",
                    updated_at=self.now + timedelta(minutes=1),
                )
            )

        unchanged = await self.store.get_pull_request(100, 7)
        self.assertEqual(unchanged.github_pr_id, 700)
        self.assertEqual(unchanged.title, "Add GitHub App integration")

        mutable = await self.store.observe_pull_request(
            self.pull_request(
                title="Updated title",
                login="OctoCat-Renamed",
                updated_at=self.now + timedelta(minutes=2),
            )
        )
        self.assertIs(mutable.state, models.PullRequestObservationState.APPLIED)
        self.assertEqual(mutable.pull_request.github_pr_id, 700)
        self.assertEqual(mutable.pull_request.github_author_id, 900)
        self.assertEqual(mutable.pull_request.title, "Updated title")
        self.assertEqual(mutable.pull_request.github_author_login, "OctoCat-Renamed")
        ignored_older = await self.store.observe_pull_request(
            self.pull_request(
                title="Stale title",
                updated_at=self.now + timedelta(minutes=1),
            )
        )
        self.assertIs(ignored_older.state, models.PullRequestObservationState.STALE)
        self.assertEqual(ignored_older.pull_request.title, "Updated title")

        conflict = await self.store.observe_pull_request(
            self.pull_request(
                title="Conflicting title",
                login="OctoCat-Renamed",
                updated_at=self.now + timedelta(minutes=2),
            )
        )
        self.assertIs(conflict.state, models.PullRequestObservationState.CONFLICT)
        self.assertEqual(conflict.pull_request.title, "Updated title")

        authoritative = await self.store.observe_pull_request(
            self.pull_request(
                title="Authoritative title",
                login="OctoCat-Renamed",
                updated_at=self.now + timedelta(minutes=2),
            ),
            authoritative=True,
        )
        self.assertIs(authoritative.state, models.PullRequestObservationState.APPLIED)
        self.assertEqual(authoritative.pull_request.title, "Authoritative title")

        self.assertTrue(await self.store.delete_ticket(ticket.ticket_id))
        self.assertIsNone((await self.store.get_pull_request(100, 7)).current_ticket_id)
        replacement = await self.store.create_ticket_for_pull_request(
            self.new_ticket(author_id=111),
            self.pull_request(updated_at=self.now + timedelta(minutes=3)),
        )
        self.assertNotEqual(replacement.ticket_id, ticket.ticket_id)

    async def test_ticket_creation_requires_authority_for_each_origin(self):
        invalid_discord = models.NewTicket(
            guild_id=10,
            channel_id=20,
            author_id=None,
            pr_title="Missing Discord author",
            pr_url="https://example.test/pull/1",
            category_display="",
            routing_mode=models.RoutingMode.NONE,
            direct_target_id=None,
            category_ids=(),
            created_at=self.now,
        )
        with self.assertRaisesRegex(ValueError, "Discord.*author"):
            await self.store.create_ticket(invalid_discord)

        with self.assertRaisesRegex(ValueError, "pull request binding"):
            await self.store.create_ticket(self.new_ticket())

        self.assertEqual(await self.store.list_projection_cleanup_tickets(), ())

    async def test_observation_without_action_preserves_last_processed_action(self):
        await self.store.create_ticket_for_pull_request(
            self.new_ticket(),
            self.pull_request(),
        )

        observed = await self.store.observe_pull_request(
            self.pull_request(
                title="Title-only observation",
                updated_at=self.now + timedelta(minutes=1),
                last_processed_action=None,
            )
        )

        self.assertIs(observed.state, models.PullRequestObservationState.APPLIED)
        self.assertEqual(observed.pull_request.title, "Title-only observation")
        self.assertEqual(observed.pull_request.last_processed_action, "labeled")

    async def test_assignees_round_trip_and_conflict_at_equal_timestamp(self):
        applied = await self.store.observe_pull_request(
            self.pull_request(assignees=("ReviewerOne",))
        )

        self.assertIs(applied.state, models.PullRequestObservationState.APPLIED)
        self.assertEqual(applied.pull_request.assignees, ("ReviewerOne",))
        self.assertEqual(
            (await self.store.get_pull_request(100, 7)).assignees,
            ("ReviewerOne",),
        )

        conflict = await self.store.observe_pull_request(
            self.pull_request(assignees=("ReviewerTwo",))
        )
        self.assertIs(conflict.state, models.PullRequestObservationState.CONFLICT)
        self.assertEqual(conflict.pull_request.assignees, ("ReviewerOne",))

        authoritative = await self.store.observe_pull_request(
            self.pull_request(assignees=("ReviewerTwo",)),
            authoritative=True,
        )
        self.assertIs(authoritative.state, models.PullRequestObservationState.APPLIED)
        self.assertEqual(authoritative.pull_request.assignees, ("ReviewerTwo",))

    async def test_ticket_deletion_preserves_executable_github_intent(self):
        ticket = await self.store.create_ticket_for_pull_request(
            self.new_ticket(),
            self.pull_request(),
        )
        await self.store.activate_ticket(
            ticket.ticket_id,
            message_id=1001,
            thread_id=2001,
            protection_until=self.now,
            next_action=None,
            next_action_at=None,
            updated_at=self.now,
        )
        self.assertTrue(
            await self.store.claim_with_github_outbox(
                ticket.ticket_id,
                assignee_id=222,
                github_login="Reviewer",
                protection_until=self.now + timedelta(seconds=10),
                updated_at=self.now,
            )
        )

        self.assertTrue(await self.store.delete_ticket(ticket.ticket_id))
        intent = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.repository_id, 100)
        self.assertEqual(intent.pr_number, 7)
        self.assertEqual(intent.github_login, "reviewer")
        self.assertTrue(
            await self.store.complete_outbox(intent.outbox_id, completed_at=self.now)
        )

    async def test_delivery_inbox_deduplicates_recovers_stale_work_and_clears_successful_body(
        self,
    ):
        self.assertTrue(
            hasattr(self.store, "accept_delivery"),
            "the store must own durable delivery acceptance",
        )
        for guid in ("delivery-b", "delivery-a"):
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
                    raw_body=b'{"private":"payload"}',
                )
            )
        self.assertFalse(
            await self.store.accept_delivery(
                delivery_guid="delivery-a",
                github_delivery_id=None,
                event="pull_request",
                action="edited",
                installation_id=123,
                repository_id=100,
                pr_number=7,
                received_at=self.now + timedelta(minutes=1),
                raw_body=b"different duplicate body",
            )
        )

        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        first = await reopened.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        second = await reopened.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(first.delivery_guid, "delivery-a")
        self.assertEqual(second.delivery_guid, "delivery-b")
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.raw_body, b'{"private":"payload"}')
        self.assertFalse(
            await reopened.accept_delivery(
                delivery_guid="delivery-a",
                github_delivery_id=None,
                event="pull_request",
                action="labeled",
                installation_id=123,
                repository_id=100,
                pr_number=7,
                received_at=self.now + timedelta(minutes=1),
                raw_body=b"processing duplicate",
            )
        )
        self.assertIsNone(
            await reopened.claim_next_delivery(
                now=self.now,
                stale_before=self.now - timedelta(minutes=5),
            )
        )

        recovered = await reopened.claim_next_delivery(
            now=self.now + timedelta(minutes=10),
            stale_before=self.now + timedelta(minutes=1),
        )
        self.assertEqual(recovered.delivery_guid, "delivery-a")
        self.assertEqual(recovered.attempts, 2)
        self.assertTrue(
            await reopened.complete_delivery(
                recovered.delivery_guid,
                completed_at=self.now + timedelta(minutes=10),
            )
        )
        completed = await reopened.get_delivery(recovered.delivery_guid)
        self.assertEqual(completed.state, models.GitHubDeliveryState.PROCESSED)
        self.assertIsNone(completed.raw_body)
        self.assertFalse(
            await reopened.accept_delivery(
                delivery_guid="delivery-a",
                github_delivery_id=None,
                event="pull_request",
                action="labeled",
                installation_id=123,
                repository_id=100,
                pr_number=7,
                received_at=self.now + timedelta(minutes=11),
                raw_body=b"processed duplicate",
            )
        )

        with self.assertRaisesRegex(ValueError, "raw body"):
            await reopened.accept_delivery(
                delivery_guid="too-large",
                github_delivery_id=None,
                event="ping",
                action=None,
                installation_id=123,
                repository_id=None,
                pr_number=None,
                received_at=self.now,
                raw_body=b"x" * (store_module.MAX_DELIVERY_BODY_BYTES + 1),
            )

    async def test_recovery_checkpoint_is_durable_and_globally_owned(self):
        self.assertEqual(
            await self.store.get_delivery_recovery_checkpoint(),
            (1, None),
        )
        await self.store.save_delivery_recovery_checkpoint(
            next_page=4,
            last_delivery_id=321,
            checked_at=self.now,
        )
        await self.store.save_profile(
            guild_id=10,
            user_id=20,
            github_username="someone",
            category_ids=(),
            automatic_pings=False,
            updated_at=self.now,
        )

        reloaded = store_module.GitHubTicketsStore(self.path)
        await reloaded.initialize()
        self.assertEqual(
            await reloaded.get_delivery_recovery_checkpoint(),
            (4, 321),
        )
        self.assertTrue(await reloaded.delete_guild_state(10))
        self.assertEqual(
            await reloaded.get_delivery_recovery_checkpoint(),
            (4, 321),
        )

    async def test_delivery_retention_bounds_failed_bodies_and_keeps_identity_for_seven_days(
        self,
    ):
        self.assertTrue(
            hasattr(self.store, "prune_deliveries"),
            "the store must own delivery retention cutoffs",
        )
        await self.store.accept_delivery(
            delivery_guid="failed-delivery",
            github_delivery_id=1234,
            event="pull_request",
            action="edited",
            installation_id=123,
            repository_id=100,
            pr_number=7,
            received_at=self.now,
            raw_body=b'{"private":"payload"}',
        )
        await self.store.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        await self.store.fail_delivery(
            "failed-delivery",
            completed_at=self.now,
            error_summary="terminal failure",
        )
        await self.store.accept_delivery(
            delivery_guid="awaiting-redelivery",
            github_delivery_id=1235,
            event="pull_request",
            action="edited",
            installation_id=123,
            repository_id=100,
            pr_number=8,
            received_at=self.now,
            raw_body=b'{"private":"redelivery"}',
        )
        awaiting = await self.store.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertIsNotNone(awaiting)
        assert awaiting is not None
        await self.store.fail_delivery(
            awaiting.delivery_guid,
            completed_at=self.now,
            error_summary="retry through GitHub",
        )
        await self.store.prepare_delivery_redelivery(
            awaiting.delivery_guid,
            github_delivery_id=1235,
            now=self.now,
            next_attempt_at=self.now + timedelta(minutes=1),
        )

        self.assertEqual(
            await self.store.prune_deliveries(self.now + timedelta(days=4)),
            (2, 0),
        )
        retained = await self.store.get_delivery("failed-delivery")
        self.assertIsNone(retained.raw_body)
        self.assertEqual(retained.github_delivery_id, 1234)
        awaiting_retained = await self.store.get_delivery("awaiting-redelivery")
        self.assertEqual(
            awaiting_retained.state,
            models.GitHubDeliveryState.AWAITING_REDELIVERY,
        )
        self.assertIsNone(awaiting_retained.raw_body)
        self.assertEqual(
            await self.store.prune_deliveries(self.now + timedelta(days=8)),
            (0, 2),
        )
        self.assertIsNone(await self.store.get_delivery("failed-delivery"))
        self.assertIsNone(await self.store.get_delivery("awaiting-redelivery"))

    async def test_delivery_retry_ignored_and_terminal_states_are_durable(self):
        await self.store.accept_delivery(
            delivery_guid="retry-delivery",
            github_delivery_id=None,
            event="pull_request",
            action="edited",
            installation_id=123,
            repository_id=100,
            pr_number=7,
            received_at=self.now,
            raw_body=b"retry body",
        )
        retry = await self.store.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        retry_at = self.now + timedelta(minutes=5)
        self.assertTrue(
            await self.store.defer_delivery(
                retry.delivery_guid,
                next_attempt_at=retry_at,
                error_summary="x" * 1_000,
            )
        )
        self.assertFalse(
            await self.store.accept_delivery(
                delivery_guid="retry-delivery",
                github_delivery_id=None,
                event="pull_request",
                action="edited",
                installation_id=123,
                repository_id=100,
                pr_number=7,
                received_at=self.now + timedelta(minutes=1),
                raw_body=b"retry duplicate",
            )
        )
        await self.store.accept_delivery(
            delivery_guid="ignored-delivery",
            github_delivery_id=None,
            event="unknown_event",
            action=None,
            installation_id=123,
            repository_id=None,
            pr_number=None,
            received_at=self.now,
            raw_body=b"ignored body",
        )
        ignored = await self.store.claim_next_delivery(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertTrue(
            await self.store.complete_delivery(
                ignored.delivery_guid,
                completed_at=self.now,
                ignored=True,
            )
        )
        stored_ignored = await self.store.get_delivery(ignored.delivery_guid)
        self.assertEqual(stored_ignored.state, models.GitHubDeliveryState.IGNORED)
        self.assertIsNone(stored_ignored.raw_body)
        self.assertFalse(
            await self.store.accept_delivery(
                delivery_guid="ignored-delivery",
                github_delivery_id=None,
                event="unknown_event",
                action=None,
                installation_id=123,
                repository_id=None,
                pr_number=None,
                received_at=self.now + timedelta(minutes=1),
                raw_body=b"ignored duplicate",
            )
        )
        self.assertIsNone(
            await self.store.claim_next_delivery(
                now=self.now + timedelta(minutes=4),
                stale_before=self.now - timedelta(minutes=5),
            )
        )
        retried = await self.store.claim_next_delivery(
            now=retry_at,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(retried.delivery_guid, retry.delivery_guid)
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(len(retried.error_summary), store_module.MAX_ERROR_SUMMARY_LENGTH)
        self.assertTrue(
            await self.store.fail_delivery(
                retried.delivery_guid,
                completed_at=retry_at,
                error_summary="y" * 1_000,
            )
        )
        failed = await self.store.get_delivery(retried.delivery_guid)
        self.assertEqual(failed.state, models.GitHubDeliveryState.FAILED)
        self.assertEqual(len(failed.error_summary), store_module.MAX_ERROR_SUMMARY_LENGTH)
        self.assertIsNone(
            await self.store.claim_next_delivery(
                now=self.now + timedelta(days=1),
                stale_before=self.now + timedelta(hours=1),
            )
        )

    async def test_outbox_ordering_retry_and_terminal_states_are_durable(self):
        await self.create_pending_outbox(
            repository_id=102,
            pr_number=9,
            github_pr_id=900,
            assignee_id=201,
            github_login="zeta-user",
        )
        await self.create_pending_outbox(
            repository_id=103,
            pr_number=10,
            github_pr_id=1_000,
            assignee_id=202,
            github_login="alpha-user",
        )

        first = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(first.github_login, "zeta-user")
        retry_at = self.now + timedelta(minutes=5)
        self.assertTrue(
            await self.store.defer_outbox(
                first.outbox_id,
                next_attempt_at=retry_at,
                error_summary="x" * 1_000,
            )
        )
        second = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(second.github_login, "alpha-user")
        self.assertTrue(
            await self.store.fail_outbox(
                second.outbox_id,
                failed_at=self.now,
                error_summary="y" * 1_000,
            )
        )
        failed = await self.store.get_outbox_item(second.outbox_id)
        self.assertEqual(failed.state, models.GitHubOutboxState.FAILED)
        self.assertEqual(len(failed.error_summary), store_module.MAX_ERROR_SUMMARY_LENGTH)
        self.assertIsNone(
            await self.store.claim_next_outbox(
                now=self.now + timedelta(minutes=4),
                stale_before=self.now - timedelta(minutes=5),
            )
        )
        retried = await self.store.claim_next_outbox(
            now=retry_at,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(retried.outbox_id, first.outbox_id)
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(len(retried.error_summary), store_module.MAX_ERROR_SUMMARY_LENGTH)
        self.assertTrue(
            await self.store.complete_outbox(
                retried.outbox_id,
                completed_at=retry_at,
            )
        )
        self.assertIsNone(
            await self.store.claim_next_outbox(
                now=self.now + timedelta(days=1),
                stale_before=self.now + timedelta(hours=1),
            )
        )

    async def test_deferred_outbox_intent_blocks_later_intents_for_same_pr(self):
        ticket = await self.create_pending_outbox(
            repository_id=102,
            pr_number=9,
            github_pr_id=900,
            assignee_id=201,
            github_login="reviewer",
        )
        add_intent = await self.store.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        retry_at = self.now + timedelta(minutes=5)
        self.assertTrue(
            await self.store.defer_outbox(
                add_intent.outbox_id,
                next_attempt_at=retry_at,
                error_summary="temporary failure",
            )
        )
        self.assertEqual(
            await self.store.unassign_with_github_outbox(
                ticket.ticket_id,
                github_login="reviewer",
                protection_until=self.now + timedelta(minutes=1),
                next_action=None,
                next_action_at=None,
                updated_at=self.now + timedelta(seconds=1),
            ),
            201,
        )

        self.assertIsNone(
            await self.store.claim_next_outbox(
                now=self.now + timedelta(seconds=1),
                stale_before=self.now - timedelta(minutes=5),
            )
        )
        retried_add = await self.store.claim_next_outbox(
            now=retry_at,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(retried_add.outbox_id, add_intent.outbox_id)
        self.assertTrue(
            await self.store.complete_outbox(
                retried_add.outbox_id,
                completed_at=retry_at,
            )
        )
        remove_intent = await self.store.claim_next_outbox(
            now=retry_at,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(
            remove_intent.operation,
            models.GitHubOutboxOperation.REMOVE_ASSIGNEE,
        )

    async def test_claim_and_unassign_commit_outbox_intents_atomically(self):
        self.assertTrue(
            hasattr(self.store, "claim_with_github_outbox"),
            "the store must own the local transition and outbox transaction",
        )
        ticket = await self.store.create_ticket_for_pull_request(
            self.new_ticket(author_id=111),
            self.pull_request(),
        )
        await self.store.activate_ticket(
            ticket.ticket_id,
            message_id=300,
            thread_id=400,
            protection_until=self.now,
            next_action=None,
            next_action_at=None,
            updated_at=self.now,
        )

        self.assertTrue(
            await self.store.claim_with_github_outbox(
                ticket.ticket_id,
                assignee_id=222,
                github_login=" OctoCat ",
                protection_until=self.now + timedelta(minutes=5),
                updated_at=self.now,
            )
        )
        claimed_ticket = await self.store.get_ticket(ticket.ticket_id)
        self.assertEqual(claimed_ticket.state, models.TicketState.CLAIMED)
        self.assertEqual(claimed_ticket.assignee_id, 222)

        reopened = store_module.GitHubTicketsStore(self.path)
        await reopened.initialize()
        add_intent = await reopened.claim_next_outbox(
            now=self.now,
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(
            add_intent.operation,
            models.GitHubOutboxOperation.ADD_ASSIGNEE,
        )
        self.assertEqual(add_intent.github_login, "octocat")
        self.assertEqual(add_intent.actor_user_id, 222)
        self.assertEqual(add_intent.repository_id, 100)
        self.assertEqual(add_intent.repository_full_name, "NewHorizons/NHCogs")
        self.assertEqual(add_intent.pr_number, 7)
        self.assertEqual(add_intent.transition_version, claimed_ticket.transition_version)
        self.assertTrue(
            await reopened.complete_outbox(
                add_intent.outbox_id,
                completed_at=self.now + timedelta(seconds=1),
            )
        )

        with closing(store_module.connect(self.path)) as connection:
            connection.execute(
                f"""
                CREATE TRIGGER reject_test_remove_outbox
                BEFORE INSERT ON github_outbox
                WHEN NEW.ticket_id = {ticket.ticket_id}
                    AND NEW.operation = 'remove_assignee'
                BEGIN
                    SELECT RAISE(ABORT, 'test remove outbox failure');
                END
                """
            )
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await reopened.unassign_with_github_outbox(
                ticket.ticket_id,
                github_login="octocat",
                protection_until=self.now + timedelta(minutes=10),
                next_action=None,
                next_action_at=None,
                updated_at=self.now + timedelta(seconds=2),
            )
        still_claimed = await reopened.get_ticket(ticket.ticket_id)
        self.assertEqual(still_claimed.state, models.TicketState.CLAIMED)
        self.assertEqual(still_claimed.assignee_id, 222)
        with closing(store_module.connect(self.path)) as connection:
            connection.execute("DROP TRIGGER reject_test_remove_outbox")
            connection.commit()

        self.assertEqual(
            await reopened.unassign_with_github_outbox(
                ticket.ticket_id,
                github_login="OCTOCAT",
                protection_until=self.now + timedelta(minutes=10),
                next_action=None,
                next_action_at=None,
                updated_at=self.now + timedelta(seconds=2),
            ),
            222,
        )
        remove_intent = await reopened.claim_next_outbox(
            now=self.now + timedelta(seconds=2),
            stale_before=self.now - timedelta(minutes=5),
        )
        self.assertEqual(
            remove_intent.operation,
            models.GitHubOutboxOperation.REMOVE_ASSIGNEE,
        )
        self.assertEqual(remove_intent.github_login, "octocat")
        self.assertEqual(remove_intent.repository_full_name, "NewHorizons/NHCogs")
        self.assertEqual(remove_intent.actor_user_id, 222)
        recovered_remove = await reopened.claim_next_outbox(
            now=self.now + timedelta(minutes=10),
            stale_before=self.now + timedelta(minutes=1),
        )
        self.assertEqual(recovered_remove.outbox_id, remove_intent.outbox_id)
        self.assertEqual(recovered_remove.attempts, 2)

        second = await reopened.create_ticket_for_pull_request(
            self.new_ticket(author_id=333),
            self.pull_request(
                repository_id=101,
                pr_number=8,
                github_pr_id=800,
                updated_at=self.now + timedelta(minutes=1),
            ),
        )
        await reopened.activate_ticket(
            second.ticket_id,
            message_id=301,
            thread_id=401,
            protection_until=self.now,
            next_action=None,
            next_action_at=None,
            updated_at=self.now,
        )
        with closing(store_module.connect(self.path)) as connection:
            connection.execute(
                f"""
                CREATE TRIGGER reject_test_outbox
                BEFORE INSERT ON github_outbox
                WHEN NEW.ticket_id = {second.ticket_id}
                BEGIN
                    SELECT RAISE(ABORT, 'test outbox failure');
                END
                """
            )
            connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            await reopened.claim_with_github_outbox(
                second.ticket_id,
                assignee_id=444,
                github_login="other-user",
                protection_until=self.now + timedelta(minutes=5),
                updated_at=self.now,
            )
        unchanged = await reopened.get_ticket(second.ticket_id)
        self.assertEqual(unchanged.state, models.TicketState.OPEN)
        self.assertIsNone(unchanged.assignee_id)
