from __future__ import annotations

import asyncio
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from NHCogs.storage import ConnectionFactory, apply_migrations, connect

from .models import (
    CandidateHistory,
    Category,
    CategoryAlreadyExists,
    CategoryLimitReached,
    ExclusionReason,
    InvalidCategoryName,
    NewTicket,
    NextAction,
    PingReservation,
    PresenceTier,
    Profile,
    RoutingMode,
    Ticket,
    TicketExclusion,
    TicketPing,
    TicketState,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_CATEGORIES = 25
MAX_CATEGORY_NAME_LENGTH = 100


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _serialize_optional_datetime(value: datetime | None) -> str | None:
    return _serialize_datetime(value) if value is not None else None


def _deserialize_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _deserialize_optional_datetime(value: object) -> datetime | None:
    return _deserialize_datetime(str(value)) if value is not None else None


def _normalize_category_name(name: str) -> str:
    normalized = name.strip().lower()
    if not normalized or len(normalized) > MAX_CATEGORY_NAME_LENGTH:
        raise InvalidCategoryName(name)
    return normalized


def _decode_category(row: sqlite3.Row) -> Category:
    return Category(
        category_id=int(row["category_id"]),
        guild_id=int(row["guild_id"]),
        name=str(row["name"]),
        created_at=_deserialize_datetime(str(row["created_at"])),
    )


def _decode_profile(connection: sqlite3.Connection, row: sqlite3.Row) -> Profile:
    category_ids = tuple(
        int(category_row["category_id"])
        for category_row in connection.execute(
            """
            SELECT pc.category_id
            FROM profile_categories AS pc
            JOIN categories AS c ON c.category_id = pc.category_id
            WHERE pc.guild_id = ? AND pc.user_id = ?
            ORDER BY c.name, pc.category_id
            """,
            (row["guild_id"], row["user_id"]),
        )
    )
    return Profile(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        github_username=(
            str(row["github_username"])
            if row["github_username"] is not None
            else None
        ),
        automatic_pings=bool(row["automatic_pings"]),
        category_ids=category_ids,
        updated_at=_deserialize_datetime(str(row["updated_at"])),
    )


def _decode_ticket(connection: sqlite3.Connection, row: sqlite3.Row) -> Ticket:
    category_ids = tuple(
        int(category_row["category_id"])
        for category_row in connection.execute(
            """
            SELECT tc.category_id
            FROM ticket_categories AS tc
            JOIN categories AS c ON c.category_id = tc.category_id
            WHERE tc.ticket_id = ?
            ORDER BY c.name, tc.category_id
            """,
            (row["ticket_id"],),
        )
    )
    return Ticket(
        ticket_id=int(row["ticket_id"]),
        guild_id=int(row["guild_id"]),
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        thread_id=int(row["thread_id"]) if row["thread_id"] is not None else None,
        author_id=int(row["author_id"]),
        pr_title=str(row["pr_title"]),
        pr_url=str(row["pr_url"]),
        category_display=str(row["category_display"]),
        routing_mode=RoutingMode(str(row["routing_mode"])),
        state=TicketState(str(row["state"])),
        direct_target_id=(
            int(row["direct_target_id"]) if row["direct_target_id"] is not None else None
        ),
        current_target_id=(
            int(row["current_target_id"])
            if row["current_target_id"] is not None
            else None
        ),
        assignee_id=int(row["assignee_id"]) if row["assignee_id"] is not None else None,
        ping_count=int(row["ping_count"]),
        protection_until=_deserialize_optional_datetime(row["protection_until"]),
        next_action=(
            NextAction(str(row["next_action"])) if row["next_action"] is not None else None
        ),
        next_action_at=_deserialize_optional_datetime(row["next_action_at"]),
        pending_target_id=(
            int(row["pending_target_id"])
            if row["pending_target_id"] is not None
            else None
        ),
        pending_presence_tier=(
            PresenceTier(str(row["pending_presence_tier"]))
            if row["pending_presence_tier"] is not None
            else None
        ),
        pending_ping_automatic=(
            bool(row["pending_ping_automatic"])
            if row["pending_ping_automatic"] is not None
            else None
        ),
        pending_ping_reserved_at=_deserialize_optional_datetime(
            row["pending_ping_reserved_at"]
        ),
        pending_response_deadline=_deserialize_optional_datetime(
            row["pending_response_deadline"]
        ),
        created_at=_deserialize_datetime(str(row["created_at"])),
        updated_at=_deserialize_datetime(str(row["updated_at"])),
        transition_version=int(row["transition_version"]),
        category_ids=category_ids,
        public_token=str(row["public_token"]),
    )
def _create_schema(connection: sqlite3.Connection) -> None:
    schema = """
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
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)


MIGRATIONS = (_create_schema,)


class GitHubTicketsStore:
    def __init__(
        self,
        path: str | Path,
        *,
        connection_factory: ConnectionFactory = sqlite3.connect,
    ) -> None:
        self._path = Path(path)
        self._connection_factory = connection_factory
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def add_category(
        self,
        guild_id: int,
        name: str,
        created_at: datetime,
    ) -> Category:
        normalized = _normalize_category_name(name)
        async with self._lock:
            return await asyncio.to_thread(
                self._add_category_sync,
                guild_id,
                normalized,
                created_at,
            )

    async def list_categories(self, guild_id: int) -> tuple[Category, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_categories_sync, guild_id)

    async def delete_category(self, category_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_category_sync, category_id)

    async def save_profile(
        self,
        *,
        guild_id: int,
        user_id: int,
        github_username: str | None,
        category_ids: Iterable[int],
        automatic_pings: bool,
        updated_at: datetime,
    ) -> Profile | None:
        username = github_username.strip() if github_username else ""
        normalized_username = username or None
        normalized_category_ids = tuple(dict.fromkeys(category_ids))
        if automatic_pings and not normalized_category_ids:
            raise ValueError("automatic pings require at least one category")
        profile = Profile(
            guild_id=guild_id,
            user_id=user_id,
            github_username=normalized_username,
            automatic_pings=automatic_pings,
            category_ids=normalized_category_ids,
            updated_at=updated_at,
        )
        async with self._lock:
            return await asyncio.to_thread(self._save_profile_sync, profile)

    async def get_profile(self, guild_id: int, user_id: int) -> Profile | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_profile_sync, guild_id, user_id)

    async def list_profiles_for_category(
        self,
        guild_id: int,
        category_id: int,
    ) -> tuple[Profile, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_profiles_for_category_sync,
                guild_id,
                category_id,
            )

    async def candidate_history(
        self,
        ticket_id: int,
        candidate_user_ids: Iterable[int],
    ) -> tuple[CandidateHistory, ...]:
        user_ids = tuple(dict.fromkeys(candidate_user_ids))
        if not user_ids:
            return ()
        async with self._lock:
            return await asyncio.to_thread(
                self._candidate_history_sync,
                ticket_id,
                user_ids,
            )

    async def create_ticket(self, new_ticket: NewTicket) -> Ticket:
        async with self._lock:
            return await asyncio.to_thread(self._create_ticket_sync, new_ticket)

    async def activate_ticket(
        self,
        ticket_id: int,
        *,
        message_id: int,
        thread_id: int,
        protection_until: datetime | None,
        next_action: NextAction | None,
        next_action_at: datetime | None,
        updated_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._activate_ticket_sync,
                ticket_id,
                (message_id, thread_id),
                (protection_until, next_action, next_action_at),
                updated_at,
            )

    async def record_ticket_message(
        self,
        ticket_id: int,
        message_id: int,
        updated_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._record_ticket_message_sync,
                ticket_id,
                message_id,
                updated_at,
            )

    async def record_ticket_thread(
        self,
        ticket_id: int,
        thread_id: int,
        updated_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._record_ticket_thread_sync,
                ticket_id,
                thread_id,
                updated_at,
            )

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_ticket_sync, ticket_id)

    async def get_ticket_by_public_token(self, public_token: str) -> Ticket | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_ticket_by_public_token_sync,
                public_token,
            )

    async def get_projection_sync_ticket(self, ticket_id: int) -> Ticket | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_projection_sync_ticket_sync,
                ticket_id,
            )

    async def acknowledge_projection_sync(
        self,
        ticket_id: int,
        transition_version: int,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._acknowledge_projection_sync_sync,
                ticket_id,
                transition_version,
            )

    async def defer_projection_sync(
        self,
        ticket_id: int,
        transition_version: int,
        retry_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._defer_projection_sync_sync,
                ticket_id,
                transition_version,
                retry_at,
            )

    async def get_ticket_by_message_id(self, message_id: int) -> Ticket | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_ticket_by_projection_id_sync,
                "message_id",
                message_id,
            )

    async def get_ticket_by_thread_id(self, thread_id: int) -> Ticket | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_ticket_by_projection_id_sync,
                "thread_id",
                thread_id,
            )

    async def list_active_tickets(self) -> tuple[Ticket, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_active_tickets_sync)

    async def list_projection_cleanup_tickets(self) -> tuple[Ticket, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_projection_cleanup_tickets_sync)

    async def claim(
        self,
        ticket_id: int,
        assignee_id: int,
        protection_until: datetime,
        updated_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_sync,
                ticket_id,
                assignee_id,
                protection_until,
                updated_at,
            )

    async def decline(
        self,
        ticket_id: int,
        user_id: int,
        updated_at: datetime,
        *,
        protection_until: datetime | None = None,
        next_action: NextAction | None = None,
        next_action_at: datetime | None = None,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._decline_sync,
                ticket_id,
                user_id,
                updated_at,
                (protection_until, next_action, next_action_at),
            )

    async def unassign(
        self,
        ticket_id: int,
        *,
        protection_until: datetime,
        next_action: NextAction | None,
        next_action_at: datetime | None,
        updated_at: datetime,
    ) -> int | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._unassign_sync,
                ticket_id,
                protection_until,
                next_action,
                next_action_at,
                updated_at,
            )

    async def list_exclusions(self, ticket_id: int) -> tuple[TicketExclusion, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_exclusions_sync, ticket_id)

    async def reserve_ping(
        self,
        ticket_id: int,
        *,
        target_user_id: int,
        presence_tier: PresenceTier | None,
        automatic: bool,
        reserved_at: datetime,
        response_deadline: datetime,
        maximum_pings: int,
    ) -> PingReservation | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_ping_sync,
                ticket_id,
                (
                    target_user_id,
                    presence_tier,
                    automatic,
                    reserved_at,
                    response_deadline,
                ),
                maximum_pings,
            )

    async def acknowledge_ping(
        self,
        ticket_id: int,
        sent_at: datetime,
    ) -> TicketPing | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._acknowledge_ping_sync,
                ticket_id,
                sent_at,
            )

    async def settle_target_timeout(
        self,
        ticket_id: int,
        *,
        target_user_id: int,
        protection_until: datetime,
        next_action: NextAction | None,
        next_action_at: datetime | None,
        updated_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._settle_target_timeout_sync,
                ticket_id,
                target_user_id,
                (protection_until, next_action, next_action_at, updated_at),
            )

    async def defer_due_ping(
        self,
        ticket_id: int,
        next_action_at: datetime,
        updated_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._defer_due_ping_sync,
                ticket_id,
                next_action_at,
                updated_at,
            )

    async def exhaust_due_routing(
        self,
        ticket_id: int,
        expected_action: NextAction,
        updated_at: datetime,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._exhaust_due_routing_sync,
                ticket_id,
                expected_action,
                updated_at,
            )

    async def list_pings(self, ticket_id: int) -> tuple[TicketPing, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_pings_sync, ticket_id)

    async def nearest_deadline(self) -> datetime | None:
        async with self._lock:
            return await asyncio.to_thread(self._nearest_deadline_sync)

    async def due_ticket_ids(self, now: datetime) -> tuple[int, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._due_ticket_ids_sync, now)

    async def delete_tickets_for_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> tuple[Ticket, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_tickets_for_channel_sync,
                guild_id,
                channel_id,
            )

    async def delete_guild_state(self, guild_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_guild_state_sync, guild_id)

    async def list_authored_tickets(self, user_id: int) -> tuple[Ticket, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_authored_tickets_sync, user_id)

    async def begin_authored_ticket_cleanup(
        self,
        ticket_id: int,
        *,
        author_id: int,
        updated_at: datetime,
    ) -> Ticket | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._begin_authored_ticket_cleanup_sync,
                ticket_id,
                author_id,
                updated_at,
            )

    async def user_reference_guild_ids(self, user_id: int) -> tuple[int, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._user_reference_guild_ids_sync, user_id)

    async def user_reference_ticket_ids(self, user_id: int) -> tuple[int, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._user_reference_ticket_ids_sync, user_id)

    async def redact_user(
        self,
        user_id: int,
        *,
        protection_until_by_guild: Mapping[int, datetime],
        updated_at: datetime,
    ) -> tuple[Ticket, ...]:
        deadlines = dict(protection_until_by_guild)
        async with self._lock:
            return await asyncio.to_thread(
                self._redact_user_sync,
                user_id,
                deadlines,
                updated_at,
            )

    async def begin_finishing(
        self,
        ticket_id: int,
        updated_at: datetime,
        *,
        message_absent: bool = False,
        thread_absent: bool = False,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._begin_finishing_sync,
                ticket_id,
                updated_at,
                message_absent,
                thread_absent,
            )

    async def delete_ticket(self, ticket_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_ticket_sync, ticket_id)

    def _connect(self) -> sqlite3.Connection:
        return connect(self._path, connection_factory=self._connection_factory)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            apply_migrations(connection, MIGRATIONS, label="GitHub Tickets")

    def _add_category_sync(
        self,
        guild_id: int,
        name: str,
        created_at: datetime,
    ) -> Category:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT 1 FROM categories WHERE guild_id = ? AND name = ?",
                    (guild_id, name),
                ).fetchone()
                if existing is not None:
                    raise CategoryAlreadyExists(name)
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM categories WHERE guild_id = ?",
                        (guild_id,),
                    ).fetchone()[0]
                )
                if count >= MAX_CATEGORIES:
                    raise CategoryLimitReached(guild_id)
                cursor = connection.execute(
                    """
                    INSERT INTO categories (guild_id, name, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (guild_id, name, _serialize_datetime(created_at)),
                )
                row = connection.execute(
                    "SELECT * FROM categories WHERE category_id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:
            raise RuntimeError("created category could not be loaded")
        return _decode_category(row)

    def _list_categories_sync(self, guild_id: int) -> tuple[Category, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM categories WHERE guild_id = ? ORDER BY name, category_id",
                (guild_id,),
            ).fetchall()
        return tuple(_decode_category(row) for row in rows)

    def _delete_category_sync(self, category_id: int) -> bool:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM categories WHERE category_id = ?",
                    (category_id,),
                )
                connection.execute(
                    """
                    UPDATE profiles
                    SET automatic_pings = 0
                    WHERE automatic_pings = 1
                      AND NOT EXISTS (
                          SELECT 1 FROM profile_categories AS pc
                          WHERE pc.guild_id = profiles.guild_id
                            AND pc.user_id = profiles.user_id
                      )
                    """
                )
                connection.execute(
                    """
                    DELETE FROM profiles
                    WHERE github_username IS NULL AND automatic_pings = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM profile_categories AS pc
                          WHERE pc.guild_id = profiles.guild_id
                            AND pc.user_id = profiles.user_id
                      )
                    """
                )
                connection.commit()
                return cursor.rowcount > 0
            except Exception:
                connection.rollback()
                raise

    def _save_profile_sync(self, profile: Profile) -> Profile | None:
        guild_id = profile.guild_id
        user_id = profile.user_id
        github_username = profile.github_username
        category_ids = profile.category_ids
        automatic_pings = profile.automatic_pings
        updated_at = profile.updated_at
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if github_username is None and not category_ids and not automatic_pings:
                    connection.execute(
                        "DELETE FROM profiles WHERE guild_id = ? AND user_id = ?",
                        (guild_id, user_id),
                    )
                    connection.commit()
                    return None

                valid_category_ids = {
                    int(row["category_id"])
                    for row in connection.execute(
                        "SELECT category_id FROM categories WHERE guild_id = ?",
                        (guild_id,),
                    )
                }
                if not set(category_ids).issubset(valid_category_ids):
                    raise ValueError("profile categories must belong to the guild")

                connection.execute(
                    """
                    INSERT INTO profiles (
                        guild_id, user_id, github_username, automatic_pings, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, user_id) DO UPDATE SET
                        github_username = excluded.github_username,
                        automatic_pings = excluded.automatic_pings,
                        updated_at = excluded.updated_at
                    """,
                    (
                        guild_id,
                        user_id,
                        github_username,
                        int(automatic_pings),
                        _serialize_datetime(updated_at),
                    ),
                )
                connection.execute(
                    "DELETE FROM profile_categories WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
                connection.executemany(
                    """
                    INSERT INTO profile_categories (guild_id, user_id, category_id)
                    VALUES (?, ?, ?)
                    """,
                    ((guild_id, user_id, category_id) for category_id in category_ids),
                )
                row = connection.execute(
                    "SELECT * FROM profiles WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:
            raise RuntimeError("saved profile could not be loaded")
        with closing(self._connect()) as connection:
            return _decode_profile(connection, row)

    def _get_profile_sync(self, guild_id: int, user_id: int) -> Profile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM profiles WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                return None
            return _decode_profile(connection, row)

    def _list_profiles_for_category_sync(
        self,
        guild_id: int,
        category_id: int,
    ) -> tuple[Profile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                    (
                        SELECT GROUP_CONCAT(ordered.category_id, ',')
                        FROM (
                            SELECT pc2.category_id
                            FROM profile_categories AS pc2
                            JOIN categories AS c2
                                ON c2.category_id = pc2.category_id
                            WHERE pc2.guild_id = p.guild_id
                                AND pc2.user_id = p.user_id
                            ORDER BY c2.name, pc2.category_id
                        ) AS ordered
                    ) AS category_ids
                FROM profiles AS p
                JOIN profile_categories AS requested
                    ON requested.guild_id = p.guild_id
                    AND requested.user_id = p.user_id
                    AND requested.category_id = ?
                JOIN categories AS c
                    ON c.category_id = requested.category_id
                    AND c.guild_id = p.guild_id
                WHERE p.guild_id = ?
                ORDER BY p.user_id
                """,
                (category_id, guild_id),
            ).fetchall()
        return tuple(
            Profile(
                guild_id=int(row["guild_id"]),
                user_id=int(row["user_id"]),
                github_username=(
                    str(row["github_username"])
                    if row["github_username"] is not None
                    else None
                ),
                automatic_pings=bool(row["automatic_pings"]),
                category_ids=tuple(
                    int(value)
                    for value in str(row["category_ids"] or "").split(",")
                    if value
                ),
                updated_at=_deserialize_datetime(str(row["updated_at"])),
            )
            for row in rows
        )

    def _candidate_history_sync(
        self,
        ticket_id: int,
        user_ids: tuple[int, ...],
    ) -> tuple[CandidateHistory, ...]:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TEMP TABLE candidate_history_input (
                    user_id INTEGER PRIMARY KEY,
                    ordinal INTEGER NOT NULL
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO candidate_history_input (user_id, ordinal)
                VALUES (?, ?)
                """,
                ((user_id, ordinal) for ordinal, user_id in enumerate(user_ids)),
            )
            rows = connection.execute(
                """
                WITH target AS (
                    SELECT ticket_id, guild_id
                    FROM tickets
                    WHERE ticket_id = ?
                ),
                matches AS (
                    SELECT pc.user_id, COUNT(*) AS matching_category_count
                    FROM profile_categories AS pc
                    JOIN ticket_categories AS tc
                        ON tc.category_id = pc.category_id
                    JOIN target ON target.ticket_id = tc.ticket_id
                    WHERE pc.guild_id = target.guild_id
                    GROUP BY pc.user_id
                ),
                assignments AS (
                    SELECT assignee_id AS user_id, COUNT(*) AS active_assignment_count
                    FROM tickets
                    JOIN target ON target.guild_id = tickets.guild_id
                    WHERE tickets.state = 'claimed' AND assignee_id IS NOT NULL
                    GROUP BY assignee_id
                ),
                last_pings AS (
                    SELECT tp.target_user_id AS user_id, MAX(tp.sent_at) AS last_ping_at
                    FROM ticket_pings AS tp
                    JOIN tickets AS ping_ticket
                        ON ping_ticket.ticket_id = tp.ticket_id
                    JOIN target ON target.guild_id = ping_ticket.guild_id
                    GROUP BY tp.target_user_id
                ),
                ticket_ping_facts AS (
                    SELECT target_user_id AS user_id, 1 AS was_pinged
                    FROM ticket_pings
                    WHERE ticket_id = ?
                    GROUP BY target_user_id
                ),
                exclusion_facts AS (
                    SELECT user_id,
                        MAX(reason = 'declined') AS declined,
                        MAX(reason = 'unassigned') AS unassigned,
                        MAX(reason = 'timed_out') AS timed_out
                    FROM ticket_exclusions
                    WHERE ticket_id = ?
                    GROUP BY user_id
                )
                SELECT input.user_id,
                    profile.user_id IS NOT NULL AS has_profile,
                    COALESCE(profile.automatic_pings, 0) AS automatic_pings,
                    COALESCE(matches.matching_category_count, 0)
                        AS matching_category_count,
                    COALESCE(assignments.active_assignment_count, 0)
                        AS active_assignment_count,
                    last_pings.last_ping_at,
                    COALESCE(ticket_ping_facts.was_pinged, 0) AS was_pinged,
                    COALESCE(exclusion_facts.declined, 0) AS declined,
                    COALESCE(exclusion_facts.unassigned, 0) AS unassigned,
                    COALESCE(exclusion_facts.timed_out, 0) AS timed_out
                FROM candidate_history_input AS input
                CROSS JOIN target
                LEFT JOIN profiles AS profile
                    ON profile.guild_id = target.guild_id
                    AND profile.user_id = input.user_id
                LEFT JOIN matches ON matches.user_id = input.user_id
                LEFT JOIN assignments ON assignments.user_id = input.user_id
                LEFT JOIN last_pings ON last_pings.user_id = input.user_id
                LEFT JOIN ticket_ping_facts
                    ON ticket_ping_facts.user_id = input.user_id
                LEFT JOIN exclusion_facts ON exclusion_facts.user_id = input.user_id
                ORDER BY input.ordinal
                """,
                (ticket_id, ticket_id, ticket_id),
            ).fetchall()
        return tuple(
            CandidateHistory(
                user_id=int(row["user_id"]),
                has_profile=bool(row["has_profile"]),
                automatic_pings=bool(row["automatic_pings"]),
                matching_category_count=int(row["matching_category_count"]),
                active_assignment_count=int(row["active_assignment_count"]),
                last_ping_at=_deserialize_optional_datetime(row["last_ping_at"]),
                was_pinged=bool(row["was_pinged"]),
                declined=bool(row["declined"]),
                unassigned=bool(row["unassigned"]),
                timed_out=bool(row["timed_out"]),
            )
            for row in rows
        )

    def _create_ticket_sync(self, new_ticket: NewTicket) -> Ticket:
        title = new_ticket.pr_title.strip()
        url = new_ticket.pr_url.strip()
        if not title or not url:
            raise ValueError("ticket title and URL cannot be empty")
        category_ids = tuple(dict.fromkeys(new_ticket.category_ids))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                valid_category_ids = {
                    int(row["category_id"])
                    for row in connection.execute(
                        "SELECT category_id FROM categories WHERE guild_id = ?",
                        (new_ticket.guild_id,),
                    )
                }
                if not set(category_ids).issubset(valid_category_ids):
                    raise ValueError("ticket categories must belong to the guild")
                timestamp = _serialize_datetime(new_ticket.created_at)
                cursor = connection.execute(
                    """
                    INSERT INTO tickets (
                        public_token, guild_id, channel_id, author_id, pr_title, pr_url,
                        category_display, routing_mode, state, direct_target_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?, ?)
                    """,
                    (
                        secrets.token_urlsafe(16),
                        new_ticket.guild_id,
                        new_ticket.channel_id,
                        new_ticket.author_id,
                        title,
                        url,
                        new_ticket.category_display,
                        new_ticket.routing_mode.value,
                        new_ticket.direct_target_id,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("ticket insert did not return an ID")
                ticket_id = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO ticket_categories (ticket_id, category_id) VALUES (?, ?)",
                    ((ticket_id, category_id) for category_id in category_ids),
                )
                row = connection.execute(
                    "SELECT * FROM tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("created ticket could not be loaded")
                ticket = _decode_ticket(connection, row)
                connection.commit()
                return ticket
            except Exception:
                connection.rollback()
                raise

    def _activate_ticket_sync(
        self,
        ticket_id: int,
        projection_ids: tuple[int, int],
        schedule: tuple[datetime | None, NextAction | None, datetime | None],
        updated_at: datetime,
    ) -> bool:
        message_id, thread_id = projection_ids
        protection_until, next_action, next_action_at = schedule
        cursor = self._update_ticket_state(
            """
            UPDATE tickets
            SET message_id = ?, thread_id = ?, state = 'open',
                protection_until = ?, next_action = ?, next_action_at = ?,
                projection_sync_at = NULL, updated_at = ?,
                transition_version = transition_version + 1
            WHERE ticket_id = ? AND state = 'creating'
            """,
            (
                message_id,
                thread_id,
                _serialize_optional_datetime(protection_until),
                next_action.value if next_action is not None else None,
                _serialize_optional_datetime(next_action_at),
                _serialize_datetime(updated_at),
                ticket_id,
            ),
        )
        return cursor > 0

    def _record_ticket_message_sync(
        self,
        ticket_id: int,
        message_id: int,
        updated_at: datetime,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET message_id = ?, updated_at = ?,
                transition_version = transition_version + 1
            WHERE ticket_id = ? AND state = 'creating' AND message_id IS NULL
            """,
            (
                message_id,
                _serialize_datetime(updated_at),
                ticket_id,
            ),
        )
        return changed > 0

    def _record_ticket_thread_sync(
        self,
        ticket_id: int,
        thread_id: int,
        updated_at: datetime,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET thread_id = ?, updated_at = ?,
                transition_version = transition_version + 1
            WHERE ticket_id = ? AND state = 'creating'
                AND message_id IS NOT NULL AND thread_id IS NULL
            """,
            (
                thread_id,
                _serialize_datetime(updated_at),
                ticket_id,
            ),
        )
        return changed > 0

    def _get_ticket_sync(self, ticket_id: int) -> Ticket | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            return _decode_ticket(connection, row) if row is not None else None

    def _get_ticket_by_public_token_sync(self, public_token: str) -> Ticket | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE public_token = ?",
                (public_token,),
            ).fetchone()
            return _decode_ticket(connection, row) if row is not None else None

    def _get_projection_sync_ticket_sync(self, ticket_id: int) -> Ticket | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM tickets
                WHERE ticket_id = ? AND state IN ('open', 'claimed')
                    AND projection_sync_at IS NOT NULL
                """,
                (ticket_id,),
            ).fetchone()
            return _decode_ticket(connection, row) if row is not None else None

    def _acknowledge_projection_sync_sync(
        self,
        ticket_id: int,
        transition_version: int,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET projection_sync_at = NULL
            WHERE ticket_id = ? AND transition_version = ?
                AND projection_sync_at IS NOT NULL
            """,
            (ticket_id, transition_version),
        )
        return changed > 0

    def _defer_projection_sync_sync(
        self,
        ticket_id: int,
        transition_version: int,
        retry_at: datetime,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET projection_sync_at = ?
            WHERE ticket_id = ? AND transition_version = ?
            """,
            (
                _serialize_datetime(retry_at),
                ticket_id,
                transition_version,
            ),
        )
        return changed > 0

    def _get_ticket_by_projection_id_sync(
        self,
        column: str,
        projection_id: int,
    ) -> Ticket | None:
        if column not in {"message_id", "thread_id"}:
            raise ValueError("unsupported ticket projection column")
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT * FROM tickets WHERE {column} = ?",  # noqa: S608
                (projection_id,),
            ).fetchone()
            return _decode_ticket(connection, row) if row is not None else None

    def _list_active_tickets_sync(self) -> tuple[Ticket, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tickets
                WHERE state IN ('open', 'claimed')
                ORDER BY ticket_id
                """
            ).fetchall()
            return tuple(_decode_ticket(connection, row) for row in rows)

    def _list_projection_cleanup_tickets_sync(self) -> tuple[Ticket, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tickets
                WHERE state IN ('creating', 'finishing')
                ORDER BY ticket_id
                """
            ).fetchall()
            return tuple(_decode_ticket(connection, row) for row in rows)

    def _claim_sync(
        self,
        ticket_id: int,
        assignee_id: int,
        protection_until: datetime,
        updated_at: datetime,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET state = 'claimed', assignee_id = ?, current_target_id = NULL,
                protection_until = ?, next_action = NULL, next_action_at = NULL,
                pending_target_id = NULL, pending_presence_tier = NULL,
                pending_ping_automatic = NULL,
                pending_ping_reserved_at = NULL,
                pending_response_deadline = NULL,
                updated_at = ?, projection_sync_at = ?,
                transition_version = transition_version + 1
            WHERE ticket_id = ? AND state = 'open'
            """,
            (
                assignee_id,
                _serialize_datetime(protection_until),
                _serialize_datetime(updated_at),
                _serialize_datetime(updated_at),
                ticket_id,
            ),
        )
        return changed > 0

    def _decline_sync(
        self,
        ticket_id: int,
        user_id: int,
        updated_at: datetime,
        schedule: tuple[datetime | None, NextAction | None, datetime | None],
    ) -> bool:
        protection_until, next_action, next_action_at = schedule
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT state, current_target_id, pending_target_id
                    FROM tickets WHERE ticket_id = ?
                    """,
                    (ticket_id,),
                ).fetchone()
                if row is None or row["state"] != TicketState.OPEN.value:
                    connection.rollback()
                    return False
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO ticket_exclusions (
                        ticket_id, user_id, reason, created_at
                    ) VALUES (?, ?, 'declined', ?)
                    """,
                    (ticket_id, user_id, _serialize_datetime(updated_at)),
                ).rowcount
                if inserted == 0:
                    connection.rollback()
                    return False
                if (
                    row["current_target_id"] == user_id
                    or row["pending_target_id"] == user_id
                ):
                    connection.execute(
                        """
                        UPDATE tickets
                        SET current_target_id = NULL, protection_until = ?,
                            next_action = ?, next_action_at = ?, updated_at = ?,
                            projection_sync_at = ?,
                            pending_target_id = NULL,
                            pending_presence_tier = NULL,
                            pending_ping_automatic = NULL,
                            pending_ping_reserved_at = NULL,
                            pending_response_deadline = NULL,
                            transition_version = transition_version + 1
                        WHERE ticket_id = ?
                        """,
                        (
                            _serialize_optional_datetime(protection_until),
                            next_action.value if next_action is not None else None,
                            _serialize_optional_datetime(next_action_at),
                            _serialize_datetime(updated_at),
                            _serialize_datetime(updated_at),
                            ticket_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE tickets
                        SET protection_until = COALESCE(?, protection_until),
                            updated_at = ?,
                            transition_version = transition_version + 1
                        WHERE ticket_id = ?
                        """,
                        (
                            _serialize_optional_datetime(protection_until),
                            _serialize_datetime(updated_at),
                            ticket_id,
                        ),
                    )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def _unassign_sync(
        self,
        ticket_id: int,
        protection_until: datetime,
        next_action: NextAction | None,
        next_action_at: datetime | None,
        updated_at: datetime,
    ) -> int | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT assignee_id FROM tickets WHERE ticket_id = ? AND state = 'claimed'",
                    (ticket_id,),
                ).fetchone()
                if row is None or row["assignee_id"] is None:
                    connection.rollback()
                    return None
                assignee_id = int(row["assignee_id"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO ticket_exclusions (
                        ticket_id, user_id, reason, created_at
                    ) VALUES (?, ?, 'unassigned', ?)
                    """,
                    (ticket_id, assignee_id, _serialize_datetime(updated_at)),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET state = 'open', assignee_id = NULL, current_target_id = NULL,
                        protection_until = ?, next_action = ?, next_action_at = ?,
                        pending_target_id = NULL, pending_presence_tier = NULL,
                        pending_ping_automatic = NULL,
                        pending_ping_reserved_at = NULL,
                        pending_response_deadline = NULL,
                        updated_at = ?, projection_sync_at = ?,
                        transition_version = transition_version + 1
                    WHERE ticket_id = ? AND state = 'claimed'
                    """,
                    (
                        _serialize_datetime(protection_until),
                        next_action.value if next_action is not None else None,
                        _serialize_optional_datetime(next_action_at),
                        _serialize_datetime(updated_at),
                        _serialize_datetime(updated_at),
                        ticket_id,
                    ),
                )
                connection.commit()
                return assignee_id
            except Exception:
                connection.rollback()
                raise

    def _list_exclusions_sync(self, ticket_id: int) -> tuple[TicketExclusion, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM ticket_exclusions
                WHERE ticket_id = ? ORDER BY created_at, user_id
                """,
                (ticket_id,),
            ).fetchall()
        return tuple(
            TicketExclusion(
                ticket_id=int(row["ticket_id"]),
                user_id=int(row["user_id"]),
                reason=ExclusionReason(str(row["reason"])),
                created_at=_deserialize_datetime(str(row["created_at"])),
            )
            for row in rows
        )

    def _reserve_ping_sync(
        self,
        ticket_id: int,
        target: tuple[int, PresenceTier | None, bool, datetime, datetime],
        maximum_pings: int,
    ) -> PingReservation | None:
        target_user_id, presence_tier, automatic, reserved_at, response_deadline = target
        if maximum_pings <= 0:
            return None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                if row is not None and row["pending_target_id"] is not None:
                    connection.rollback()
                    return PingReservation(
                        ticket_id=ticket_id,
                        target_user_id=int(row["pending_target_id"]),
                        presence_tier=(
                            PresenceTier(str(row["pending_presence_tier"]))
                            if row["pending_presence_tier"] is not None
                            else None
                        ),
                        automatic=bool(row["pending_ping_automatic"]),
                        reserved_at=_deserialize_datetime(
                            str(row["pending_ping_reserved_at"])
                        ),
                        response_deadline=_deserialize_datetime(
                            str(row["pending_response_deadline"])
                        ),
                    )
                if (
                    row is None
                    or row["state"] != TicketState.OPEN.value
                    or int(row["ping_count"]) >= maximum_pings
                    or self._target_was_used(connection, ticket_id, target_user_id)
                ):
                    connection.rollback()
                    return None
                connection.execute(
                    """
                    UPDATE tickets
                    SET pending_target_id = ?, pending_presence_tier = ?,
                        pending_ping_automatic = ?, pending_ping_reserved_at = ?,
                        pending_response_deadline = ?,
                        updated_at = ?, transition_version = transition_version + 1
                    WHERE ticket_id = ? AND state = 'open'
                    """,
                    (
                        target_user_id,
                        presence_tier.value if presence_tier is not None else None,
                        int(automatic),
                        _serialize_datetime(reserved_at),
                        _serialize_datetime(response_deadline),
                        _serialize_datetime(reserved_at),
                        ticket_id,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return PingReservation(
            ticket_id=ticket_id,
            target_user_id=target_user_id,
            presence_tier=presence_tier,
            automatic=automatic,
            reserved_at=reserved_at.astimezone(timezone.utc),
            response_deadline=response_deadline.astimezone(timezone.utc),
        )

    def _acknowledge_ping_sync(
        self,
        ticket_id: int,
        sent_at: datetime,
    ) -> TicketPing | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM tickets WHERE ticket_id = ? AND state = 'open'",
                    (ticket_id,),
                ).fetchone()
                if row is None or row["pending_target_id"] is None:
                    connection.rollback()
                    return None
                sequence_number = int(row["ping_count"]) + 1
                target_user_id = int(row["pending_target_id"])
                presence_tier = (
                    PresenceTier(str(row["pending_presence_tier"]))
                    if row["pending_presence_tier"] is not None
                    else None
                )
                automatic = bool(row["pending_ping_automatic"])
                response_deadline = _deserialize_datetime(
                    str(row["pending_response_deadline"])
                )
                connection.execute(
                    """
                    INSERT INTO ticket_pings (
                        ticket_id, sequence_number, target_user_id, presence_tier,
                        automatic, sent_at, response_deadline
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        sequence_number,
                        target_user_id,
                        presence_tier.value if presence_tier is not None else None,
                        int(automatic),
                        _serialize_datetime(sent_at),
                        _serialize_datetime(response_deadline),
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET ping_count = ?, current_target_id = ?,
                        next_action = 'target_timeout', next_action_at = ?,
                        pending_target_id = NULL, pending_presence_tier = NULL,
                        pending_ping_automatic = NULL,
                        pending_ping_reserved_at = NULL,
                        pending_response_deadline = NULL,
                        updated_at = ?, projection_sync_at = ?,
                        transition_version = transition_version + 1
                    WHERE ticket_id = ? AND state = 'open'
                    """,
                    (
                        sequence_number,
                        target_user_id,
                        _serialize_datetime(response_deadline),
                        _serialize_datetime(sent_at),
                        _serialize_datetime(sent_at),
                        ticket_id,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return TicketPing(
            ticket_id=ticket_id,
            sequence_number=sequence_number,
            target_user_id=target_user_id,
            presence_tier=presence_tier,
            automatic=automatic,
            sent_at=sent_at.astimezone(timezone.utc),
            response_deadline=response_deadline,
        )

    def _settle_target_timeout_sync(
        self,
        ticket_id: int,
        target_user_id: int,
        settlement: tuple[
            datetime,
            NextAction | None,
            datetime | None,
            datetime,
        ],
    ) -> bool:
        protection_until, next_action, next_action_at, updated_at = settlement
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT 1 FROM tickets
                    WHERE ticket_id = ? AND state = 'open'
                        AND current_target_id = ? AND next_action = 'target_timeout'
                    """,
                    (ticket_id, target_user_id),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                connection.execute(
                    """
                    INSERT OR IGNORE INTO ticket_exclusions (
                        ticket_id, user_id, reason, created_at
                    ) VALUES (?, ?, 'timed_out', ?)
                    """,
                    (ticket_id, target_user_id, _serialize_datetime(updated_at)),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET current_target_id = NULL, protection_until = ?,
                        next_action = ?, next_action_at = ?, updated_at = ?,
                        projection_sync_at = ?,
                        transition_version = transition_version + 1
                    WHERE ticket_id = ? AND state = 'open'
                        AND current_target_id = ?
                    """,
                    (
                        _serialize_datetime(protection_until),
                        next_action.value if next_action is not None else None,
                        _serialize_optional_datetime(next_action_at),
                        _serialize_datetime(updated_at),
                        _serialize_datetime(updated_at),
                        ticket_id,
                        target_user_id,
                    ),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def _defer_due_ping_sync(
        self,
        ticket_id: int,
        next_action_at: datetime,
        updated_at: datetime,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET next_action_at = ?, updated_at = ?,
                transition_version = transition_version + 1
            WHERE ticket_id = ? AND state = 'open'
                AND next_action IN ('direct_ping', 'automatic_ping')
            """,
            (
                _serialize_datetime(next_action_at),
                _serialize_datetime(updated_at),
                ticket_id,
            ),
        )
        return changed > 0

    def _exhaust_due_routing_sync(
        self,
        ticket_id: int,
        expected_action: NextAction,
        updated_at: datetime,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET next_action = NULL, next_action_at = NULL,
                pending_target_id = NULL, pending_presence_tier = NULL,
                pending_ping_automatic = NULL,
                pending_ping_reserved_at = NULL,
                pending_response_deadline = NULL,
                updated_at = ?, transition_version = transition_version + 1
            WHERE ticket_id = ? AND state = 'open' AND next_action = ?
            """,
            (
                _serialize_datetime(updated_at),
                ticket_id,
                expected_action.value,
            ),
        )
        return changed > 0

    @staticmethod
    def _target_was_used(
        connection: sqlite3.Connection,
        ticket_id: int,
        target_user_id: int,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM ticket_pings
                WHERE ticket_id = ? AND target_user_id = ?
                UNION ALL
                SELECT 1 FROM ticket_exclusions
                WHERE ticket_id = ? AND user_id = ?
                LIMIT 1
                """,
                (ticket_id, target_user_id, ticket_id, target_user_id),
            ).fetchone()
            is not None
        )

    def _list_pings_sync(self, ticket_id: int) -> tuple[TicketPing, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM ticket_pings
                WHERE ticket_id = ? ORDER BY sequence_number
                """,
                (ticket_id,),
            ).fetchall()
        return tuple(
            TicketPing(
                ticket_id=int(row["ticket_id"]),
                sequence_number=int(row["sequence_number"]),
                target_user_id=int(row["target_user_id"]),
                presence_tier=(
                    PresenceTier(str(row["presence_tier"]))
                    if row["presence_tier"] is not None
                    else None
                ),
                automatic=bool(row["automatic"]),
                sent_at=_deserialize_datetime(str(row["sent_at"])),
                response_deadline=_deserialize_datetime(str(row["response_deadline"])),
            )
            for row in rows
        )

    def _nearest_deadline_sync(self) -> datetime | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT MIN(deadline) AS deadline
                FROM (
                    SELECT next_action_at AS deadline
                    FROM tickets
                    WHERE state = 'open' AND next_action_at IS NOT NULL
                    UNION ALL
                    SELECT projection_sync_at AS deadline
                    FROM tickets
                    WHERE projection_sync_at IS NOT NULL
                )
                """
            ).fetchone()
        return _deserialize_optional_datetime(row["deadline"])

    def _due_ticket_ids_sync(self, now: datetime) -> tuple[int, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT ticket_id FROM tickets
                WHERE (
                    state = 'open' AND next_action_at <= ?
                ) OR (
                    projection_sync_at <= ?
                )
                ORDER BY CASE
                    WHEN projection_sync_at IS NOT NULL THEN projection_sync_at
                    ELSE next_action_at
                END, ticket_id
                """,
                (_serialize_datetime(now), _serialize_datetime(now)),
            ).fetchall()
        return tuple(int(row["ticket_id"]) for row in rows)

    def _delete_tickets_for_channel_sync(
        self,
        guild_id: int,
        channel_id: int,
    ) -> tuple[Ticket, ...]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT * FROM tickets
                    WHERE guild_id = ? AND channel_id = ?
                    ORDER BY ticket_id
                    """,
                    (guild_id, channel_id),
                ).fetchall()
                tickets = tuple(_decode_ticket(connection, row) for row in rows)
                connection.execute(
                    "DELETE FROM tickets WHERE guild_id = ? AND channel_id = ?",
                    (guild_id, channel_id),
                )
                connection.commit()
                return tickets
            except Exception:
                connection.rollback()
                raise

    def _delete_guild_state_sync(self, guild_id: int) -> bool:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = 0
                changed += connection.execute(
                    "DELETE FROM tickets WHERE guild_id = ?",
                    (guild_id,),
                ).rowcount
                changed += connection.execute(
                    "DELETE FROM profiles WHERE guild_id = ?",
                    (guild_id,),
                ).rowcount
                changed += connection.execute(
                    "DELETE FROM categories WHERE guild_id = ?",
                    (guild_id,),
                ).rowcount
                connection.commit()
                return changed > 0
            except Exception:
                connection.rollback()
                raise

    def _list_authored_tickets_sync(self, user_id: int) -> tuple[Ticket, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM tickets
                WHERE author_id = ?
                ORDER BY ticket_id
                """,
                (user_id,),
            ).fetchall()
            return tuple(_decode_ticket(connection, row) for row in rows)

    def _user_reference_guild_ids_sync(self, user_id: int) -> tuple[int, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT guild_id FROM profiles WHERE user_id = ?
                UNION
                SELECT guild_id FROM tickets
                WHERE author_id = ? OR direct_target_id = ?
                    OR current_target_id = ? OR pending_target_id = ?
                    OR assignee_id = ?
                UNION
                SELECT ticket.guild_id
                FROM ticket_exclusions AS exclusion
                JOIN tickets AS ticket ON ticket.ticket_id = exclusion.ticket_id
                WHERE exclusion.user_id = ?
                UNION
                SELECT ticket.guild_id
                FROM ticket_pings AS ping
                JOIN tickets AS ticket ON ticket.ticket_id = ping.ticket_id
                WHERE ping.target_user_id = ?
                ORDER BY guild_id
                """,
                (user_id,) * 8,
            ).fetchall()
        return tuple(int(row["guild_id"]) for row in rows)

    def _user_reference_ticket_ids_sync(self, user_id: int) -> tuple[int, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT ticket_id FROM tickets
                WHERE author_id <> ? AND (
                    direct_target_id = ? OR current_target_id = ?
                    OR pending_target_id = ? OR assignee_id = ?
                )
                ORDER BY ticket_id
                """,
                (user_id, user_id, user_id, user_id, user_id),
            ).fetchall()
        return tuple(int(row["ticket_id"]) for row in rows)

    def _redact_user_sync(
        self,
        user_id: int,
        protection_until_by_guild: dict[int, datetime],
        updated_at: datetime,
    ) -> tuple[Ticket, ...]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                affected_rows = connection.execute(
                    """
                    SELECT ticket_id, guild_id,
                        CASE
                            WHEN state = 'open'
                                OR current_target_id = ?
                                OR pending_target_id = ?
                                OR assignee_id = ?
                            THEN 1
                            ELSE 0
                        END AS reopen
                    FROM tickets
                    WHERE author_id <> ? AND state IN ('open', 'claimed')
                        AND (
                            current_target_id = ?
                            OR pending_target_id = ?
                            OR assignee_id = ?
                            OR direct_target_id = ?
                        )
                    ORDER BY ticket_id
                    """,
                    (
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                    ),
                ).fetchall()
                affected_guild_ids = {
                    int(row["guild_id"])
                    for row in affected_rows
                    if bool(row["reopen"])
                }
                missing_deadlines = affected_guild_ids.difference(
                    protection_until_by_guild
                )
                if missing_deadlines:
                    raise ValueError(
                        "a protection deadline is required for every affected guild"
                    )
                serialized_deadlines = {
                    guild_id: _serialize_datetime(
                        protection_until_by_guild[guild_id]
                    )
                    for guild_id in affected_guild_ids
                }
                updated_timestamp = _serialize_datetime(updated_at)
                connection.execute(
                    """
                    CREATE TEMP TABLE redacted_user_affected_tickets (
                        ticket_id INTEGER PRIMARY KEY,
                        reopen INTEGER NOT NULL CHECK (reopen IN (0, 1))
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO redacted_user_affected_tickets (ticket_id, reopen)
                    VALUES (?, ?)
                    """,
                    (
                        (int(row["ticket_id"]), int(row["reopen"]))
                        for row in affected_rows
                    ),
                )

                connection.execute(
                    "DELETE FROM tickets WHERE author_id = ?",
                    (user_id,),
                )
                connection.execute(
                    "DELETE FROM profiles WHERE user_id = ?",
                    (user_id,),
                )
                connection.execute(
                    "DELETE FROM ticket_exclusions WHERE user_id = ?",
                    (user_id,),
                )
                connection.execute(
                    "DELETE FROM ticket_pings WHERE target_user_id = ?",
                    (user_id,),
                )

                for guild_id in sorted(affected_guild_ids):
                    deadline = serialized_deadlines[guild_id]
                    connection.execute(
                        """
                        UPDATE tickets
                        SET state = 'open',
                            direct_target_id = NULL,
                            current_target_id = NULL,
                            assignee_id = NULL,
                            pending_target_id = NULL,
                            pending_presence_tier = NULL,
                            pending_ping_automatic = NULL,
                            pending_ping_reserved_at = NULL,
                            pending_response_deadline = NULL,
                            protection_until = ?,
                            projection_sync_at = ?,
                            next_action = CASE
                                WHEN routing_mode IN (
                                    'automatic', 'direct_automatic'
                                ) THEN 'automatic_ping'
                                ELSE NULL
                            END,
                            next_action_at = CASE
                                WHEN routing_mode IN (
                                    'automatic', 'direct_automatic'
                                ) THEN ?
                                ELSE NULL
                            END,
                            updated_at = ?,
                            transition_version = transition_version + 1
                        WHERE guild_id = ?
                            AND ticket_id IN (
                                SELECT ticket_id
                                FROM redacted_user_affected_tickets
                                WHERE reopen = 1
                            )
                        """,
                        (
                            deadline,
                            updated_timestamp,
                            deadline,
                            updated_timestamp,
                            guild_id,
                        ),
                    )

                connection.execute(
                    """
                    UPDATE tickets
                    SET direct_target_id = NULL, updated_at = ?,
                        transition_version = transition_version + 1
                    WHERE state IN ('open', 'claimed') AND direct_target_id = ?
                    """,
                    (updated_timestamp, user_id),
                )

                connection.execute(
                    """
                    UPDATE tickets
                    SET current_target_id = CASE
                            WHEN current_target_id = ? THEN NULL
                            ELSE current_target_id
                        END,
                        assignee_id = CASE
                            WHEN assignee_id = ? THEN NULL
                            ELSE assignee_id
                        END,
                        pending_target_id = CASE
                            WHEN pending_target_id = ? THEN NULL
                            ELSE pending_target_id
                        END,
                        pending_presence_tier = CASE
                            WHEN pending_target_id = ? THEN NULL
                            ELSE pending_presence_tier
                        END,
                        pending_ping_automatic = CASE
                            WHEN pending_target_id = ? THEN NULL
                            ELSE pending_ping_automatic
                        END,
                        pending_ping_reserved_at = CASE
                            WHEN pending_target_id = ? THEN NULL
                            ELSE pending_ping_reserved_at
                        END,
                        pending_response_deadline = CASE
                            WHEN pending_target_id = ? THEN NULL
                            ELSE pending_response_deadline
                        END,
                        updated_at = ?,
                        transition_version = transition_version + 1
                    WHERE state NOT IN ('open', 'claimed')
                        AND (
                            current_target_id = ?
                            OR pending_target_id = ?
                            OR assignee_id = ?
                        )
                    """,
                    (
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                        updated_timestamp,
                        user_id,
                        user_id,
                        user_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET direct_target_id = NULL, updated_at = ?,
                        transition_version = transition_version + 1
                    WHERE state NOT IN ('open', 'claimed') AND direct_target_id = ?
                    """,
                    (updated_timestamp, user_id),
                )

                rows = connection.execute(
                    """
                    SELECT ticket.*
                    FROM tickets AS ticket
                    JOIN redacted_user_affected_tickets AS affected
                        ON affected.ticket_id = ticket.ticket_id
                    ORDER BY ticket.ticket_id
                    """
                ).fetchall()
                affected = tuple(_decode_ticket(connection, row) for row in rows)
                connection.commit()
                return affected
            except Exception:
                connection.rollback()
                raise

    def _begin_authored_ticket_cleanup_sync(
        self,
        ticket_id: int,
        author_id: int,
        updated_at: datetime,
    ) -> Ticket | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT 1 FROM tickets
                    WHERE ticket_id = ? AND author_id = ?
                    """,
                    (ticket_id, author_id),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None

                connection.execute(
                    "DELETE FROM ticket_categories WHERE ticket_id = ?",
                    (ticket_id,),
                )
                connection.execute(
                    "DELETE FROM ticket_exclusions WHERE ticket_id = ?",
                    (ticket_id,),
                )
                connection.execute(
                    "DELETE FROM ticket_pings WHERE ticket_id = ?",
                    (ticket_id,),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET state = 'finishing', author_id = 0,
                        pr_title = '', pr_url = '', category_display = '',
                        routing_mode = 'none', direct_target_id = NULL,
                        current_target_id = NULL, assignee_id = NULL,
                        ping_count = 0, protection_until = NULL,
                        next_action = NULL, next_action_at = NULL,
                        pending_target_id = NULL,
                        pending_presence_tier = NULL,
                        pending_ping_automatic = NULL,
                        pending_ping_reserved_at = NULL,
                        pending_response_deadline = NULL,
                        updated_at = ?, transition_version = transition_version + 1
                    WHERE ticket_id = ? AND author_id = ?
                    """,
                    (_serialize_datetime(updated_at), ticket_id, author_id),
                )
                updated = connection.execute(
                    "SELECT * FROM tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                cleanup = _decode_ticket(connection, updated)
                connection.commit()
                return cleanup
            except Exception:
                connection.rollback()
                raise

    def _begin_finishing_sync(
        self,
        ticket_id: int,
        updated_at: datetime,
        message_absent: bool,
        thread_absent: bool,
    ) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET state = 'finishing', next_action = NULL, next_action_at = NULL,
                message_id = CASE WHEN ? THEN NULL ELSE message_id END,
                thread_id = CASE WHEN ? THEN NULL ELSE thread_id END,
                projection_sync_at = ?, updated_at = ?,
                transition_version = transition_version + 1
            WHERE ticket_id = ?
                AND state IN ('creating', 'open', 'claimed', 'finishing')
            """,
            (
                int(message_absent),
                int(thread_absent),
                _serialize_datetime(updated_at),
                _serialize_datetime(updated_at),
                ticket_id,
            ),
        )
        return changed > 0

    def _delete_ticket_sync(self, ticket_id: int) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM tickets WHERE ticket_id = ?",
                (ticket_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

    def _update_ticket_state(self, statement: str, parameters: tuple[object, ...]) -> int:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(statement, parameters)
                connection.commit()
                return cursor.rowcount
            except Exception:
                connection.rollback()
                raise
