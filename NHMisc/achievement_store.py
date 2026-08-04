from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STARGATE_COMPLETED_KEY = "stargate_completed"


@dataclass(frozen=True, slots=True)
class AchievementAward:
    award_id: int
    guild_id: int
    user_id: int
    achievement_key: str
    ordinal: int | None
    awarded_at: str
    source_channel_id: int | None
    source_message_id: int | None


@dataclass(frozen=True, slots=True)
class AwardResult:
    created: bool
    award: AchievementAward


@dataclass(frozen=True, slots=True)
class StargateProof:
    ordinal: int
    source_channel_id: int
    source_message_id: int


@dataclass(frozen=True, slots=True)
class AchievementProfile:
    stargate_count: int
    stargate_proofs: tuple[StargateProof, ...]
    boolean_keys: tuple[str, ...]


class AchievementStore:
    """Durable achievement history shared by every achievement workflow."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def grant_boolean(
        self,
        guild_id: int,
        user_id: int,
        achievement_key: str,
        *,
        source_channel_id: int | None = None,
        source_message_id: int | None = None,
    ) -> AwardResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._grant_boolean_sync,
                guild_id,
                user_id,
                achievement_key,
                source_channel_id,
                source_message_id,
            )

    async def grant_stargate(
        self,
        guild_id: int,
        user_id: int,
        *,
        source_channel_id: int | None = None,
        source_message_id: int | None = None,
    ) -> AwardResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._grant_stargate_sync,
                guild_id,
                user_id,
                source_channel_id,
                source_message_id,
            )

    async def import_gate_progress(
        self, guild_id: int, user_id: int, completed_count: int
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._import_gate_progress_sync,
                guild_id,
                user_id,
                completed_count,
            )

    async def get_profile(
        self, guild_id: int, user_id: int
    ) -> AchievementProfile:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_profile_sync,
                guild_id,
                user_id,
            )

    async def shared_boolean_keys(
        self, guild_id: int, user_ids: tuple[int, ...]
    ) -> tuple[str, ...]:
        if not user_ids:
            return ()
        async with self._lock:
            return await asyncio.to_thread(
                self._shared_boolean_keys_sync,
                guild_id,
                user_ids,
            )

    async def revoke_booleans(
        self,
        guild_id: int,
        user_ids: tuple[int, ...],
        achievement_keys: tuple[str, ...],
    ) -> int:
        if not user_ids or not achievement_keys:
            return 0
        async with self._lock:
            return await asyncio.to_thread(
                self._revoke_booleans_sync,
                guild_id,
                user_ids,
                achievement_keys,
            )

    async def is_bootstrapped(self, guild_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._is_bootstrapped_sync, guild_id)

    async def mark_bootstrapped(self, guild_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_bootstrapped_sync, guild_id)

    async def bootstrap_guild(
        self,
        guild_id: int,
        *,
        gate_tiers: Mapping[int, int],
        boolean_users: Mapping[str, tuple[int, ...]],
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._bootstrap_guild_sync,
                guild_id,
                dict(gate_tiers),
                dict(boolean_users),
            )

    async def get_gate_projection(self, guild_id: int, user_id: int) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_gate_projection_sync,
                guild_id,
                user_id,
            )

    async def list_gate_projections(self, guild_id: int) -> dict[int, int]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_gate_projections_sync,
                guild_id,
            )

    async def active_users_for_boolean(
        self, guild_id: int, achievement_key: str
    ) -> tuple[int, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._active_users_for_boolean_sync,
                guild_id,
                achievement_key,
            )

    async def delete_user_everywhere(self, user_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_user_everywhere_sync, user_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS achievement_awards (
                    award_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    achievement_key TEXT NOT NULL,
                    ordinal INTEGER,
                    awarded_at TEXT NOT NULL,
                    revoked_at TEXT,
                    source_channel_id INTEGER,
                    source_message_id INTEGER,
                    gate_operation_id INTEGER,
                    state TEXT NOT NULL DEFAULT 'active'
                        CHECK (state IN ('pending', 'active', 'revoked'))
                );

                CREATE UNIQUE INDEX IF NOT EXISTS achievement_boolean_active
                ON achievement_awards (guild_id, user_id, achievement_key)
                WHERE ordinal IS NULL AND state IN ('pending', 'active');

                CREATE UNIQUE INDEX IF NOT EXISTS achievement_ordinal_unique
                ON achievement_awards (
                    guild_id, user_id, achievement_key, ordinal
                )
                WHERE ordinal IS NOT NULL;

                CREATE INDEX IF NOT EXISTS achievement_profile_lookup
                ON achievement_awards (guild_id, user_id, state);

                CREATE TABLE IF NOT EXISTS achievement_bootstrap (
                    guild_id INTEGER PRIMARY KEY,
                    completed_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def _grant_boolean_sync(
        self,
        guild_id: int,
        user_id: int,
        achievement_key: str,
        source_channel_id: int | None,
        source_message_id: int | None,
    ) -> AwardResult:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM achievement_awards
                WHERE guild_id = ? AND user_id = ? AND achievement_key = ?
                    AND ordinal IS NULL AND state IN ('pending', 'active')
                """,
                (guild_id, user_id, achievement_key),
            ).fetchone()
            created = row is None
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO achievement_awards (
                        guild_id, user_id, achievement_key, awarded_at,
                        source_channel_id, source_message_id, state
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        guild_id,
                        user_id,
                        achievement_key,
                        now,
                        source_channel_id,
                        source_message_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM achievement_awards WHERE award_id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            connection.commit()
        return AwardResult(created, self._award_from_row(row))

    def _grant_stargate_sync(
        self,
        guild_id: int,
        user_id: int,
        source_channel_id: int | None,
        source_message_id: int | None,
    ) -> AwardResult:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            next_ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal), 0) + 1
                    FROM achievement_awards
                    WHERE guild_id = ? AND user_id = ?
                        AND achievement_key = ?
                        AND state IN ('pending', 'active')
                    """,
                    (guild_id, user_id, STARGATE_COMPLETED_KEY),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, ordinal, awarded_at,
                    source_channel_id, source_message_id, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    guild_id,
                    user_id,
                    STARGATE_COMPLETED_KEY,
                    next_ordinal,
                    now,
                    source_channel_id,
                    source_message_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM achievement_awards WHERE award_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            connection.commit()
        return AwardResult(True, self._award_from_row(row))

    def _import_gate_progress_sync(
        self, guild_id: int, user_id: int, completed_count: int
    ) -> None:
        if completed_count < 0:
            raise ValueError("completed_count cannot be negative")
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for ordinal in range(1, completed_count + 1):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO achievement_awards (
                        guild_id, user_id, achievement_key, ordinal,
                        awarded_at, state
                    ) VALUES (?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        guild_id,
                        user_id,
                        STARGATE_COMPLETED_KEY,
                        ordinal,
                        now,
                    ),
                )
            connection.commit()

    def _get_profile_sync(
        self, guild_id: int, user_id: int
    ) -> AchievementProfile:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT achievement_key, ordinal, source_channel_id,
                    source_message_id
                FROM achievement_awards
                WHERE guild_id = ? AND user_id = ? AND state = 'active'
                ORDER BY award_id
                """,
                (guild_id, user_id),
            ).fetchall()
        gates = [
            row
            for row in rows
            if row["achievement_key"] == STARGATE_COMPLETED_KEY
            and row["ordinal"] is not None
        ]
        proofs = tuple(
            StargateProof(
                int(row["ordinal"]),
                int(row["source_channel_id"]),
                int(row["source_message_id"]),
            )
            for row in gates
            if row["source_channel_id"] is not None
            and row["source_message_id"] is not None
        )
        boolean_keys = tuple(
            str(row["achievement_key"])
            for row in rows
            if row["ordinal"] is None
        )
        return AchievementProfile(len(gates), proofs, boolean_keys)

    def _shared_boolean_keys_sync(
        self, guild_id: int, user_ids: tuple[int, ...]
    ) -> tuple[str, ...]:
        placeholders = ", ".join("?" for _ in user_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT achievement_key
                FROM achievement_awards
                WHERE guild_id = ? AND user_id IN ({placeholders})
                    AND ordinal IS NULL AND state = 'active'
                GROUP BY achievement_key
                HAVING COUNT(DISTINCT user_id) = ?
                ORDER BY MIN(award_id)
                """,  # noqa: S608 - placeholders are generated, not user input
                (guild_id, *user_ids, len(set(user_ids))),
            ).fetchall()
        return tuple(str(row["achievement_key"]) for row in rows)

    def _revoke_booleans_sync(
        self,
        guild_id: int,
        user_ids: tuple[int, ...],
        achievement_keys: tuple[str, ...],
    ) -> int:
        user_placeholders = ", ".join("?" for _ in user_ids)
        key_placeholders = ", ".join("?" for _ in achievement_keys)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE achievement_awards
                SET state = 'revoked', revoked_at = ?
                WHERE guild_id = ? AND user_id IN ({user_placeholders})
                    AND achievement_key IN ({key_placeholders})
                    AND ordinal IS NULL AND state = 'active'
                """,  # noqa: S608 - placeholders are generated, not user input
                (now, guild_id, *user_ids, *achievement_keys),
            )
            connection.commit()
        return int(cursor.rowcount)

    def _is_bootstrapped_sync(self, guild_id: int) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM achievement_bootstrap WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return row is not None

    def _mark_bootstrapped_sync(self, guild_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO achievement_bootstrap (guild_id, completed_at)
                VALUES (?, ?)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                (guild_id, now),
            )
            connection.commit()

    def _bootstrap_guild_sync(
        self,
        guild_id: int,
        gate_tiers: dict[int, int],
        boolean_users: dict[str, tuple[int, ...]],
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM achievement_bootstrap WHERE guild_id = ?",
                (guild_id,),
            ).fetchone() is not None:
                connection.commit()
                return False
            for user_id, completed_count in gate_tiers.items():
                if completed_count < 0:
                    connection.rollback()
                    raise ValueError("Gate tiers cannot be negative")
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO achievement_awards (
                        guild_id, user_id, achievement_key, ordinal,
                        awarded_at, state
                    ) VALUES (?, ?, 'stargate_completed', ?, ?, 'active')
                    """,
                    (
                        (guild_id, user_id, ordinal, now)
                        for ordinal in range(1, completed_count + 1)
                    ),
                )
            for achievement_key, user_ids in boolean_users.items():
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO achievement_awards (
                        guild_id, user_id, achievement_key, awarded_at, state
                    ) VALUES (?, ?, ?, ?, 'active')
                    """,
                    (
                        (guild_id, user_id, achievement_key, now)
                        for user_id in user_ids
                    ),
                )
            connection.execute(
                """
                INSERT INTO achievement_bootstrap (guild_id, completed_at)
                VALUES (?, ?)
                """,
                (guild_id, now),
            )
            connection.commit()
        return True

    def _get_gate_projection_sync(self, guild_id: int, user_id: int) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM achievement_awards
                WHERE guild_id = ? AND user_id = ?
                    AND achievement_key = 'stargate_completed'
                    AND ordinal IS NOT NULL
                    AND state IN ('pending', 'active')
                """,
                (guild_id, user_id),
            ).fetchone()
        return int(row[0])

    def _list_gate_projections_sync(self, guild_id: int) -> dict[int, int]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id, COUNT(*) AS completed_count
                FROM achievement_awards
                WHERE guild_id = ?
                    AND achievement_key = 'stargate_completed'
                    AND ordinal IS NOT NULL
                    AND state IN ('pending', 'active')
                GROUP BY user_id
                """,
                (guild_id,),
            ).fetchall()
        return {
            int(row["user_id"]): int(row["completed_count"])
            for row in rows
        }

    def _active_users_for_boolean_sync(
        self, guild_id: int, achievement_key: str
    ) -> tuple[int, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id
                FROM achievement_awards
                WHERE guild_id = ? AND achievement_key = ?
                    AND ordinal IS NULL AND state = 'active'
                ORDER BY user_id
                """,
                (guild_id, achievement_key),
            ).fetchall()
        return tuple(int(row["user_id"]) for row in rows)

    def _delete_user_everywhere_sync(self, user_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM achievement_awards WHERE user_id = ?",
                (user_id,),
            )
            connection.commit()

    @staticmethod
    def _award_from_row(row: sqlite3.Row) -> AchievementAward:
        return AchievementAward(
            award_id=int(row["award_id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            achievement_key=str(row["achievement_key"]),
            ordinal=int(row["ordinal"]) if row["ordinal"] is not None else None,
            awarded_at=str(row["awarded_at"]),
            source_channel_id=(
                int(row["source_channel_id"])
                if row["source_channel_id"] is not None
                else None
            ),
            source_message_id=(
                int(row["source_message_id"])
                if row["source_message_id"] is not None
                else None
            ),
        )
