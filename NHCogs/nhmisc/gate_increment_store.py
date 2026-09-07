from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SourceMessageKey:
    guild_id: int
    channel_id: int
    message_id: int


@dataclass(frozen=True, slots=True)
class GateIncrementMemberPlan:
    user_id: int
    expected_gate_role_ids: tuple[int, ...]
    target_role_id: int
    target_ordinal: int | None = None
    grant_solo: bool = False


@dataclass(frozen=True, slots=True)
class GateIncrementAchievementPlan:
    key: str
    display_name: str
    role_id: int | None = None


class GateProgressConflict(RuntimeError):
    def __init__(self, user_id: int) -> None:
        super().__init__(f"Gate progress is stale for user {user_id}")
        self.user_id = user_id


class AchievementDefinitionConflict(RuntimeError):
    pass


class OperationState(str, Enum):
    APPLYING = "applying"
    COMPLETED = "completed"
    PARTIAL = "partial"


class MemberState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CONFLICT = "conflict"


class RecoveryAction(str, Enum):
    COMPLETE = "complete"
    RETRY = "retry"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class GateIncrementOperation:
    operation_id: int
    key: SourceMessageKey
    moderator_id: int | None
    state: OperationState = OperationState.APPLYING
    selected_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    conflict_count: int = 0
    result_channel_id: int | None = None
    result_message_id: int | None = None
    published_completed_count: int = 0


@dataclass(frozen=True, slots=True)
class StoredGateIncrementMember:
    position: int
    user_id: int | None
    expected_gate_role_ids: tuple[int, ...]
    target_role_id: int | None
    state: MemberState
    failure_code: str | None
    grant_solo: bool = False
    solo_awarded: bool = False
    custom_achievement_keys: tuple[str, ...] = ()
    moderation_logged: bool = False


