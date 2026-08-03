from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .gate_migration import MemberMigrationPlan, MemberSnapshot, MigrationPlan


class ActiveMigrationExistsError(RuntimeError):
    pass


class InvalidStateTransitionError(RuntimeError):
    pass


class SchemaState(str, Enum):
    LEGACY = "legacy"
    MIGRATING = "migrating"
    CURRENT = "current"


class RunState(str, Enum):
    PREPARING = "preparing"
    PREPARED = "prepared"
    CONFIRMED = "confirmed"
    APPLYING = "applying"
    APPLY_FAILED = "apply_failed"
    APPLIED = "applied"
    VERIFIED = "verified"
    RESTORING = "restoring"
    RESTORE_FAILED = "restore_failed"
    RESTORED = "restored"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FINALIZED = "finalized"


class MemberStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DEPARTED = "departed"
    CONFLICT = "conflict"
    SKIPPED_UNMODIFIABLE = "skipped_unmodifiable"
    FAILED = "failed"


class RestoreStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DEPARTED = "departed"
    SKIPPED_UNMODIFIABLE = "skipped_unmodifiable"
    FAILED = "failed"


RUN_TRANSITIONS = {
    RunState.PREPARING: frozenset({RunState.PREPARED, RunState.CANCELLED}),
    RunState.PREPARED: frozenset(
        {
            RunState.CONFIRMED,
            RunState.APPLYING,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.CONFIRMED: frozenset(
        {RunState.APPLYING, RunState.CANCELLED, RunState.EXPIRED}
    ),
    RunState.APPLYING: frozenset({RunState.APPLY_FAILED, RunState.APPLIED}),
    RunState.APPLY_FAILED: frozenset({RunState.APPLYING, RunState.RESTORING}),
    RunState.APPLIED: frozenset(
        {RunState.APPLYING, RunState.VERIFIED, RunState.RESTORING}
    ),
    RunState.VERIFIED: frozenset({RunState.FINALIZED, RunState.RESTORING}),
    RunState.RESTORING: frozenset({RunState.RESTORE_FAILED, RunState.RESTORED}),
    RunState.RESTORE_FAILED: frozenset({RunState.RESTORING}),
    RunState.RESTORED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.EXPIRED: frozenset(),
    RunState.FINALIZED: frozenset(),
}


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    guild_id: int
    operator_id: int
    channel_id: int
    created_at: str
    snapshot_sha256: str
    state: RunState


@dataclass(frozen=True)
class StoredMember:
    plan: MemberMigrationPlan
    status: MemberStatus
    attempts: int
    error_code: str | None
    restore_status: RestoreStatus
    restore_attempts: int
    restore_error_code: str | None


@dataclass(frozen=True)
class StoredArtifact:
    run_id: str
    kind: str
    part_index: int
    filename: str
    sha256: str
    size: int
    channel_id: int
    message_id: int


@dataclass(frozen=True)
class CompletionReceipt:
    run_id: str
    guild_id: int
    completed_at: str
    snapshot_sha256: str
    backup_channel_id: int
    backup_message_ids: tuple[int, ...]


class GateMigrationStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def create_run(
        self,
        *,
        run_id: str,
        guild_id: int,
        operator_id: int,
        channel_id: int,
        created_at: str,
        snapshot_sha256: str,
        plan: MigrationPlan,
    ) -> StoredRun:
        run = StoredRun(
            run_id=run_id,
            guild_id=guild_id,
            operator_id=operator_id,
            channel_id=channel_id,
            created_at=created_at,
            snapshot_sha256=snapshot_sha256,
            state=RunState.PREPARING,
        )
        async with self._lock:
            await asyncio.to_thread(self._create_run_sync, run, plan)
        return run

    async def get_run(self, run_id: str) -> StoredRun | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_run_sync, run_id)

    async def get_active_run(self, guild_id: int) -> StoredRun | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_active_run_sync, guild_id)

    async def get_members(self, run_id: str) -> tuple[StoredMember, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._get_members_sync, run_id)

    async def get_schema_state(self, guild_id: int) -> SchemaState:
        async with self._lock:
            return await asyncio.to_thread(self._get_schema_state_sync, guild_id)

    async def transition_run(self, run_id: str, state: RunState) -> StoredRun:
        async with self._lock:
            return await asyncio.to_thread(self._transition_run_sync, run_id, state)

    async def begin_member_attempt(self, run_id: str, user_id: int) -> StoredMember:
        async with self._lock:
            return await asyncio.to_thread(
                self._begin_member_attempt_sync, run_id, user_id
            )

    async def complete_unchanged_members(self, run_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._complete_unchanged_members_sync, run_id)

    async def mark_unattempted_member_departed(
        self, run_id: str, user_id: int
    ) -> StoredMember:
        async with self._lock:
            return await asyncio.to_thread(
                self._mark_unattempted_member_departed_sync, run_id, user_id
            )

    async def set_member_status(
        self,
        run_id: str,
        user_id: int,
        status: MemberStatus,
        *,
        error_code: str | None = None,
    ) -> StoredMember:
        async with self._lock:
            return await asyncio.to_thread(
                self._set_member_status_sync,
                run_id,
                user_id,
                status,
                error_code,
            )

    async def begin_restore_attempt(self, run_id: str, user_id: int) -> StoredMember:
        async with self._lock:
            return await asyncio.to_thread(
                self._begin_restore_attempt_sync, run_id, user_id
            )

    async def set_restore_status(
        self,
        run_id: str,
        user_id: int,
        status: RestoreStatus,
        *,
        error_code: str | None = None,
    ) -> StoredMember:
        async with self._lock:
            return await asyncio.to_thread(
                self._set_restore_status_sync,
                run_id,
                user_id,
                status,
                error_code,
            )

    async def record_artifact(self, artifact: StoredArtifact) -> None:
        async with self._lock:
            await asyncio.to_thread(self._record_artifact_sync, artifact)

    async def get_artifacts(self, run_id: str) -> tuple[StoredArtifact, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._get_artifacts_sync, run_id)

    async def finalize(self, receipt: CompletionReceipt) -> None:
        async with self._lock:
            await asyncio.to_thread(self._finalize_sync, receipt)

    async def get_receipt(self, run_id: str) -> CompletionReceipt | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_receipt_sync, run_id)

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_migration_state (
                    guild_id INTEGER PRIMARY KEY,
                    schema_state TEXT NOT NULL DEFAULT 'legacy',
                    active_run_id TEXT
                );

                CREATE TABLE IF NOT EXISTS migration_runs (
                    run_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    operator_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS migration_members (
                    run_id TEXT NOT NULL REFERENCES migration_runs(run_id)
                        ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    role_ids TEXT NOT NULL,
                    sp_count INTEGER NOT NULL,
                    mp_count INTEGER NOT NULL,
                    target_tier INTEGER NOT NULL,
                    target_gate_role_ids TEXT NOT NULL,
                    duplicate_sp_role_ids TEXT NOT NULL,
                    duplicate_mp_role_ids TEXT NOT NULL,
                    original_gate_role_ids TEXT NOT NULL,
                    unexpected_role_ids TEXT NOT NULL,
                    changed INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    restore_status TEXT NOT NULL DEFAULT 'pending',
                    restore_attempts INTEGER NOT NULL DEFAULT 0,
                    restore_error_code TEXT,
                    PRIMARY KEY (run_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS migration_artifacts (
                    run_id TEXT NOT NULL REFERENCES migration_runs(run_id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    part_index INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (run_id, kind, part_index)
                );

                CREATE TABLE IF NOT EXISTS migration_receipts (
                    run_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    backup_channel_id INTEGER NOT NULL,
                    backup_message_ids TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS migration_runs_guild_idx
                    ON migration_runs(guild_id);
                CREATE INDEX IF NOT EXISTS migration_members_status_idx
                    ON migration_members(run_id, status, user_id);
                """
            )

    def _create_run_sync(self, run: StoredRun, plan: MigrationPlan) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO guild_migration_state (guild_id)
                VALUES (?)
                ON CONFLICT(guild_id) DO NOTHING
                """,
                (run.guild_id,),
            )
            state = connection.execute(
                """
                SELECT schema_state, active_run_id
                FROM guild_migration_state
                WHERE guild_id = ?
                """,
                (run.guild_id,),
            ).fetchone()
            if (
                SchemaState(state["schema_state"]) is not SchemaState.LEGACY
                or state["active_run_id"] is not None
            ):
                raise ActiveMigrationExistsError(
                    f"Guild {run.guild_id} has already started a migration"
                )
            connection.execute(
                """
                INSERT INTO migration_runs (
                    run_id, guild_id, operator_id, channel_id, created_at,
                    snapshot_sha256, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.guild_id,
                    run.operator_id,
                    run.channel_id,
                    run.created_at,
                    run.snapshot_sha256,
                    run.state,
                ),
            )
            connection.executemany(
                """
                INSERT INTO migration_members (
                    run_id, user_id, username, role_ids, sp_count, mp_count,
                    target_tier, target_gate_role_ids, duplicate_sp_role_ids,
                    duplicate_mp_role_ids, original_gate_role_ids,
                    unexpected_role_ids, changed, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                [self._member_values(run.run_id, member) for member in plan.members],
            )
            connection.execute(
                """
                UPDATE guild_migration_state
                SET active_run_id = ?
                WHERE guild_id = ?
                """,
                (run.run_id, run.guild_id),
            )

    def _get_run_sync(self, run_id: str) -> StoredRun | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT run_id, guild_id, operator_id, channel_id, created_at,
                       snapshot_sha256, state
                FROM migration_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredRun(
            run_id=row["run_id"],
            guild_id=row["guild_id"],
            operator_id=row["operator_id"],
            channel_id=row["channel_id"],
            created_at=row["created_at"],
            snapshot_sha256=row["snapshot_sha256"],
            state=RunState(row["state"]),
        )

    def _get_active_run_sync(self, guild_id: int) -> StoredRun | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT active_run_id
                FROM guild_migration_state
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        if row is None or row["active_run_id"] is None:
            return None
        return self._get_run_sync(row["active_run_id"])

    def _get_members_sync(self, run_id: str) -> tuple[StoredMember, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM migration_members
                WHERE run_id = ?
                ORDER BY user_id
                """,
                (run_id,),
            ).fetchall()
        return tuple(self._stored_member(row) for row in rows)

    def _get_schema_state_sync(self, guild_id: int) -> SchemaState:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT schema_state
                FROM guild_migration_state
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        return SchemaState.LEGACY if row is None else SchemaState(row["schema_state"])

    def _record_artifact_sync(self, artifact: StoredArtifact) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO migration_artifacts (
                    run_id, kind, part_index, filename, sha256, size,
                    channel_id, message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.run_id,
                    artifact.kind,
                    artifact.part_index,
                    artifact.filename,
                    artifact.sha256,
                    artifact.size,
                    artifact.channel_id,
                    artifact.message_id,
                ),
            )

    def _get_artifacts_sync(self, run_id: str) -> tuple[StoredArtifact, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, kind, part_index, filename, sha256, size,
                       channel_id, message_id
                FROM migration_artifacts
                WHERE run_id = ?
                ORDER BY message_id, kind, part_index
                """,
                (run_id,),
            ).fetchall()
        return tuple(
            StoredArtifact(
                run_id=row["run_id"],
                kind=row["kind"],
                part_index=row["part_index"],
                filename=row["filename"],
                sha256=row["sha256"],
                size=row["size"],
                channel_id=row["channel_id"],
                message_id=row["message_id"],
            )
            for row in rows
        )

    def _finalize_sync(self, receipt: CompletionReceipt) -> None:
        with self._connection() as connection:
            run = connection.execute(
                """
                SELECT guild_id, snapshot_sha256, state
                FROM migration_runs
                WHERE run_id = ?
                """,
                (receipt.run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(receipt.run_id)
            if RunState(run["state"]) is not RunState.VERIFIED:
                raise InvalidStateTransitionError(
                    "Only a verified migration can be finalized"
                )
            if (
                run["guild_id"] != receipt.guild_id
                or run["snapshot_sha256"] != receipt.snapshot_sha256
            ):
                raise ValueError("Completion receipt does not match the migration run")
            connection.execute(
                """
                INSERT INTO migration_receipts (
                    run_id, guild_id, completed_at, snapshot_sha256,
                    backup_channel_id, backup_message_ids
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.run_id,
                    receipt.guild_id,
                    receipt.completed_at,
                    receipt.snapshot_sha256,
                    receipt.backup_channel_id,
                    _encode_ids(receipt.backup_message_ids),
                ),
            )
            connection.execute(
                "DELETE FROM migration_runs WHERE run_id = ?",
                (receipt.run_id,),
            )
            connection.execute(
                """
                UPDATE guild_migration_state
                SET schema_state = ?, active_run_id = NULL
                WHERE guild_id = ? AND active_run_id = ?
                """,
                (SchemaState.CURRENT.value, receipt.guild_id, receipt.run_id),
            )

    def _get_receipt_sync(self, run_id: str) -> CompletionReceipt | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT run_id, guild_id, completed_at, snapshot_sha256,
                       backup_channel_id, backup_message_ids
                FROM migration_receipts
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return CompletionReceipt(
            run_id=row["run_id"],
            guild_id=row["guild_id"],
            completed_at=row["completed_at"],
            snapshot_sha256=row["snapshot_sha256"],
            backup_channel_id=row["backup_channel_id"],
            backup_message_ids=_decode_ids(row["backup_message_ids"]),
        )

    def _transition_run_sync(self, run_id: str, state: RunState) -> StoredRun:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT guild_id, state FROM migration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current_state = RunState(row["state"])
            if state not in RUN_TRANSITIONS[current_state]:
                raise InvalidStateTransitionError(
                    f"Cannot transition run from {current_state.value} to {state.value}"
                )
            connection.execute(
                "UPDATE migration_runs SET state = ? WHERE run_id = ?",
                (state.value, run_id),
            )
            schema_state = {
                RunState.APPLYING: SchemaState.MIGRATING,
                RunState.VERIFIED: SchemaState.CURRENT,
                RunState.RESTORED: SchemaState.LEGACY,
            }.get(state)
            if schema_state is not None:
                connection.execute(
                    """
                    UPDATE guild_migration_state
                    SET schema_state = ?
                    WHERE guild_id = ?
                    """,
                    (schema_state.value, row["guild_id"]),
                )
            if state in {
                RunState.CANCELLED,
                RunState.EXPIRED,
                RunState.RESTORED,
                RunState.FINALIZED,
            }:
                connection.execute(
                    """
                    UPDATE guild_migration_state
                    SET active_run_id = NULL
                    WHERE guild_id = ? AND active_run_id = ?
                    """,
                    (row["guild_id"], run_id),
                )
        stored_run = self._get_run_sync(run_id)
        if stored_run is None:
            raise KeyError(run_id)
        return stored_run

    def _begin_member_attempt_sync(self, run_id: str, user_id: int) -> StoredMember:
        with self._connection() as connection:
            row = self._member_row(connection, run_id, user_id)
            current_status = MemberStatus(row["status"])
            if current_status not in {
                MemberStatus.PENDING,
                MemberStatus.FAILED,
                MemberStatus.CONFLICT,
                MemberStatus.SKIPPED_UNMODIFIABLE,
            }:
                raise InvalidStateTransitionError(
                    f"Cannot begin member attempt from {current_status.value}"
                )
            connection.execute(
                """
                UPDATE migration_members
                SET status = ?, attempts = attempts + 1, error_code = NULL
                WHERE run_id = ? AND user_id = ?
                """,
                (MemberStatus.IN_PROGRESS.value, run_id, user_id),
            )
            updated = self._member_row(connection, run_id, user_id)
        return self._stored_member(updated)

    def _complete_unchanged_members_sync(self, run_id: str) -> None:
        with self._connection() as connection:
            run = connection.execute(
                "SELECT state FROM migration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None or RunState(run["state"]) is not RunState.APPLYING:
                raise InvalidStateTransitionError(
                    "Unchanged members can only be completed while applying"
                )
            connection.execute(
                """
                UPDATE migration_members
                SET status = ?, error_code = NULL
                WHERE run_id = ? AND changed = 0 AND status = ?
                """,
                (
                    MemberStatus.COMPLETED.value,
                    run_id,
                    MemberStatus.PENDING.value,
                ),
            )

    def _mark_unattempted_member_departed_sync(
        self, run_id: str, user_id: int
    ) -> StoredMember:
        with self._connection() as connection:
            row = self._member_row(connection, run_id, user_id)
            if (
                MemberStatus(row["status"]) is not MemberStatus.PENDING
                or row["changed"]
            ):
                raise InvalidStateTransitionError(
                    "Only an unchanged pending member can depart without an attempt"
                )
            connection.execute(
                """
                UPDATE migration_members
                SET status = ?, error_code = NULL
                WHERE run_id = ? AND user_id = ?
                """,
                (MemberStatus.DEPARTED.value, run_id, user_id),
            )
            updated = self._member_row(connection, run_id, user_id)
        return self._stored_member(updated)

    def _set_member_status_sync(
        self,
        run_id: str,
        user_id: int,
        status: MemberStatus,
        error_code: str | None,
    ) -> StoredMember:
        allowed_outcomes = {
            MemberStatus.COMPLETED,
            MemberStatus.DEPARTED,
            MemberStatus.CONFLICT,
            MemberStatus.SKIPPED_UNMODIFIABLE,
            MemberStatus.FAILED,
        }
        if status not in allowed_outcomes:
            raise InvalidStateTransitionError(
                f"Cannot finish a member attempt as {status.value}"
            )
        with self._connection() as connection:
            row = self._member_row(connection, run_id, user_id)
            current_status = MemberStatus(row["status"])
            if current_status is not MemberStatus.IN_PROGRESS:
                raise InvalidStateTransitionError(
                    f"Cannot finish member attempt from {current_status.value}"
                )
            connection.execute(
                """
                UPDATE migration_members
                SET status = ?, error_code = ?
                WHERE run_id = ? AND user_id = ?
                """,
                (status.value, error_code, run_id, user_id),
            )
            updated = self._member_row(connection, run_id, user_id)
        return self._stored_member(updated)

    def _begin_restore_attempt_sync(self, run_id: str, user_id: int) -> StoredMember:
        with self._connection() as connection:
            row = self._member_row(connection, run_id, user_id)
            current_status = RestoreStatus(row["restore_status"])
            if row["attempts"] == 0 or current_status not in {
                RestoreStatus.PENDING,
                RestoreStatus.FAILED,
                RestoreStatus.SKIPPED_UNMODIFIABLE,
            }:
                raise InvalidStateTransitionError(
                    f"Cannot begin restore attempt from {current_status.value}"
                )
            connection.execute(
                """
                UPDATE migration_members
                SET restore_status = ?,
                    restore_attempts = restore_attempts + 1,
                    restore_error_code = NULL
                WHERE run_id = ? AND user_id = ?
                """,
                (RestoreStatus.IN_PROGRESS.value, run_id, user_id),
            )
            updated = self._member_row(connection, run_id, user_id)
        return self._stored_member(updated)

    def _set_restore_status_sync(
        self,
        run_id: str,
        user_id: int,
        status: RestoreStatus,
        error_code: str | None,
    ) -> StoredMember:
        allowed_outcomes = {
            RestoreStatus.COMPLETED,
            RestoreStatus.DEPARTED,
            RestoreStatus.SKIPPED_UNMODIFIABLE,
            RestoreStatus.FAILED,
        }
        if status not in allowed_outcomes:
            raise InvalidStateTransitionError(
                f"Cannot finish a restore attempt as {status.value}"
            )
        with self._connection() as connection:
            row = self._member_row(connection, run_id, user_id)
            current_status = RestoreStatus(row["restore_status"])
            if current_status is not RestoreStatus.IN_PROGRESS:
                raise InvalidStateTransitionError(
                    f"Cannot finish restore attempt from {current_status.value}"
                )
            connection.execute(
                """
                UPDATE migration_members
                SET restore_status = ?, restore_error_code = ?
                WHERE run_id = ? AND user_id = ?
                """,
                (status.value, error_code, run_id, user_id),
            )
            updated = self._member_row(connection, run_id, user_id)
        return self._stored_member(updated)

    @staticmethod
    def _member_row(
        connection: sqlite3.Connection, run_id: str, user_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM migration_members
            WHERE run_id = ? AND user_id = ?
            """,
            (run_id, user_id),
        ).fetchone()
        if row is None:
            raise KeyError((run_id, user_id))
        return row

    @staticmethod
    def _member_values(run_id: str, member: MemberMigrationPlan) -> tuple:
        return (
            run_id,
            member.snapshot.user_id,
            member.snapshot.username,
            _encode_ids(member.snapshot.role_ids),
            member.sp_count,
            member.mp_count,
            member.target_tier,
            _encode_ids(member.target_gate_role_ids),
            _encode_ids(member.duplicate_sp_role_ids),
            _encode_ids(member.duplicate_mp_role_ids),
            _encode_ids(member.original_gate_role_ids),
            _encode_ids(member.unexpected_role_ids),
            member.changed,
        )

    @staticmethod
    def _stored_member(row: sqlite3.Row) -> StoredMember:
        snapshot = MemberSnapshot(
            user_id=row["user_id"],
            username=row["username"],
            role_ids=_decode_ids(row["role_ids"]),
        )
        plan = MemberMigrationPlan(
            snapshot=snapshot,
            sp_count=row["sp_count"],
            mp_count=row["mp_count"],
            target_tier=row["target_tier"],
            target_gate_role_ids=frozenset(_decode_ids(row["target_gate_role_ids"])),
            duplicate_sp_role_ids=_decode_ids(row["duplicate_sp_role_ids"]),
            duplicate_mp_role_ids=_decode_ids(row["duplicate_mp_role_ids"]),
            original_gate_role_ids=frozenset(
                _decode_ids(row["original_gate_role_ids"])
            ),
            unexpected_role_ids=_decode_ids(row["unexpected_role_ids"]),
            changed=bool(row["changed"]),
        )
        return StoredMember(
            plan=plan,
            status=MemberStatus(row["status"]),
            attempts=row["attempts"],
            error_code=row["error_code"],
            restore_status=RestoreStatus(row["restore_status"]),
            restore_attempts=row["restore_attempts"],
            restore_error_code=row["restore_error_code"],
        )


def _encode_ids(role_ids) -> str:
    return json.dumps([str(role_id) for role_id in role_ids], separators=(",", ":"))


def _decode_ids(payload: str) -> tuple[int, ...]:
    return tuple(int(role_id) for role_id in json.loads(payload))
