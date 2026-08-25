from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from NHCogs.storage import ConnectionFactory, apply_migrations, connect

from .models import (
    Category,
    CategoryAlreadyExists,
    CategoryLimitReached,
    ExclusionReason,
    InvalidCategoryName,
    NewTicket,
    NextAction,
    PresenceTier,
    Profile,
    RoutingMode,
    Ticket,
    TicketExclusion,
    TicketPing,
    TicketState,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


SCHEMA_VERSION = 1
MAX_CATEGORIES = 25
MAX_CATEGORY_NAME_LENGTH = 100


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC).isoformat()


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
        created_at=_deserialize_datetime(str(row["created_at"])),
        updated_at=_deserialize_datetime(str(row["updated_at"])),
        transition_version=int(row["transition_version"]),
        category_ids=category_ids,
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            transition_version INTEGER NOT NULL DEFAULT 0
                CHECK (transition_version >= 0),
            CHECK (
                (next_action IS NULL AND next_action_at IS NULL)
                OR (next_action IS NOT NULL AND next_action_at IS NOT NULL)
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
            presence_tier TEXT NOT NULL CHECK (
                presence_tier IN ('online', 'idle', 'do_not_disturb', 'offline')
            ),
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

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_ticket_sync, ticket_id)

    async def list_active_tickets(self) -> tuple[Ticket, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_active_tickets_sync)

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
        presence_tier: PresenceTier,
        sent_at: datetime,
        response_deadline: datetime,
        maximum_pings: int,
    ) -> TicketPing | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_ping_sync,
                ticket_id,
                (target_user_id, presence_tier, sent_at, response_deadline),
                maximum_pings,
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

    async def begin_finishing(self, ticket_id: int, updated_at: datetime) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._begin_finishing_sync, ticket_id, updated_at)

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
                        guild_id, channel_id, author_id, pr_title, pr_url,
                        category_display, routing_mode, state, direct_target_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?, ?)
                    """,
                    (
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
                updated_at = ?, transition_version = transition_version + 1
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

    def _get_ticket_sync(self, ticket_id: int) -> Ticket | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?",
                (ticket_id,),
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
                updated_at = ?, transition_version = transition_version + 1
            WHERE ticket_id = ? AND state = 'open'
            """,
            (
                assignee_id,
                _serialize_datetime(protection_until),
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
                    "SELECT state, current_target_id FROM tickets WHERE ticket_id = ?",
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
                if row["current_target_id"] == user_id:
                    connection.execute(
                        """
                        UPDATE tickets
                        SET current_target_id = NULL, protection_until = ?,
                            next_action = ?, next_action_at = ?, updated_at = ?,
                            transition_version = transition_version + 1
                        WHERE ticket_id = ?
                        """,
                        (
                            _serialize_optional_datetime(protection_until),
                            next_action.value if next_action is not None else None,
                            _serialize_optional_datetime(next_action_at),
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
                        updated_at = ?, transition_version = transition_version + 1
                    WHERE ticket_id = ? AND state = 'claimed'
                    """,
                    (
                        _serialize_datetime(protection_until),
                        next_action.value if next_action is not None else None,
                        _serialize_optional_datetime(next_action_at),
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
        target: tuple[int, PresenceTier, datetime, datetime],
        maximum_pings: int,
    ) -> TicketPing | None:
        target_user_id, presence_tier, sent_at, response_deadline = target
        if maximum_pings <= 0:
            return None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state, ping_count FROM tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != TicketState.OPEN.value
                    or int(row["ping_count"]) >= maximum_pings
                    or self._target_was_used(connection, ticket_id, target_user_id)
                ):
                    connection.rollback()
                    return None
                sequence_number = int(row["ping_count"]) + 1
                connection.execute(
                    """
                    INSERT INTO ticket_pings (
                        ticket_id, sequence_number, target_user_id, presence_tier,
                        sent_at, response_deadline
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        sequence_number,
                        target_user_id,
                        presence_tier.value,
                        _serialize_datetime(sent_at),
                        _serialize_datetime(response_deadline),
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET ping_count = ?, current_target_id = ?,
                        next_action = 'target_timeout', next_action_at = ?,
                        updated_at = ?, transition_version = transition_version + 1
                    WHERE ticket_id = ? AND state = 'open'
                    """,
                    (
                        sequence_number,
                        target_user_id,
                        _serialize_datetime(response_deadline),
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
            sent_at=sent_at.astimezone(UTC),
            response_deadline=response_deadline.astimezone(UTC),
        )

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
                presence_tier=PresenceTier(str(row["presence_tier"])),
                sent_at=_deserialize_datetime(str(row["sent_at"])),
                response_deadline=_deserialize_datetime(str(row["response_deadline"])),
            )
            for row in rows
        )

    def _nearest_deadline_sync(self) -> datetime | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT MIN(next_action_at) AS deadline
                FROM tickets
                WHERE state = 'open' AND next_action_at IS NOT NULL
                """
            ).fetchone()
        return _deserialize_optional_datetime(row["deadline"])

    def _due_ticket_ids_sync(self, now: datetime) -> tuple[int, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT ticket_id FROM tickets
                WHERE state = 'open' AND next_action_at <= ?
                ORDER BY next_action_at, ticket_id
                """,
                (_serialize_datetime(now),),
            ).fetchall()
        return tuple(int(row["ticket_id"]) for row in rows)

    def _begin_finishing_sync(self, ticket_id: int, updated_at: datetime) -> bool:
        changed = self._update_ticket_state(
            """
            UPDATE tickets
            SET state = 'finishing', next_action = NULL, next_action_at = NULL,
                updated_at = ?, transition_version = transition_version + 1
            WHERE ticket_id = ? AND state IN ('open', 'claimed')
            """,
            (_serialize_datetime(updated_at), ticket_id),
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
