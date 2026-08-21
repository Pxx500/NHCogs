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


class MigrationState(str, Enum):
    PLANNED = "planned"
    QUIESCING = "quiescing"
    BACKUP_COMPLETE = "backup_complete"
    LOADING_SUITE = "loading_suite"
    VALIDATED = "validated"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    MANUAL_INTERVENTION = "manual_intervention"
    RESTART_VERIFIED = "restart_verified"
    FINALIZED = "finalized"


_TRANSITIONS = {
    MigrationState.PLANNED: {MigrationState.QUIESCING},
    MigrationState.QUIESCING: {
        MigrationState.BACKUP_COMPLETE,
        MigrationState.ROLLING_BACK,
    },
    MigrationState.BACKUP_COMPLETE: {
        MigrationState.LOADING_SUITE,
        MigrationState.ROLLING_BACK,
    },
    MigrationState.LOADING_SUITE: {
        MigrationState.VALIDATED,
        MigrationState.ROLLING_BACK,
    },
    MigrationState.VALIDATED: {
        MigrationState.COMMITTED,
        MigrationState.ROLLING_BACK,
    },
    MigrationState.COMMITTED: {MigrationState.RESTART_VERIFIED},
    MigrationState.ROLLING_BACK: {MigrationState.ROLLED_BACK},
    MigrationState.RESTART_VERIFIED: {MigrationState.FINALIZED},
    MigrationState.ROLLED_BACK: set(),
    MigrationState.MANUAL_INTERVENTION: {MigrationState.ROLLING_BACK},
    MigrationState.FINALIZED: set(),
}


class MigrationStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationRun:
    run_id: str
    state: MigrationState
    original_packages: tuple[str, ...]
    source_commit: str
    artifacts: dict[str, object]
    checksums: dict[str, object]
    validations: dict[str, object]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MigrationEvent:
    sequence: int
    run_id: str
    previous_state: MigrationState | None
    state: MigrationState
    recorded_at: datetime


class MigrationStateStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def create_run(
        self,
        run_id: str,
        *,
        original_packages: tuple[str, ...],
        source_commit: str,
        validations: dict[str, object] | None = None,
        artifacts: dict[str, object] | None = None,
        checksums: dict[str, object] | None = None,
    ) -> MigrationRun:
        async with self._lock:
            return await asyncio.to_thread(
                self._create_run_sync,
                run_id,
                original_packages=original_packages,
                source_commit=source_commit,
                validations=validations or {},
                artifacts=artifacts or {},
                checksums=checksums or {},
            )

    async def transition(
        self,
        run_id: str,
        expected_state: MigrationState,
        state: MigrationState,
        *,
        artifacts: dict[str, object] | None = None,
        checksums: dict[str, object] | None = None,
        validations: dict[str, object] | None = None,
    ) -> MigrationRun:
        async with self._lock:
            return await asyncio.to_thread(
                self._transition_sync,
                run_id,
                expected_state,
                state,
                artifacts=artifacts or {},
                checksums=checksums or {},
                validations=validations or {},
            )

    async def latest_run(self) -> MigrationRun | None:
        async with self._lock:
            return await asyncio.to_thread(self._latest_run_sync)

    async def events(self, run_id: str) -> tuple[MigrationEvent, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._events_sync, run_id)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1}:
                raise MigrationStateError(
                    f"unsupported migration state database version {version}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS migration_runs (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    original_packages_json TEXT NOT NULL,
                    source_commit TEXT NOT NULL,
                    artifacts_json TEXT NOT NULL,
                    checksums_json TEXT NOT NULL,
                    validations_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS migration_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    previous_state TEXT,
                    state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES migration_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS migration_events_run_sequence
                    ON migration_events(run_id, sequence);

                PRAGMA user_version = 1;
                """
            )

    def _create_run_sync(
        self,
        run_id: str,
        *,
        original_packages: tuple[str, ...],
        source_commit: str,
        validations: dict[str, object],
        artifacts: dict[str, object],
        checksums: dict[str, object],
    ) -> MigrationRun:
        if not run_id or not source_commit or not original_packages:
            raise MigrationStateError(
                "run ID, source commit, and original package list are required"
            )
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                """
                SELECT state
                FROM migration_runs
                ORDER BY rowid DESC
                LIMIT 1
                """
            ).fetchone()
            if latest is not None and MigrationState(latest["state"]) is not MigrationState.ROLLED_BACK:
                raise MigrationStateError("a migration run already exists")
            try:
                connection.execute(
                    """
                    INSERT INTO migration_runs (
                        run_id,
                        state,
                        original_packages_json,
                        source_commit,
                        artifacts_json,
                        checksums_json,
                        validations_json,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        MigrationState.PLANNED.value,
                        _dump(original_packages),
                        source_commit,
                        _dump(artifacts),
                        _dump(checksums),
                        _dump(validations),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise MigrationStateError(f"migration run {run_id} already exists") from error
            connection.execute(
                """
                INSERT INTO migration_events (
                    run_id, previous_state, state, recorded_at
                ) VALUES (?, NULL, ?, ?)
                """,
                (run_id, MigrationState.PLANNED.value, now),
            )
            row = connection.execute(
                "SELECT * FROM migration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _run_from_row(row)

    def _transition_sync(
        self,
        run_id: str,
        expected_state: MigrationState,
        state: MigrationState,
        *,
        artifacts: dict[str, object],
        checksums: dict[str, object],
        validations: dict[str, object],
    ) -> MigrationRun:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM migration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise MigrationStateError(f"migration run {run_id} does not exist")
            current = MigrationState(row["state"])
            if current is not expected_state:
                raise MigrationStateError(
                    f"migration run {run_id} is {current.value}, expected {expected_state.value}"
                )
            if not _transition_allowed(current, state):
                raise MigrationStateError(
                    f"migration cannot transition from {current.value} to {state.value}"
                )
            merged_artifacts = _load_object(row["artifacts_json"])
            merged_checksums = _load_object(row["checksums_json"])
            merged_validations = _load_object(row["validations_json"])
            merged_artifacts.update(artifacts)
            merged_checksums.update(checksums)
            merged_validations.update(validations)
            now = _now()
            updated = connection.execute(
                """
                UPDATE migration_runs
                SET state = ?,
                    artifacts_json = ?,
                    checksums_json = ?,
                    validations_json = ?,
                    updated_at = ?
                WHERE run_id = ? AND state = ?
                """,
                (
                    state.value,
                    _dump(merged_artifacts),
                    _dump(merged_checksums),
                    _dump(merged_validations),
                    now,
                    run_id,
                    expected_state.value,
                ),
            )
            if updated.rowcount != 1:
                raise MigrationStateError(
                    f"migration run {run_id} changed during transition"
                )
            connection.execute(
                """
                INSERT INTO migration_events (
                    run_id, previous_state, state, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, current.value, state.value, now),
            )
            new_row = connection.execute(
                "SELECT * FROM migration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _run_from_row(new_row)

    def _latest_run_sync(self) -> MigrationRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM migration_runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _run_from_row(row)

    def _events_sync(self, run_id: str) -> tuple[MigrationEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, run_id, previous_state, state, recorded_at
                FROM migration_events
                WHERE run_id = ?
                ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return tuple(_event_from_row(row) for row in rows)


def _transition_allowed(current: MigrationState, state: MigrationState) -> bool:
    if state is MigrationState.MANUAL_INTERVENTION:
        return current not in {
            MigrationState.ROLLED_BACK,
            MigrationState.FINALIZED,
            MigrationState.MANUAL_INTERVENTION,
        }
    return state in _TRANSITIONS[current]


def _run_from_row(row: sqlite3.Row) -> MigrationRun:
    return MigrationRun(
        run_id=str(row["run_id"]),
        state=MigrationState(row["state"]),
        original_packages=tuple(json.loads(row["original_packages_json"])),
        source_commit=str(row["source_commit"]),
        artifacts=_load_object(row["artifacts_json"]),
        checksums=_load_object(row["checksums_json"]),
        validations=_load_object(row["validations_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> MigrationEvent:
    previous_state = row["previous_state"]
    return MigrationEvent(
        sequence=int(row["sequence"]),
        run_id=str(row["run_id"]),
        previous_state=(
            None if previous_state is None else MigrationState(previous_state)
        ),
        state=MigrationState(row["state"]),
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
    )


def _load_object(value: str) -> dict[str, object]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise MigrationStateError("migration state JSON must be an object")
    return loaded


def _dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