@dataclass(frozen=True, slots=True)
class GateIncrementSnapshot:
    operation: GateIncrementOperation
    members: tuple[StoredGateIncrementMember, ...]
    custom_achievements: tuple[GateIncrementAchievementPlan, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimResult:
    created: bool
    operation: GateIncrementOperation


def classify_member_recovery(
    member: StoredGateIncrementMember,
    current_gate_role_ids: tuple[int, ...],
) -> RecoveryAction:
    current_roles = set(current_gate_role_ids)
    if member.target_role_id is not None and current_roles == {
        member.target_role_id
    }:
        return RecoveryAction.COMPLETE
    if current_roles == set(member.expected_gate_role_ids):
        return RecoveryAction.RETRY
    return RecoveryAction.CONFLICT


class GateIncrementStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def claim(
        self,
        key: SourceMessageKey,
        moderator_id: int,
        member_plans: tuple[GateIncrementMemberPlan, ...],
        custom_achievements: tuple[GateIncrementAchievementPlan, ...] = (),
    ) -> ClaimResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_sync,
                key,
                moderator_id,
                member_plans,
                custom_achievements,
            )

    async def get_operation(
        self, key: SourceMessageKey
    ) -> GateIncrementSnapshot | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_operation_sync, key)

    async def list_interrupted_operations(
        self,
    ) -> tuple[GateIncrementSnapshot, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_interrupted_operations_sync)

    async def acquire_execution_lease(
        self, key: SourceMessageKey, token: str
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._acquire_execution_lease_sync, key, token
            )

    async def release_execution_lease(
        self, key: SourceMessageKey, token: str
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._release_execution_lease_sync, key, token
            )

    async def acquire_publication_lease(
        self, key: SourceMessageKey, token: str
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._acquire_publication_lease_sync, key, token
            )

    async def release_publication_lease(
        self, key: SourceMessageKey, token: str
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._release_publication_lease_sync, key, token
            )

    async def mark_member_in_progress(
        self, key: SourceMessageKey, position: int
    ) -> None:
        await self._mark_member_state(key, position, MemberState.IN_PROGRESS)

    async def mark_member_completed(
        self, key: SourceMessageKey, position: int
    ) -> None:
        await self._mark_member_state(key, position, MemberState.COMPLETED)

    async def mark_member_failed(
        self, key: SourceMessageKey, position: int, failure_code: str
    ) -> None:
        await self._mark_member_state(
            key, position, MemberState.FAILED, failure_code
        )

    async def mark_member_conflict(
        self, key: SourceMessageKey, position: int, failure_code: str
    ) -> None:
        await self._mark_member_state(
            key, position, MemberState.CONFLICT, failure_code
        )

    async def mark_moderation_logged(
        self, key: SourceMessageKey, positions: tuple[int, ...]
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mark_moderation_logged_sync,
                key,
                positions,
            )

    async def finalize_operation(
        self, key: SourceMessageKey
    ) -> GateIncrementSnapshot:
        async with self._lock:
            await asyncio.to_thread(self._finalize_operation_sync, key)
        snapshot = await self.get_operation(key)
        if snapshot is None:
            raise KeyError(key)
        return snapshot

    async def record_result_message(
        self,
        key: SourceMessageKey,
        token: str,
        channel_id: int,
        message_id: int,
        published_completed_count: int,
    ) -> GateIncrementSnapshot:
        async with self._lock:
            await asyncio.to_thread(
                self._record_result_message_sync,
                key,
                token,
                channel_id,
                message_id,
                published_completed_count,
            )
        snapshot = await self.get_operation(key)
        if snapshot is None:
            raise KeyError(key)
        return snapshot

    async def redact_user_data(self, user_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._redact_user_data_sync, user_id)

    async def _mark_member_state(
        self,
        key: SourceMessageKey,
        position: int,
        state: MemberState,
        failure_code: str | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mark_member_state_sync,
                key,
                position,
                state,
                failure_code,
            )

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
                CREATE TABLE IF NOT EXISTS gate_increment_operations (
                    operation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    moderator_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    selected_count INTEGER NOT NULL,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    conflict_count INTEGER NOT NULL DEFAULT 0,
                    result_channel_id INTEGER,
                    result_message_id INTEGER,
                    published_completed_count INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    publication_token TEXT,
                    UNIQUE (guild_id, channel_id, source_message_id)
                );

                CREATE TABLE IF NOT EXISTS gate_increment_members (
                    operation_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    user_id INTEGER,
                    expected_gate_role_ids TEXT NOT NULL,
                    target_role_id INTEGER,
                    state TEXT NOT NULL,
                    failure_code TEXT,
                    grant_solo INTEGER NOT NULL DEFAULT 0,
                    moderation_logged INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (operation_id, position),
                    UNIQUE (operation_id, user_id),
                    FOREIGN KEY (operation_id)
                        REFERENCES gate_increment_operations (operation_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS gate_increment_achievements (
                    operation_id INTEGER NOT NULL,
                    achievement_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role_id INTEGER,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (operation_id, achievement_key),
                    UNIQUE (operation_id, position),
                    FOREIGN KEY (operation_id)
                        REFERENCES gate_increment_operations (operation_id)
                        ON DELETE CASCADE
                );

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

                CREATE UNIQUE INDEX IF NOT EXISTS achievement_ordinal_active
                ON achievement_awards (
                    guild_id, user_id, achievement_key, ordinal
                )
                WHERE ordinal IS NOT NULL
                    AND state IN ('pending', 'active');

                DROP INDEX IF EXISTS achievement_ordinal_unique;
                """
            )
            operation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(gate_increment_operations)"
                )
            }
            added_publication_count = (
                "published_completed_count" not in operation_columns
            )
            if added_publication_count:
                connection.execute(
                    """
                    ALTER TABLE gate_increment_operations
                    ADD COLUMN published_completed_count INTEGER NOT NULL DEFAULT 0
                    """
                )
            member_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(gate_increment_members)"
                )
            }
            added_moderation_log_state = "moderation_logged" not in member_columns
            if added_moderation_log_state:
                connection.execute(
                    """
                    ALTER TABLE gate_increment_members
                    ADD COLUMN moderation_logged INTEGER NOT NULL DEFAULT 0
                    """
                )
            if added_publication_count:
                connection.execute(
                    """
                    UPDATE gate_increment_operations
                    SET published_completed_count = completed_count
                    WHERE result_message_id IS NOT NULL
                    """
                )
            if added_moderation_log_state:
                connection.execute(
                    """
                    UPDATE gate_increment_members
                    SET moderation_logged = 1
                    WHERE state = 'completed'
                    """
                )
            connection.execute(
                """
                UPDATE gate_increment_operations
                SET lease_token = NULL, publication_token = NULL
                WHERE state IN ('applying', 'partial')
                    OR publication_token IS NOT NULL
                """
            )
            connection.commit()

    def _claim_sync(
        self,
        key: SourceMessageKey,
        moderator_id: int,
        member_plans: tuple[GateIncrementMemberPlan, ...],
        custom_achievements: tuple[GateIncrementAchievementPlan, ...],
    ) -> ClaimResult:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM gate_increment_operations
                    WHERE guild_id = ? AND channel_id = ?
                        AND source_message_id = ?
                    """,
                    (key.guild_id, key.channel_id, key.message_id),
                ).fetchone()
                created = row is None
                if row is None:
                    for achievement in custom_achievements:
                        definition_row = connection.execute(
                            """
                            SELECT display_name, kind, role_id, grantable
                            FROM achievement_definitions
                            WHERE guild_id = ? AND achievement_key = ?
                            """,
                            (key.guild_id, achievement.key),
                        ).fetchone()
                        if (
                            definition_row is None
                            or definition_row["display_name"]
                            != achievement.display_name
                            or definition_row["kind"] != "boolean"
                            or definition_row["role_id"] != achievement.role_id
                            or not bool(definition_row["grantable"])
                        ):
                            raise AchievementDefinitionConflict(achievement.key)
                    cursor = connection.execute(
                        """
                        INSERT INTO gate_increment_operations (
                            guild_id,
                            channel_id,
                            source_message_id,
                            moderator_id,
                            created_at,
                            updated_at,
                            state,
                            selected_count
                        ) VALUES (?, ?, ?, ?, ?, ?, 'applying', ?)
                        """,
                        (
                            key.guild_id,
                            key.channel_id,
                            key.message_id,
                            moderator_id,
                            now,
                            now,
                            len(member_plans),
                        ),
                    )
                    operation_id = int(cursor.lastrowid)
                    connection.executemany(
                        """
                        INSERT INTO gate_increment_achievements (
                            operation_id, achievement_key, display_name,
                            role_id, position
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                operation_id,
                                achievement.key,
                                achievement.display_name,
                                achievement.role_id,
                                position,
                            )
                            for position, achievement in enumerate(custom_achievements)
                        ),
                    )
                    for plan in member_plans:
                        ordinal_rows = connection.execute(
                            """
                            SELECT ordinal
                            FROM achievement_awards
                            WHERE guild_id = ? AND user_id = ?
                                AND achievement_key = 'stargate_completed'
                                AND state IN ('pending', 'active')
                            """,
                            (key.guild_id, plan.user_id),
                        ).fetchall()
                        occupied_ordinals = set()
                        for ordinal_row in ordinal_rows:
                            ordinal = ordinal_row["ordinal"]
                            if ordinal is None:
                                raise RuntimeError("Stored Stargate ordinal is missing")
                            occupied_ordinals.add(int(ordinal))
                        next_ordinal = 1
                        while next_ordinal in occupied_ordinals:
                            next_ordinal += 1
                        if (
                            plan.target_ordinal is not None
                            and plan.target_ordinal != next_ordinal
                        ):
                            raise GateProgressConflict(plan.user_id)
                        connection.execute(
                            """
                            INSERT INTO achievement_awards (
                                guild_id, user_id, achievement_key, ordinal,
                                awarded_at, source_channel_id,
                                source_message_id, gate_operation_id, state
                            ) VALUES (?, ?, 'stargate_completed', ?, ?, ?, ?, ?,
                                'pending')
                            """,
                            (
                                key.guild_id,
                                plan.user_id,
                                next_ordinal,
                                now,
                                key.channel_id,
                                key.message_id,
                                operation_id,
                            ),
                        )
                        if plan.grant_solo:
                            connection.execute(
                                """
                                INSERT OR IGNORE INTO achievement_awards (
                                    guild_id, user_id, achievement_key,
                                    awarded_at, source_channel_id,
                                    source_message_id, gate_operation_id, state
                                ) VALUES (?, ?, 'solo_gater', ?, ?, ?, ?,
                                    'pending')
                                """,
                                (
                                    key.guild_id,
                                    plan.user_id,
                                    now,
                                    key.channel_id,
                                    key.message_id,
                                    operation_id,
                                ),
                            )
                        for achievement in custom_achievements:
                            connection.execute(
                                """
                                INSERT OR IGNORE INTO achievement_awards (
                                    guild_id, user_id, achievement_key,
                                    awarded_at, source_channel_id,
                                    source_message_id, gate_operation_id, state
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                                """,
                                (
                                    key.guild_id,
                                    plan.user_id,
                                    achievement.key,
                                    now,
                                    key.channel_id,
                                    key.message_id,
                                    operation_id,
                                ),
                            )
                    connection.executemany(
                        """
                        INSERT INTO gate_increment_members (
                            operation_id,
                            position,
                            user_id,
                            expected_gate_role_ids,
                            target_role_id,
                            state,
                            grant_solo
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            (
                                operation_id,
                                position,
                                plan.user_id,
                                json.dumps(plan.expected_gate_role_ids),
                                plan.target_role_id,
                                int(plan.grant_solo),
                            )
                            for position, plan in enumerate(member_plans)
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM gate_increment_operations WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                else:
                    operation_id = int(row["operation_id"])
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return ClaimResult(
            created=created,
            operation=self._operation_from_row(row),
        )

    def _get_operation_sync(
        self, key: SourceMessageKey
    ) -> GateIncrementSnapshot | None:
        with self._connection() as connection:
            operation_row = connection.execute(
                """
                SELECT *
                FROM gate_increment_operations
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ?
                """,
                (key.guild_id, key.channel_id, key.message_id),
            ).fetchone()
            if operation_row is None:
                return None
            member_rows = connection.execute(
                """
                SELECT position, user_id, expected_gate_role_ids,
                    target_role_id, state, failure_code, grant_solo,
                    moderation_logged
                FROM gate_increment_members
                WHERE operation_id = ?
                ORDER BY position
                """,
                (operation_row["operation_id"],),
            ).fetchall()
            achievement_rows = connection.execute(
                """
                SELECT achievement_key, display_name, role_id
                FROM gate_increment_achievements
                WHERE operation_id = ?
                ORDER BY position
                """,
                (operation_row["operation_id"],),
            ).fetchall()
            award_rows = connection.execute(
                """
                SELECT user_id, achievement_key
                FROM achievement_awards
                WHERE gate_operation_id = ? AND ordinal IS NULL
                ORDER BY award_id
                """,
                (operation_row["operation_id"],),
            ).fetchall()

        award_keys_by_user: dict[int, list[str]] = {}
        for row in award_rows:
            award_keys_by_user.setdefault(int(row["user_id"]), []).append(
                str(row["achievement_key"])
            )

        return GateIncrementSnapshot(
            operation=self._operation_from_row(operation_row),
            members=tuple(
                StoredGateIncrementMember(
                    position=int(row["position"]),
                    user_id=row["user_id"],
                    expected_gate_role_ids=tuple(
                        int(role_id)
                        for role_id in json.loads(row["expected_gate_role_ids"])
                    ),
                    target_role_id=row["target_role_id"],
                    state=MemberState(row["state"]),
                    failure_code=row["failure_code"],
                    grant_solo=bool(row["grant_solo"]),
                    solo_awarded=(
                        "solo_gater"
                        in award_keys_by_user.get(int(row["user_id"]), ())
                    )
                    if row["user_id"] is not None
                    else False,
                    custom_achievement_keys=tuple(
                        key
                        for key in award_keys_by_user.get(int(row["user_id"]), ())
                        if key != "solo_gater"
                    )
                    if row["user_id"] is not None
                    else (),
                    moderation_logged=bool(row["moderation_logged"]),
                )
                for row in member_rows
            ),
            custom_achievements=tuple(
                GateIncrementAchievementPlan(
                    key=str(row["achievement_key"]),
                    display_name=str(row["display_name"]),
                    role_id=row["role_id"],
                )
                for row in achievement_rows
            ),
        )

    def _list_interrupted_operations_sync(
        self,
    ) -> tuple[GateIncrementSnapshot, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT guild_id, channel_id, source_message_id
                FROM gate_increment_operations AS operations
                WHERE state IN ('applying', 'partial')
                    OR published_completed_count < completed_count
                    OR EXISTS (
                        SELECT 1
                        FROM gate_increment_members AS members
                        WHERE members.operation_id = operations.operation_id
                            AND members.state = 'completed'
                            AND members.moderation_logged = 0
                    )
                ORDER BY operation_id
                """
            ).fetchall()
        snapshots = []
        for row in rows:
            key = SourceMessageKey(
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                message_id=int(row["source_message_id"]),
            )
            snapshot = self._get_operation_sync(key)
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots)

    def _acquire_execution_lease_sync(
        self, key: SourceMessageKey, token: str
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE gate_increment_operations
                SET lease_token = ?, updated_at = ?
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ?
                    AND state IN ('applying', 'partial')
                    AND lease_token IS NULL
                """,
                (
                    token,
                    datetime.now(timezone.utc).isoformat(),
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def _release_execution_lease_sync(
        self, key: SourceMessageKey, token: str
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE gate_increment_operations
                SET lease_token = NULL, updated_at = ?
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ? AND lease_token = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                    token,
                ),
            )
            connection.commit()

    def _acquire_publication_lease_sync(
        self, key: SourceMessageKey, token: str
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE gate_increment_operations
                SET publication_token = ?, updated_at = ?
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ?
                    AND state IN ('completed', 'partial')
                    AND completed_count > published_completed_count
                    AND publication_token IS NULL
                """,
                (
                    token,
                    datetime.now(timezone.utc).isoformat(),
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def _release_publication_lease_sync(
        self, key: SourceMessageKey, token: str
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE gate_increment_operations
                SET publication_token = NULL, updated_at = ?
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ? AND publication_token = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                    token,
                ),
            )
            connection.commit()

    def _mark_member_state_sync(
        self,
        key: SourceMessageKey,
        position: int,
        state: MemberState,
        failure_code: str | None,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            member_row = connection.execute(
                """
                SELECT members.user_id, operations.operation_id
                FROM gate_increment_members AS members
                JOIN gate_increment_operations AS operations
                    ON operations.operation_id = members.operation_id
                WHERE operations.guild_id = ? AND operations.channel_id = ?
                    AND operations.source_message_id = ?
                    AND members.position = ?
                """,
                (
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                    position,
                ),
            ).fetchone()
            if member_row is None:
                connection.rollback()
                raise KeyError((key, position))
            cursor = connection.execute(
                """
                UPDATE gate_increment_members
                SET state = ?, failure_code = ?
                WHERE operation_id = (
                    SELECT operation_id
                    FROM gate_increment_operations
                    WHERE guild_id = ? AND channel_id = ?
                        AND source_message_id = ?
                ) AND position = ?
                """,
                (
                    state.value,
                    failure_code,
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                    position,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise KeyError((key, position))
            if state is MemberState.COMPLETED and member_row["user_id"] is not None:
                connection.execute(
                    """
                    UPDATE achievement_awards
                    SET state = 'active'
                    WHERE gate_operation_id = ? AND user_id = ?
                        AND state = 'pending'
                    """,
                    (member_row["operation_id"], member_row["user_id"]),
                )
            connection.execute(
                """
                UPDATE gate_increment_operations
                SET updated_at = ?
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                ),
            )
            connection.commit()

    def _mark_moderation_logged_sync(
        self, key: SourceMessageKey, positions: tuple[int, ...]
    ) -> None:
        if not positions:
            return
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in positions)
            connection.execute(
                f"""
                UPDATE gate_increment_members
                SET moderation_logged = 1
                WHERE operation_id = (
                    SELECT operation_id
                    FROM gate_increment_operations
                    WHERE guild_id = ? AND channel_id = ?
                        AND source_message_id = ?
                ) AND position IN ({placeholders})
                    AND state = 'completed'
                """,
                (key.guild_id, key.channel_id, key.message_id, *positions),
            )
            connection.commit()

    def _finalize_operation_sync(self, key: SourceMessageKey) -> None:
        with self._connection() as connection:
            operation_row = connection.execute(
                """
                SELECT operation_id, selected_count
                FROM gate_increment_operations
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ?
                """,
                (key.guild_id, key.channel_id, key.message_id),
            ).fetchone()
            if operation_row is None:
                raise KeyError(key)
            counts = {
                row["state"]: int(row["member_count"])
                for row in connection.execute(
                    """
                    SELECT state, COUNT(*) AS member_count
                    FROM gate_increment_members
                    WHERE operation_id = ?
                    GROUP BY state
                    """,
                    (operation_row["operation_id"],),
                )
            }
            completed_count = counts.get(MemberState.COMPLETED.value, 0)
            failed_count = counts.get(MemberState.FAILED.value, 0)
            conflict_count = counts.get(MemberState.CONFLICT.value, 0)
            state = (
                OperationState.COMPLETED
                if completed_count == int(operation_row["selected_count"])
                else OperationState.PARTIAL
            )
            connection.execute(
                """
                UPDATE gate_increment_operations
                SET state = ?, completed_count = ?, failed_count = ?,
                    conflict_count = ?, lease_token = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    state.value,
                    completed_count,
                    failed_count,
                    conflict_count,
                    datetime.now(timezone.utc).isoformat(),
                    operation_row["operation_id"],
                ),
            )
            connection.commit()

    def _record_result_message_sync(
        self,
        key: SourceMessageKey,
        token: str,
        channel_id: int,
        message_id: int,
        published_completed_count: int,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE gate_increment_operations
                SET result_channel_id = ?, result_message_id = ?,
                    published_completed_count = ?,
                    publication_token = NULL, updated_at = ?
                WHERE guild_id = ? AND channel_id = ?
                    AND source_message_id = ?
                    AND state IN ('completed', 'partial')
                    AND publication_token = ?
                """,
                (
                    channel_id,
                    message_id,
                    published_completed_count,
                    datetime.now(timezone.utc).isoformat(),
                    key.guild_id,
                    key.channel_id,
                    key.message_id,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise KeyError(key)
            connection.commit()

    def _redact_user_data_sync(self, user_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE gate_increment_operations
                SET moderator_id = NULL
                WHERE moderator_id = ?
                """,
                (user_id,),
            )
            connection.execute(
                """
                UPDATE gate_increment_members
                SET user_id = NULL,
                    expected_gate_role_ids = '[]',
                    target_role_id = NULL,
                    failure_code = NULL
                WHERE user_id = ?
                    AND state = 'completed'
                    AND operation_id IN (
                        SELECT operation_id
                        FROM gate_increment_operations
                        WHERE state IN ('completed', 'partial')
                            AND (
                                result_message_id IS NOT NULL
                                OR completed_count = 0
                            )
                    )
                """,
                (user_id,),
            )
            connection.commit()

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> GateIncrementOperation:
        return GateIncrementOperation(
            operation_id=int(row["operation_id"]),
            key=SourceMessageKey(
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                message_id=int(row["source_message_id"]),
            ),
            moderator_id=row["moderator_id"],
            state=OperationState(row["state"]),
            selected_count=int(row["selected_count"]),
            completed_count=int(row["completed_count"]),
            failed_count=int(row["failed_count"]),
            conflict_count=int(row["conflict_count"]),
            result_channel_id=row["result_channel_id"],
            result_message_id=row["result_message_id"],
            published_completed_count=int(row["published_completed_count"]),
        )
