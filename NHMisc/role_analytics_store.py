from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence


class SyncStatus(str, Enum):
    DISABLED = "DISABLED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    SYNCING = "SYNCING"
    READY = "READY"
    RETRYING = "RETRYING"
    FAILED = "FAILED"


class AnalyticsUnavailableError(RuntimeError):
    """Raised when a guild has no ready active analytics generation."""


@dataclass(frozen=True, slots=True)
class RoleAnalyticsState:
    guild_id: int
    enabled: bool
    status: SyncStatus
    active_generation: int | None
    last_completed_at: str | None
    source_member_count: int | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class MemberSnapshot:
    user_id: int
    is_bot: bool
    role_ids: tuple[int, ...]


class RoleAnalyticsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def get_state(self, guild_id: int) -> RoleAnalyticsState:
        async with self._lock:
            return await asyncio.to_thread(self._get_state_sync, guild_id)

    async def set_status(
        self,
        guild_id: int,
        status: SyncStatus,
        error_code: str | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._set_status_sync,
                guild_id,
                status,
                error_code,
            )

    async def mark_needs_reconciliation_if_enabled(self, guild_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._mark_needs_reconciliation_if_enabled_sync,
                guild_id,
            )

    async def next_generation(self, guild_id: int) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._next_generation_sync, guild_id)

    async def write_generation(
        self,
        guild_id: int,
        generation: int,
        members: Sequence[MemberSnapshot],
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._write_generation_sync,
                guild_id,
                generation,
                tuple(members),
            )

    async def activate_generation(
        self,
        guild_id: int,
        generation: int,
        source_member_count: int,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._activate_generation_sync,
                guild_id,
                generation,
                source_member_count,
            )

    async def count_matching(
        self,
        guild_id: int,
        predicate_sql: str,
        parameters: Sequence[int],
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._count_matching_sync,
                guild_id,
                predicate_sql,
                tuple(parameters),
            )

    async def matching_user_ids(
        self,
        guild_id: int,
        predicate_sql: str,
        parameters: Sequence[int],
    ) -> tuple[int, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._matching_user_ids_sync,
                guild_id,
                predicate_sql,
                tuple(parameters),
            )

    async def replace_member(
        self,
        guild_id: int,
        member: MemberSnapshot,
        generation: int | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._replace_member_sync,
                guild_id,
                member,
                generation,
            )

    async def remove_member(
        self,
        guild_id: int,
        user_id: int,
        generation: int | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._remove_member_sync,
                guild_id,
                user_id,
                generation,
            )

    async def remove_role(
        self,
        guild_id: int,
        role_id: int,
        generation: int | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._remove_role_sync,
                guild_id,
                role_id,
                generation,
            )

    async def clear_guild(self, guild_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._clear_guild_sync, guild_id)

    async def discard_generation(self, guild_id: int, generation: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._discard_generation_sync,
                guild_id,
                generation,
            )

    async def delete_inactive_generations(self, guild_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._delete_inactive_generations_sync,
                guild_id,
            )

    async def delete_user_everywhere(self, user_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_user_everywhere_sync, user_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS role_analytics_state (
                    guild_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    active_generation INTEGER,
                    last_completed_at TEXT,
                    source_member_count INTEGER,
                    last_error_code TEXT
                );

                CREATE TABLE IF NOT EXISTS role_analytics_members (
                    guild_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    is_bot INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, generation, user_id)
                );

                CREATE TABLE IF NOT EXISTS role_analytics_memberships (
                    guild_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, generation, user_id, role_id),
                    FOREIGN KEY (guild_id, generation, user_id)
                        REFERENCES role_analytics_members
                            (guild_id, generation, user_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_role_analytics_memberships_role
                    ON role_analytics_memberships
                        (guild_id, generation, role_id, user_id);
                """
            )

    def _get_state_sync(self, guild_id: int) -> RoleAnalyticsState:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT guild_id, enabled, status, active_generation,
                       last_completed_at, source_member_count, last_error_code
                FROM role_analytics_state
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        if row is None:
            return RoleAnalyticsState(
                guild_id=guild_id,
                enabled=False,
                status=SyncStatus.DISABLED,
                active_generation=None,
                last_completed_at=None,
                source_member_count=None,
                last_error_code=None,
            )
        return RoleAnalyticsState(
            guild_id=int(row["guild_id"]),
            enabled=bool(row["enabled"]),
            status=SyncStatus(row["status"]),
            active_generation=row["active_generation"],
            last_completed_at=row["last_completed_at"],
            source_member_count=row["source_member_count"],
            last_error_code=row["last_error_code"],
        )

    def _set_status_sync(
        self,
        guild_id: int,
        status: SyncStatus,
        error_code: str | None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO role_analytics_state (
                    guild_id, enabled, status, last_error_code
                ) VALUES (?, 0, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    status = excluded.status,
                    last_error_code = excluded.last_error_code
                """,
                (guild_id, status.value, error_code),
            )

    def _next_generation_sync(self, guild_id: int) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT MAX(generation) AS maximum_generation
                FROM role_analytics_members
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            state = connection.execute(
                """
                SELECT active_generation
                FROM role_analytics_state
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            candidates = [0]
            if row is not None and row["maximum_generation"] is not None:
                candidates.append(int(row["maximum_generation"]))
            if state is not None and state["active_generation"] is not None:
                candidates.append(int(state["active_generation"]))
            generation = max(candidates) + 1
            connection.execute(
                """
                INSERT INTO role_analytics_state (guild_id, enabled, status)
                VALUES (?, 0, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    status = excluded.status,
                    last_error_code = NULL
                """,
                (guild_id, SyncStatus.SYNCING.value),
            )
            return generation

    def _mark_needs_reconciliation_if_enabled_sync(self, guild_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE role_analytics_state
                SET status = ?, last_error_code = NULL
                WHERE guild_id = ? AND enabled = 1
                """,
                (SyncStatus.NEEDS_RECONCILIATION.value, guild_id),
            )
            return cursor.rowcount > 0

    def _write_generation_sync(
        self,
        guild_id: int,
        generation: int,
        members: tuple[MemberSnapshot, ...],
    ) -> None:
        member_rows = [
            (guild_id, generation, member.user_id, int(member.is_bot))
            for member in members
        ]
        membership_rows = [
            (guild_id, generation, member.user_id, role_id)
            for member in members
            for role_id in sorted(set(member.role_ids))
        ]
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM role_analytics_members
                WHERE guild_id = ? AND generation = ?
                """,
                (guild_id, generation),
            )
            for start in range(0, len(member_rows), 5_000):
                connection.executemany(
                    """
                    INSERT INTO role_analytics_members
                        (guild_id, generation, user_id, is_bot)
                    VALUES (?, ?, ?, ?)
                    """,
                    member_rows[start : start + 5_000],
                )
            for start in range(0, len(membership_rows), 20_000):
                connection.executemany(
                    """
                    INSERT INTO role_analytics_memberships
                        (guild_id, generation, user_id, role_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    membership_rows[start : start + 20_000],
                )

    def _activate_generation_sync(
        self,
        guild_id: int,
        generation: int,
        source_member_count: int,
    ) -> None:
        completed_at = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            exists = connection.execute(
                """
                SELECT 1
                FROM role_analytics_members
                WHERE guild_id = ? AND generation = ?
                LIMIT 1
                """,
                (guild_id, generation),
            ).fetchone()
            if exists is None and source_member_count:
                raise ValueError("Cannot activate a missing generation")
            connection.execute(
                """
                INSERT INTO role_analytics_state (
                    guild_id, enabled, status, active_generation,
                    last_completed_at, source_member_count, last_error_code
                ) VALUES (?, 1, ?, ?, ?, ?, NULL)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled = 1,
                    status = excluded.status,
                    active_generation = excluded.active_generation,
                    last_completed_at = excluded.last_completed_at,
                    source_member_count = excluded.source_member_count,
                    last_error_code = NULL
                """,
                (
                    guild_id,
                    SyncStatus.READY.value,
                    generation,
                    completed_at,
                    source_member_count,
                ),
            )

    @staticmethod
    def _ready_generation(
        connection: sqlite3.Connection,
        guild_id: int,
    ) -> int:
        row = connection.execute(
            """
            SELECT enabled, status, active_generation
            FROM role_analytics_state
            WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        if (
            row is None
            or not row["enabled"]
            or row["status"] != SyncStatus.READY.value
            or row["active_generation"] is None
        ):
            raise AnalyticsUnavailableError("Role analytics are not ready")
        return int(row["active_generation"])

    def _count_matching_sync(
        self,
        guild_id: int,
        predicate_sql: str,
        parameters: tuple[int, ...],
    ) -> int:
        with self._connection() as connection:
            generation = self._ready_generation(connection, guild_id)
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS matching_count
                FROM role_analytics_members AS member
                WHERE member.guild_id = ?
                  AND member.generation = ?
                  AND member.is_bot = 0
                  AND ({predicate_sql})
                """,
                (guild_id, generation, *parameters),
            ).fetchone()
            return int(row["matching_count"])

    def _matching_user_ids_sync(
        self,
        guild_id: int,
        predicate_sql: str,
        parameters: tuple[int, ...],
    ) -> tuple[int, ...]:
        with self._connection() as connection:
            generation = self._ready_generation(connection, guild_id)
            rows = connection.execute(
                f"""
                SELECT member.user_id
                FROM role_analytics_members AS member
                WHERE member.guild_id = ?
                  AND member.generation = ?
                  AND member.is_bot = 0
                  AND ({predicate_sql})
                ORDER BY member.user_id
                """,
                (guild_id, generation, *parameters),
            ).fetchall()
            return tuple(int(row["user_id"]) for row in rows)

    def _replace_member_sync(
        self,
        guild_id: int,
        member: MemberSnapshot,
        generation: int | None,
    ) -> None:
        with self._connection() as connection:
            target_generation = (
                self._ready_generation(connection, guild_id)
                if generation is None
                else generation
            )
            connection.execute(
                """
                INSERT INTO role_analytics_members
                    (guild_id, generation, user_id, is_bot)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, generation, user_id) DO UPDATE SET
                    is_bot = excluded.is_bot
                """,
                (
                    guild_id,
                    target_generation,
                    member.user_id,
                    int(member.is_bot),
                ),
            )
            connection.execute(
                """
                DELETE FROM role_analytics_memberships
                WHERE guild_id = ? AND generation = ? AND user_id = ?
                """,
                (guild_id, target_generation, member.user_id),
            )
            connection.executemany(
                """
                INSERT INTO role_analytics_memberships
                    (guild_id, generation, user_id, role_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (guild_id, target_generation, member.user_id, role_id)
                    for role_id in sorted(set(member.role_ids))
                ),
            )

    def _remove_member_sync(
        self,
        guild_id: int,
        user_id: int,
        generation: int | None,
    ) -> None:
        with self._connection() as connection:
            target_generation = (
                self._ready_generation(connection, guild_id)
                if generation is None
                else generation
            )
            connection.execute(
                """
                DELETE FROM role_analytics_members
                WHERE guild_id = ? AND generation = ? AND user_id = ?
                """,
                (guild_id, target_generation, user_id),
            )

    def _remove_role_sync(
        self,
        guild_id: int,
        role_id: int,
        generation: int | None,
    ) -> None:
        with self._connection() as connection:
            target_generation = (
                self._ready_generation(connection, guild_id)
                if generation is None
                else generation
            )
            connection.execute(
                """
                DELETE FROM role_analytics_memberships
                WHERE guild_id = ? AND generation = ? AND role_id = ?
                """,
                (guild_id, target_generation, role_id),
            )

    def _clear_guild_sync(self, guild_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM role_analytics_members WHERE guild_id = ?",
                (guild_id,),
            )
            connection.execute(
                "DELETE FROM role_analytics_state WHERE guild_id = ?",
                (guild_id,),
            )

    def _discard_generation_sync(self, guild_id: int, generation: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM role_analytics_members
                WHERE guild_id = ? AND generation = ?
                """,
                (guild_id, generation),
            )

    def _delete_inactive_generations_sync(self, guild_id: int) -> None:
        with self._connection() as connection:
            state = connection.execute(
                """
                SELECT active_generation
                FROM role_analytics_state
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            if state is None or state["active_generation"] is None:
                return
            connection.execute(
                """
                DELETE FROM role_analytics_members
                WHERE guild_id = ? AND generation <> ?
                """,
                (guild_id, int(state["active_generation"])),
            )

    def _delete_user_everywhere_sync(self, user_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM role_analytics_members WHERE user_id = ?",
                (user_id,),
            )
