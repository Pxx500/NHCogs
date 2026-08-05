from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

STARGATE_COMPLETED_KEY = "stargate_completed"
SYSTEM_ACHIEVEMENT_KEYS = frozenset((STARGATE_COMPLETED_KEY, "solo_gater"))


class AchievementKind(str, Enum):
    BOOLEAN = "boolean"
    REPEATABLE = "repeatable"


@dataclass(frozen=True, slots=True)
class AchievementDefinition:
    key: str
    display_name: str
    kind: AchievementKind
    role_id: int | None = None
    grantable: bool = True
    revocable: bool = True
    display_order: int = 0


@dataclass(frozen=True, slots=True)
class AchievementDeletionPreview:
    definition: AchievementDefinition
    award_count: int


@dataclass(frozen=True, slots=True)
class RoleBindingResult:
    definition: AchievementDefinition
    imported_count: int


@dataclass(frozen=True, slots=True)
class DiscordSnapshotResult:
    changed_users: int
    gate_users_changed: int
    boolean_grants: int
    boolean_revocations: int


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

    async def backup_database(self) -> bytes:
        async with self._lock:
            return await asyncio.to_thread(self._backup_database_sync)

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

    async def get_latest_stargate(
        self, guild_id: int, user_id: int
    ) -> AchievementAward | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_latest_stargate_sync,
                guild_id,
                user_id,
            )

    async def delete_latest_stargate(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_award_id: int,
    ) -> AchievementAward | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_latest_stargate_sync,
                guild_id,
                user_id,
                expected_award_id,
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
        boolean_definitions: tuple[AchievementDefinition, ...],
        boolean_users: Mapping[str, tuple[int, ...]],
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._bootstrap_guild_sync,
                guild_id,
                dict(gate_tiers),
                boolean_definitions,
                dict(boolean_users),
            )

    async def list_definitions(
        self, guild_id: int
    ) -> tuple[AchievementDefinition, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_definitions_sync, guild_id)

    async def unbind_role(
        self, guild_id: int, role_id: int
    ) -> AchievementDefinition:
        async with self._lock:
            return await asyncio.to_thread(
                self._unbind_role_sync,
                guild_id,
                role_id,
            )

    async def create_boolean_definition(
        self, guild_id: int, display_name: str
    ) -> AchievementDefinition:
        async with self._lock:
            return await asyncio.to_thread(
                self._create_boolean_definition_sync,
                guild_id,
                display_name,
            )

    async def rename_definition(
        self,
        guild_id: int,
        achievement_key: str,
        display_name: str,
    ) -> AchievementDefinition:
        async with self._lock:
            return await asyncio.to_thread(
                self._rename_definition_sync,
                guild_id,
                achievement_key,
                display_name,
            )

    async def prepare_definition_deletion(
        self,
        guild_id: int,
        achievement_key: str,
    ) -> AchievementDeletionPreview:
        async with self._lock:
            return await asyncio.to_thread(
                self._prepare_definition_deletion_sync,
                guild_id,
                achievement_key,
            )

    async def delete_definition(
        self,
        guild_id: int,
        achievement_key: str,
        *,
        expected_award_count: int,
    ) -> AchievementDeletionPreview:
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_definition_sync,
                guild_id,
                achievement_key,
                expected_award_count,
            )

    async def bind_role(
        self,
        guild_id: int,
        achievement_key: str,
        *,
        role_id: int,
        user_ids: tuple[int, ...],
    ) -> RoleBindingResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._bind_role_sync,
                guild_id,
                achievement_key,
                role_id,
                user_ids,
            )

    async def replace_role(
        self,
        guild_id: int,
        *,
        old_role_id: int,
        new_role_id: int,
        user_ids: tuple[int, ...],
    ) -> RoleBindingResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._replace_role_sync,
                guild_id,
                old_role_id,
                new_role_id,
                user_ids,
            )

    async def apply_discord_snapshot(
        self,
        guild_id: int,
        *,
        gate_tiers: Mapping[int, int],
        boolean_users: Mapping[str, tuple[int, ...]],
    ) -> DiscordSnapshotResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._apply_discord_snapshot_sync,
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

    async def projected_users_for_boolean(
        self, guild_id: int, achievement_key: str
    ) -> tuple[int, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._projected_users_for_boolean_sync,
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

    def _backup_database_sync(self) -> bytes:
        with TemporaryDirectory() as directory:
            backup_path = Path(directory) / "achievements.sqlite"
            with self._connection() as source, closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)
            return backup_path.read_bytes()

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

                CREATE TABLE IF NOT EXISTS achievement_definitions (
                    guild_id INTEGER NOT NULL,
                    achievement_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('boolean', 'repeatable')),
                    role_id INTEGER,
                    grantable INTEGER NOT NULL DEFAULT 1,
                    revocable INTEGER NOT NULL DEFAULT 1,
                    display_order INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, achievement_key)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS achievement_definition_role
                ON achievement_definitions (guild_id, role_id)
                WHERE role_id IS NOT NULL;
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
            definition = connection.execute(
                """
                SELECT kind, grantable
                FROM achievement_definitions
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (guild_id, achievement_key),
            ).fetchone()
            if definition is None:
                connection.rollback()
                raise LookupError("Achievement does not exist")
            if definition["kind"] != AchievementKind.BOOLEAN.value:
                connection.rollback()
                raise ValueError("Only boolean achievements can be granted")
            if not bool(definition["grantable"]):
                connection.rollback()
                raise ValueError("Achievement cannot be granted directly")
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

    def _get_latest_stargate_sync(
        self, guild_id: int, user_id: int
    ) -> AchievementAward | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM achievement_awards
                WHERE guild_id = ? AND user_id = ?
                    AND achievement_key = ?
                    AND ordinal IS NOT NULL
                    AND state = 'active'
                ORDER BY ordinal DESC, award_id DESC
                LIMIT 1
                """,
                (guild_id, user_id, STARGATE_COMPLETED_KEY),
            ).fetchone()
        return self._award_from_row(row) if row is not None else None

    def _delete_latest_stargate_sync(
        self,
        guild_id: int,
        user_id: int,
        expected_award_id: int,
    ) -> AchievementAward | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending_increment = connection.execute(
                """
                SELECT 1
                FROM achievement_awards
                WHERE guild_id = ? AND user_id = ?
                    AND achievement_key = ?
                    AND ordinal IS NOT NULL
                    AND state = 'pending'
                LIMIT 1
                """,
                (guild_id, user_id, STARGATE_COMPLETED_KEY),
            ).fetchone()
            if pending_increment is not None:
                connection.rollback()
                return None
            row = connection.execute(
                """
                SELECT *
                FROM achievement_awards
                WHERE guild_id = ? AND user_id = ?
                    AND achievement_key = ?
                    AND ordinal IS NOT NULL
                    AND state = 'active'
                ORDER BY ordinal DESC, award_id DESC
                LIMIT 1
                """,
                (guild_id, user_id, STARGATE_COMPLETED_KEY),
            ).fetchone()
            if row is None or int(row["award_id"]) != expected_award_id:
                connection.rollback()
                return None
            cursor = connection.execute(
                "DELETE FROM achievement_awards WHERE award_id = ? AND state = 'active'",
                (expected_award_id,),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
        return self._award_from_row(row)

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
        boolean_definitions: tuple[AchievementDefinition, ...],
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
            connection.executemany(
                """
                INSERT INTO achievement_definitions (
                    guild_id, achievement_key, display_name, kind, role_id,
                    grantable, revocable, display_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        guild_id,
                        definition.key,
                        definition.display_name,
                        definition.kind.value,
                        definition.role_id,
                        int(definition.grantable),
                        int(definition.revocable),
                        definition.display_order,
                    )
                    for definition in boolean_definitions
                ),
            )
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

    def _list_definitions_sync(
        self, guild_id: int
    ) -> tuple[AchievementDefinition, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT achievement_key, display_name, kind, role_id,
                       grantable, revocable, display_order
                FROM achievement_definitions
                WHERE guild_id = ?
                ORDER BY display_order, achievement_key
                """,
                (guild_id,),
            ).fetchall()
        return tuple(self._definition_from_row(row) for row in rows)

    def _unbind_role_sync(
        self, guild_id: int, role_id: int
    ) -> AchievementDefinition:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT achievement_key, display_name, kind, role_id,
                       grantable, revocable, display_order
                FROM achievement_definitions
                WHERE guild_id = ? AND role_id = ?
                """,
                (guild_id, role_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LookupError("Role is not bound to an achievement")
            connection.execute(
                """
                UPDATE achievement_definitions
                SET role_id = NULL
                WHERE guild_id = ? AND role_id = ?
                """,
                (guild_id, role_id),
            )
            connection.commit()
        return AchievementDefinition(
            key=str(row["achievement_key"]),
            display_name=str(row["display_name"]),
            kind=AchievementKind(str(row["kind"])),
            role_id=None,
            grantable=bool(row["grantable"]),
            revocable=bool(row["revocable"]),
            display_order=int(row["display_order"]),
        )

    def _create_boolean_definition_sync(
        self, guild_id: int, display_name: str
    ) -> AchievementDefinition:
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("Achievement name cannot be empty")
        definition = AchievementDefinition(
            key=f"achievement_{uuid4().hex}",
            display_name=normalized_name,
            kind=AchievementKind.BOOLEAN,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(display_order), -1) + 1 AS next_order
                FROM achievement_definitions
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            definition = AchievementDefinition(
                key=definition.key,
                display_name=definition.display_name,
                kind=definition.kind,
                display_order=int(row["next_order"]),
            )
            connection.execute(
                """
                INSERT INTO achievement_definitions (
                    guild_id, achievement_key, display_name, kind, role_id,
                    grantable, revocable, display_order
                ) VALUES (?, ?, ?, ?, NULL, 1, 1, ?)
                """,
                (
                    guild_id,
                    definition.key,
                    definition.display_name,
                    definition.kind.value,
                    definition.display_order,
                ),
            )
            connection.commit()
        return definition

    def _rename_definition_sync(
        self,
        guild_id: int,
        achievement_key: str,
        display_name: str,
    ) -> AchievementDefinition:
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("Achievement name cannot be empty")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT achievement_key, display_name, kind, role_id,
                       grantable, revocable, display_order
                FROM achievement_definitions
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (guild_id, achievement_key),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LookupError("Achievement does not exist")
            connection.execute(
                """
                UPDATE achievement_definitions
                SET display_name = ?
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (normalized_name, guild_id, achievement_key),
            )
            connection.commit()
        return AchievementDefinition(
            key=str(row["achievement_key"]),
            display_name=normalized_name,
            kind=AchievementKind(str(row["kind"])),
            role_id=int(row["role_id"]) if row["role_id"] is not None else None,
            grantable=bool(row["grantable"]),
            revocable=bool(row["revocable"]),
            display_order=int(row["display_order"]),
        )

    @staticmethod
    def _deletion_preview(
        connection: sqlite3.Connection,
        guild_id: int,
        achievement_key: str,
    ) -> AchievementDeletionPreview:
        row = connection.execute(
            """
            SELECT achievement_key, display_name, kind, role_id,
                   grantable, revocable, display_order
            FROM achievement_definitions
            WHERE guild_id = ? AND achievement_key = ?
            """,
            (guild_id, achievement_key),
        ).fetchone()
        if row is None:
            raise LookupError("Achievement does not exist")
        if achievement_key in SYSTEM_ACHIEVEMENT_KEYS:
            raise ValueError("System achievements cannot be deleted")
        if row["role_id"] is not None:
            raise ValueError("Unbind the Discord role before deleting this achievement")
        award_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM achievement_awards
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (guild_id, achievement_key),
            ).fetchone()[0]
        )
        return AchievementDeletionPreview(
            definition=AchievementDefinition(
                key=str(row["achievement_key"]),
                display_name=str(row["display_name"]),
                kind=AchievementKind(str(row["kind"])),
                role_id=None,
                grantable=bool(row["grantable"]),
                revocable=bool(row["revocable"]),
                display_order=int(row["display_order"]),
            ),
            award_count=award_count,
        )

    def _prepare_definition_deletion_sync(
        self,
        guild_id: int,
        achievement_key: str,
    ) -> AchievementDeletionPreview:
        with self._connection() as connection:
            return self._deletion_preview(connection, guild_id, achievement_key)

    def _delete_definition_sync(
        self,
        guild_id: int,
        achievement_key: str,
        expected_award_count: int,
    ) -> AchievementDeletionPreview:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            preview = self._deletion_preview(connection, guild_id, achievement_key)
            if preview.award_count != expected_award_count:
                connection.rollback()
                raise RuntimeError("Achievement changed during deletion review")
            connection.execute(
                """
                DELETE FROM achievement_awards
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (guild_id, achievement_key),
            )
            connection.execute(
                """
                DELETE FROM achievement_definitions
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (guild_id, achievement_key),
            )
            connection.commit()
        return preview

    def _bind_role_sync(
        self,
        guild_id: int,
        achievement_key: str,
        role_id: int,
        user_ids: tuple[int, ...],
    ) -> RoleBindingResult:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT achievement_key, display_name, kind, role_id,
                       grantable, revocable, display_order
                FROM achievement_definitions
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (guild_id, achievement_key),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LookupError("Achievement does not exist")
            if row["kind"] != AchievementKind.BOOLEAN.value:
                connection.rollback()
                raise ValueError("Only boolean achievements can bind roles")
            if row["role_id"] is not None:
                connection.rollback()
                raise ValueError("Achievement already has a role binding")
            role_owner = connection.execute(
                """
                SELECT 1
                FROM achievement_definitions
                WHERE guild_id = ? AND role_id = ?
                """,
                (guild_id, role_id),
            ).fetchone()
            if role_owner is not None:
                connection.rollback()
                raise ValueError("Role is already bound to an achievement")
            connection.execute(
                """
                UPDATE achievement_definitions
                SET role_id = ?
                WHERE guild_id = ? AND achievement_key = ?
                """,
                (role_id, guild_id, achievement_key),
            )
            before = connection.total_changes
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
            imported_count = connection.total_changes - before
            connection.commit()
        return RoleBindingResult(
            definition=AchievementDefinition(
                key=str(row["achievement_key"]),
                display_name=str(row["display_name"]),
                kind=AchievementKind(str(row["kind"])),
                role_id=role_id,
                grantable=bool(row["grantable"]),
                revocable=bool(row["revocable"]),
                display_order=int(row["display_order"]),
            ),
            imported_count=imported_count,
        )

    def _replace_role_sync(
        self,
        guild_id: int,
        old_role_id: int,
        new_role_id: int,
        user_ids: tuple[int, ...],
    ) -> RoleBindingResult:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT achievement_key, display_name, kind, role_id,
                       grantable, revocable, display_order
                FROM achievement_definitions
                WHERE guild_id = ? AND role_id = ?
                """,
                (guild_id, old_role_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LookupError("Role is not bound to an achievement")
            target_owner = connection.execute(
                """
                SELECT 1
                FROM achievement_definitions
                WHERE guild_id = ? AND role_id = ?
                """,
                (guild_id, new_role_id),
            ).fetchone()
            if target_owner is not None:
                connection.rollback()
                raise ValueError("Role is already bound to an achievement")
            connection.execute(
                """
                UPDATE achievement_definitions
                SET role_id = ?
                WHERE guild_id = ? AND role_id = ?
                """,
                (new_role_id, guild_id, old_role_id),
            )
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO achievement_awards (
                    guild_id, user_id, achievement_key, awarded_at, state
                ) VALUES (?, ?, ?, ?, 'active')
                """,
                (
                    (
                        guild_id,
                        user_id,
                        str(row["achievement_key"]),
                        now,
                    )
                    for user_id in user_ids
                ),
            )
            imported_count = connection.total_changes - before
            connection.commit()
        return RoleBindingResult(
            definition=AchievementDefinition(
                key=str(row["achievement_key"]),
                display_name=str(row["display_name"]),
                kind=AchievementKind(str(row["kind"])),
                role_id=new_role_id,
                grantable=bool(row["grantable"]),
                revocable=bool(row["revocable"]),
                display_order=int(row["display_order"]),
            ),
            imported_count=imported_count,
        )

    def _apply_discord_snapshot_sync(
        self,
        guild_id: int,
        gate_tiers: dict[int, int],
        boolean_users: dict[str, tuple[int, ...]],
    ) -> DiscordSnapshotResult:
        if any(count < 0 for count in gate_tiers.values()):
            raise ValueError("Gate tiers cannot be negative")
        now = datetime.now(timezone.utc).isoformat()
        changed_user_ids: set[int] = set()
        gate_users_changed = 0
        boolean_grants = 0
        boolean_revocations = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT user_id, COUNT(*) AS completed_count
                FROM achievement_awards
                WHERE guild_id = ?
                    AND achievement_key = ?
                    AND ordinal IS NOT NULL
                    AND state IN ('pending', 'active')
                GROUP BY user_id
                """,
                (guild_id, STARGATE_COMPLETED_KEY),
            ).fetchall()
            stored_gate_tiers = {
                int(row["user_id"]): int(row["completed_count"])
                for row in rows
            }
            for user_id in set(stored_gate_tiers) | set(gate_tiers):
                current_count = stored_gate_tiers.get(user_id, 0)
                desired_count = gate_tiers.get(user_id, 0)
                if current_count == desired_count:
                    continue
                gate_users_changed += 1
                changed_user_ids.add(user_id)
                connection.execute(
                    """
                    UPDATE achievement_awards
                    SET state = 'revoked', revoked_at = ?
                    WHERE guild_id = ? AND user_id = ?
                        AND achievement_key = ? AND ordinal > ?
                        AND state IN ('pending', 'active')
                    """,
                    (
                        now,
                        guild_id,
                        user_id,
                        STARGATE_COMPLETED_KEY,
                        desired_count,
                    ),
                )
                connection.execute(
                    """
                    UPDATE achievement_awards
                    SET state = 'active', revoked_at = NULL
                    WHERE guild_id = ? AND user_id = ?
                        AND achievement_key = ? AND ordinal <= ?
                        AND state = 'revoked'
                    """,
                    (
                        guild_id,
                        user_id,
                        STARGATE_COMPLETED_KEY,
                        desired_count,
                    ),
                )
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO achievement_awards (
                        guild_id, user_id, achievement_key, ordinal,
                        awarded_at, state
                    ) VALUES (?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        (
                            guild_id,
                            user_id,
                            STARGATE_COMPLETED_KEY,
                            ordinal,
                            now,
                        )
                        for ordinal in range(1, desired_count + 1)
                    ),
                )

            for achievement_key, desired_user_ids in boolean_users.items():
                desired_users = set(desired_user_ids)
                rows = connection.execute(
                    """
                    SELECT user_id
                    FROM achievement_awards
                    WHERE guild_id = ? AND achievement_key = ?
                        AND ordinal IS NULL AND state = 'active'
                    """,
                    (guild_id, achievement_key),
                ).fetchall()
                stored_users = {int(row["user_id"]) for row in rows}
                users_to_revoke = stored_users - desired_users
                users_to_grant = desired_users - stored_users
                connection.executemany(
                    """
                        UPDATE achievement_awards
                        SET state = 'revoked', revoked_at = ?
                        WHERE guild_id = ? AND achievement_key = ?
                            AND ordinal IS NULL AND state = 'active'
                            AND user_id = ?
                    """,
                    (
                        (now, guild_id, achievement_key, user_id)
                        for user_id in users_to_revoke
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO achievement_awards (
                        guild_id, user_id, achievement_key, awarded_at, state
                    ) VALUES (?, ?, ?, ?, 'active')
                    """,
                    (
                        (guild_id, user_id, achievement_key, now)
                        for user_id in users_to_grant
                    ),
                )
                boolean_revocations += len(users_to_revoke)
                boolean_grants += len(users_to_grant)
                changed_user_ids.update(users_to_revoke)
                changed_user_ids.update(users_to_grant)
            connection.commit()
        return DiscordSnapshotResult(
            changed_users=len(changed_user_ids),
            gate_users_changed=gate_users_changed,
            boolean_grants=boolean_grants,
            boolean_revocations=boolean_revocations,
        )

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

    def _projected_users_for_boolean_sync(
        self, guild_id: int, achievement_key: str
    ) -> tuple[int, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id
                FROM achievement_awards
                WHERE guild_id = ? AND achievement_key = ?
                    AND ordinal IS NULL AND state IN ('pending', 'active')
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

    @staticmethod
    def _definition_from_row(row: sqlite3.Row) -> AchievementDefinition:
        return AchievementDefinition(
            key=str(row["achievement_key"]),
            display_name=str(row["display_name"]),
            kind=AchievementKind(str(row["kind"])),
            role_id=int(row["role_id"]) if row["role_id"] is not None else None,
            grantable=bool(row["grantable"]),
            revocable=bool(row["revocable"]),
            display_order=int(row["display_order"]),
        )
