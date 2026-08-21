from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class MigrationPhase(str, Enum):
    NOT_PLANNED = "not_planned"
    PLANNED = "planned"
    IMPORTED_NOT_ACTIVE = "imported_not_active"
    COMPLETE = "complete"


@dataclass(frozen=True)
class MigrationState:
    phase: MigrationPhase
    source_digest: str | None = None
    destination_digest: str | None = None
    updated_at: datetime | None = None


class MigrationApplyError(RuntimeError):
    pass


class MigrationStateStore:
    def __init__(self, database_path: Path):
        self._database_path = Path(database_path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_sync(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS custom_command_migration_state (
                       singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                       phase TEXT NOT NULL,
                       source_digest TEXT,
                       destination_digest TEXT,
                       updated_at TEXT NOT NULL
                   )"""
            )

    async def get(self) -> MigrationState:
        return await asyncio.to_thread(self._get_sync)

    def _get_sync(self) -> MigrationState:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM custom_command_migration_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return MigrationState(MigrationPhase.NOT_PLANNED)
        return MigrationState(
            phase=MigrationPhase(row["phase"]),
            source_digest=row["source_digest"],
            destination_digest=row["destination_digest"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def save(
        self,
        phase: MigrationPhase,
        *,
        source_digest: str | None,
        destination_digest: str | None,
    ) -> MigrationState:
        return await asyncio.to_thread(
            self._save_sync,
            phase,
            source_digest,
            destination_digest,
        )

    async def transition(
        self,
        expected_phase: MigrationPhase,
        phase: MigrationPhase,
        *,
        source_digest: str | None,
        destination_digest: str | None,
    ) -> MigrationState:
        return await asyncio.to_thread(
            self._transition_sync,
            expected_phase,
            phase,
            source_digest,
            destination_digest,
        )

    def _save_sync(
        self,
        phase: MigrationPhase,
        source_digest: str | None,
        destination_digest: str | None,
    ) -> MigrationState:
        updated_at = datetime.now(timezone.utc)
        with closing(self._connect()) as connection, connection:
            current = connection.execute(
                "SELECT phase FROM custom_command_migration_state WHERE singleton = 1"
            ).fetchone()
            if current is not None and current["phase"] == MigrationPhase.COMPLETE.value:
                raise MigrationApplyError("Completed migration state cannot be changed")
            connection.execute(
                """INSERT INTO custom_command_migration_state
                   (singleton, phase, source_digest, destination_digest, updated_at)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                       phase = excluded.phase,
                       source_digest = excluded.source_digest,
                       destination_digest = excluded.destination_digest,
                       updated_at = excluded.updated_at""",
                (
                    phase.value,
                    source_digest,
                    destination_digest,
                    updated_at.isoformat(),
                ),
            )
        return MigrationState(
            phase=phase,
            source_digest=source_digest,
            destination_digest=destination_digest,
            updated_at=updated_at,
        )

    def _transition_sync(
        self,
        expected_phase: MigrationPhase,
        phase: MigrationPhase,
        source_digest: str | None,
        destination_digest: str | None,
    ) -> MigrationState:
        updated_at = datetime.now(timezone.utc)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE custom_command_migration_state
                   SET phase = ?, source_digest = ?, destination_digest = ?, updated_at = ?
                   WHERE singleton = 1 AND phase = ?""",
                (
                    phase.value,
                    source_digest,
                    destination_digest,
                    updated_at.isoformat(),
                    expected_phase.value,
                ),
            )
            if cursor.rowcount != 1:
                raise MigrationApplyError(
                    f"Migration state is no longer {expected_phase.value}"
                )
        return MigrationState(
            phase=phase,
            source_digest=source_digest,
            destination_digest=destination_digest,
            updated_at=updated_at,
        )
