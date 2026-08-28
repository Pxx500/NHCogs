from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ..storage import apply_migrations, connect
from .models import (
    MigrationRun,
    ProjectedAction,
    StoredObservation,
    SynchronizationState,
)

MIGRATION_STEP_COLUMNS = {
    "red_modlog": "red_modlog_complete",
    "audit_ban": "audit_ban_complete",
    "audit_unban": "audit_unban_complete",
    "ban_snapshot": "ban_snapshot_complete",
}


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _required_datetime(value: str | None, label: str) -> datetime:
    parsed = _datetime(value)
    if parsed is None:
        raise RuntimeError(f"{label} timestamp is missing")
    return parsed


def _migration_1(connection: sqlite3.Connection) -> None:
    schema = """
        CREATE TABLE moderation_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_key TEXT,
            action_hint TEXT NOT NULL,
            target_user_id INTEGER,
            executor_user_id INTEGER,
            credited_moderator_hint INTEGER,
            attribution_hint TEXT,
            occurred_at TEXT,
            observed_at TEXT NOT NULL,
            reason TEXT,
            expiry_at TEXT,
            channel_id INTEGER,
            import_batch_id TEXT,
            source_payload_version INTEGER NOT NULL,
            UNIQUE (guild_id, source_kind, source_key)
        );
        CREATE INDEX moderation_observations_guild_sequence
            ON moderation_observations(guild_id, observation_id);

        CREATE TABLE moderation_actions (
            action_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            action_kind TEXT NOT NULL,
            action_variant TEXT,
            target_user_id INTEGER,
            credited_moderator_id INTEGER,
            attribution_kind TEXT NOT NULL,
            attribution_confidence TEXT NOT NULL,
            occurred_at TEXT,
            expiry_at TEXT,
            ended_at TEXT,
            reason TEXT,
            current_state TEXT,
            projection_version INTEGER NOT NULL
        );
        CREATE INDEX moderation_actions_chart
            ON moderation_actions(guild_id, action_kind, occurred_at, attribution_kind);

        CREATE TABLE moderation_action_observations (
            action_id INTEGER NOT NULL REFERENCES moderation_actions(action_id) ON DELETE CASCADE,
            observation_id INTEGER NOT NULL UNIQUE
                REFERENCES moderation_observations(observation_id) ON DELETE CASCADE,
            PRIMARY KEY (action_id, observation_id)
        );

        CREATE TABLE moderation_sync_state (
            guild_id INTEGER PRIMARY KEY,
            audit_ban_cursor INTEGER,
            audit_unban_cursor INTEGER,
            red_modlog_cursor INTEGER,
            last_sync_at TEXT,
            last_reconciliation_at TEXT,
            historical_gap INTEGER NOT NULL DEFAULT 0,
            migration_state TEXT NOT NULL DEFAULT 'pending',
            projection_checkpoint INTEGER NOT NULL DEFAULT 0,
            projection_version INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE moderation_migration_runs (
            run_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL UNIQUE,
            state TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            red_modlog_complete INTEGER NOT NULL DEFAULT 0,
            audit_ban_complete INTEGER NOT NULL DEFAULT 0,
            audit_unban_complete INTEGER NOT NULL DEFAULT 0,
            ban_snapshot_complete INTEGER NOT NULL DEFAULT 0,
            report TEXT
        );
        """
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)


MIGRATIONS = (_migration_1,)


class ModerationStore:
    def __init__(self, path: Path):
        self._path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        return connect(self._path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            apply_migrations(connection, MIGRATIONS, label="NHModeration")

    async def guilds_needing_projection(self, projection_version: int) -> tuple[int, ...]:
        return await asyncio.to_thread(
            self._guilds_needing_projection_sync,
            projection_version,
        )

    def _guilds_needing_projection_sync(
        self, projection_version: int
    ) -> tuple[int, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT observations.guild_id
                   FROM moderation_observations AS observations
                   LEFT JOIN moderation_sync_state AS state
                     ON state.guild_id = observations.guild_id
                   GROUP BY observations.guild_id
                   HAVING COALESCE(state.projection_version, 0) != ?
                      OR COALESCE(state.projection_checkpoint, 0)
                         != MAX(observations.observation_id)""",
                (projection_version,),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    async def start_migration(
        self, guild_id: int, run_id: str, started_at: datetime
    ) -> MigrationRun:
        return await asyncio.to_thread(
            self._start_migration_sync, guild_id, run_id, started_at
        )

    def _start_migration_sync(
        self, guild_id: int, run_id: str, started_at: datetime
    ) -> MigrationRun:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO moderation_migration_runs
                   (run_id, guild_id, state, started_at)
                   VALUES (?, ?, 'running', ?)""",
                (run_id, guild_id, _timestamp(started_at)),
            )
            connection.execute(
                """INSERT INTO moderation_sync_state(guild_id, migration_state)
                   VALUES (?, 'running')
                   ON CONFLICT(guild_id) DO UPDATE SET
                     migration_state = CASE
                       WHEN moderation_sync_state.migration_state = 'complete'
                         THEN 'complete' ELSE 'running' END""",
                (guild_id,),
            )
            row = connection.execute(
                "SELECT * FROM moderation_migration_runs WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return self._migration_run_from_row(row)

    async def migration_run(self, guild_id: int) -> MigrationRun | None:
        return await asyncio.to_thread(self._migration_run_sync, guild_id)

    def _migration_run_sync(self, guild_id: int) -> MigrationRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM moderation_migration_runs WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return self._migration_run_from_row(row) if row is not None else None

    @staticmethod
    def _migration_run_from_row(row: sqlite3.Row) -> MigrationRun:
        completed_steps = frozenset(
            step
            for step, column in MIGRATION_STEP_COLUMNS.items()
            if bool(row[column])
        )
        return MigrationRun(
            run_id=row["run_id"],
            guild_id=row["guild_id"],
            state=row["state"],
            started_at=_required_datetime(row["started_at"], "Migration start"),
            completed_at=_datetime(row["completed_at"]),
            completed_steps=completed_steps,
            report=row["report"],
        )

    async def mark_migration_step(
        self, guild_id: int, run_id: str, step: str
    ) -> MigrationRun:
        return await asyncio.to_thread(
            self._mark_migration_step_sync, guild_id, run_id, step
        )

    def _mark_migration_step_sync(
        self, guild_id: int, run_id: str, step: str
    ) -> MigrationRun:
        column = MIGRATION_STEP_COLUMNS.get(step)
        if column is None:
            raise ValueError(f"Unknown migration step: {step}")
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"UPDATE moderation_migration_runs SET {column} = 1 "
                "WHERE guild_id = ? AND run_id = ? AND state = 'running'",
                (guild_id, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Active moderation migration run was not found")
            row = connection.execute(
                "SELECT * FROM moderation_migration_runs WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return self._migration_run_from_row(row)

    async def complete_migration(
        self,
        guild_id: int,
        run_id: str,
        completed_at: datetime,
        report: str,
        historical_gap: bool,
    ) -> None:
        await asyncio.to_thread(
            self._complete_migration_sync,
            guild_id,
            run_id,
            completed_at,
            report,
            historical_gap,
        )

    def _complete_migration_sync(
        self,
        guild_id: int,
        run_id: str,
        completed_at: datetime,
        report: str,
        historical_gap: bool,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE moderation_migration_runs
                   SET state = 'complete', completed_at = ?, report = ?
                   WHERE guild_id = ? AND run_id = ?
                     AND red_modlog_complete = 1
                     AND audit_ban_complete = 1
                     AND audit_unban_complete = 1
                     AND ban_snapshot_complete = 1""",
                (_timestamp(completed_at), report, guild_id, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Moderation migration has incomplete source steps")
            connection.execute(
                """INSERT INTO moderation_sync_state
                   (guild_id, migration_state, last_sync_at, historical_gap)
                   VALUES (?, 'complete', ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                      migration_state = 'complete',
                      last_sync_at = excluded.last_sync_at,
                      historical_gap = MAX(
                        moderation_sync_state.historical_gap,
                        excluded.historical_gap
                      )""",
                (guild_id, _timestamp(completed_at), int(historical_gap)),
            )

    async def append(self, item: StoredObservation) -> bool:
        return await asyncio.to_thread(self._append_sync, item)

    def _append_sync(self, item: StoredObservation) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO moderation_observations
                   (guild_id, source_kind, source_key, action_hint, target_user_id,
                    executor_user_id, credited_moderator_hint, attribution_hint,
                    occurred_at, observed_at, reason, expiry_at, channel_id,
                    import_batch_id, source_payload_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.guild_id,
                    item.source_kind,
                    item.source_key,
                    item.action_hint,
                    item.target_user_id,
                    item.executor_user_id,
                    item.credited_moderator_hint,
                    item.attribution_hint,
                    _timestamp(item.occurred_at),
                    _timestamp(item.observed_at),
                    item.reason,
                    _timestamp(item.expiry_at),
                    item.channel_id,
                    item.import_batch_id,
                    item.source_payload_version,
                ),
            )
            return cursor.rowcount > 0

    async def append_batch(
        self,
        items: list[StoredObservation],
    ) -> int:
        return await asyncio.to_thread(
            self._append_batch_sync,
            items=items,
        )

    def _append_batch_sync(
        self,
        *,
        items: list[StoredObservation],
    ) -> int:
        inserted = 0
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in items:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO moderation_observations
                       (guild_id, source_kind, source_key, action_hint, target_user_id,
                        executor_user_id, credited_moderator_hint, attribution_hint,
                        occurred_at, observed_at, reason, expiry_at, channel_id,
                        import_batch_id, source_payload_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item.guild_id,
                        item.source_kind,
                        item.source_key,
                        item.action_hint,
                        item.target_user_id,
                        item.executor_user_id,
                        item.credited_moderator_hint,
                        item.attribution_hint,
                        _timestamp(item.occurred_at),
                        _timestamp(item.observed_at),
                        item.reason,
                        _timestamp(item.expiry_at),
                        item.channel_id,
                        item.import_batch_id,
                        item.source_payload_version,
                    ),
                )
                inserted += int(cursor.rowcount > 0)
        return inserted

    async def update_sync_state(
        self,
        guild_id: int,
        *,
        audit_ban_cursor: int | None,
        audit_unban_cursor: int | None,
        red_modlog_cursor: int | None,
        completed_at: datetime | None,
        reconciliation: bool,
        migration_complete: bool,
        historical_gap: bool | None,
    ) -> None:
        await asyncio.to_thread(
            self._update_sync_state_sync,
            guild_id,
            audit_ban_cursor=audit_ban_cursor,
            audit_unban_cursor=audit_unban_cursor,
            red_modlog_cursor=red_modlog_cursor,
            completed_at=completed_at,
            reconciliation=reconciliation,
            migration_complete=migration_complete,
            historical_gap=historical_gap,
        )

    def _update_sync_state_sync(
        self,
        guild_id: int,
        *,
        audit_ban_cursor: int | None,
        audit_unban_cursor: int | None,
        red_modlog_cursor: int | None,
        completed_at: datetime | None,
        reconciliation: bool,
        migration_complete: bool,
        historical_gap: bool | None,
    ) -> None:
        timestamp = _timestamp(completed_at)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO moderation_sync_state
                   (guild_id, audit_ban_cursor, audit_unban_cursor, red_modlog_cursor,
                     last_sync_at, last_reconciliation_at, historical_gap,
                     migration_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                     audit_ban_cursor = COALESCE(
                       excluded.audit_ban_cursor,
                       moderation_sync_state.audit_ban_cursor
                     ),
                     audit_unban_cursor = COALESCE(
                       excluded.audit_unban_cursor,
                       moderation_sync_state.audit_unban_cursor
                     ),
                     red_modlog_cursor = COALESCE(
                       excluded.red_modlog_cursor,
                       moderation_sync_state.red_modlog_cursor
                     ),
                     last_sync_at = COALESCE(
                       excluded.last_sync_at,
                       moderation_sync_state.last_sync_at
                     ),
                      last_reconciliation_at = COALESCE(
                        excluded.last_reconciliation_at,
                        moderation_sync_state.last_reconciliation_at
                      ),
                      historical_gap = MAX(
                        moderation_sync_state.historical_gap,
                        excluded.historical_gap
                      ),
                      migration_state = CASE
                       WHEN excluded.migration_state = 'complete' THEN 'complete'
                       ELSE moderation_sync_state.migration_state END""",
                (
                    guild_id,
                    audit_ban_cursor,
                    audit_unban_cursor,
                    red_modlog_cursor,
                    timestamp,
                    timestamp if reconciliation else None,
                    int(bool(historical_gap)),
                    "complete" if migration_complete else "pending",
                ),
            )

    async def observations(self, guild_id: int) -> list[StoredObservation]:
        return await asyncio.to_thread(self._observations_sync, guild_id)

    def _observations_sync(self, guild_id: int) -> list[StoredObservation]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM moderation_observations WHERE guild_id = ? ORDER BY observation_id",
                (guild_id,),
            ).fetchall()
        return [
            StoredObservation(
                observation_id=row["observation_id"],
                guild_id=row["guild_id"],
                source_kind=row["source_kind"],
                source_key=row["source_key"],
                action_hint=row["action_hint"],
                target_user_id=row["target_user_id"],
                executor_user_id=row["executor_user_id"],
                credited_moderator_hint=row["credited_moderator_hint"],
                attribution_hint=row["attribution_hint"],
                occurred_at=_datetime(row["occurred_at"]),
                observed_at=_required_datetime(row["observed_at"], "observation"),
                reason=row["reason"],
                expiry_at=_datetime(row["expiry_at"]),
                channel_id=row["channel_id"],
                import_batch_id=row["import_batch_id"],
                source_payload_version=row["source_payload_version"],
            )
            for row in rows
        ]

    async def replace_projection(
        self, guild_id: int, actions: list[ProjectedAction], projection_version: int
    ) -> None:
        await asyncio.to_thread(
            self._replace_projection_sync, guild_id, actions, projection_version
        )

    def _replace_projection_sync(
        self, guild_id: int, actions: list[ProjectedAction], projection_version: int
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """DELETE FROM moderation_action_observations
                   WHERE observation_id IN (
                     SELECT observation_id FROM moderation_observations
                     WHERE guild_id = ?
                   )""",
                (guild_id,),
            )
            connection.execute(
                "DELETE FROM moderation_actions WHERE guild_id = ?", (guild_id,)
            )
            for action in actions:
                cursor = connection.execute(
                    """INSERT INTO moderation_actions
                       (guild_id, action_kind, action_variant, target_user_id,
                        credited_moderator_id, attribution_kind, attribution_confidence,
                        occurred_at, expiry_at, ended_at, reason, current_state,
                        projection_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        action.action_kind,
                        action.action_variant,
                        action.target_user_id,
                        action.moderator_user_id,
                        action.attribution_kind,
                        action.attribution_confidence,
                        _timestamp(action.occurred_at),
                        _timestamp(action.expiry_at),
                        _timestamp(action.ended_at),
                        action.reason,
                        action.current_state,
                        projection_version,
                    ),
                )
                connection.executemany(
                    """INSERT INTO moderation_action_observations
                       (action_id, observation_id) VALUES (?, ?)""",
                    ((cursor.lastrowid, observation_id) for observation_id in action.observation_ids),
                )
            checkpoint = connection.execute(
                """SELECT COALESCE(MAX(observation_id), 0) FROM moderation_observations
                   WHERE guild_id = ?""",
                (guild_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO moderation_sync_state
                   (guild_id, projection_checkpoint, projection_version)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                     projection_checkpoint = excluded.projection_checkpoint,
                     projection_version = excluded.projection_version""",
                (guild_id, checkpoint, projection_version),
            )

    async def chart_rows(
        self, guild_id: int, since: datetime | None, include_automation: bool
    ) -> list[sqlite3.Row]:
        return await asyncio.to_thread(
            self._chart_rows_sync, guild_id, since, include_automation
        )

    def _chart_rows_sync(
        self, guild_id: int, since: datetime | None, include_automation: bool
    ) -> list[sqlite3.Row]:
        clauses = [
            "guild_id = ?",
            "action_kind IN ('ban', 'tempban')",
            "occurred_at IS NOT NULL",
        ]
        params: list[object] = [guild_id]
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(_timestamp(since))
        if not include_automation:
            clauses.append("attribution_kind != 'automation'")
        with closing(self._connect()) as connection:
            return connection.execute(
                f"""SELECT credited_moderator_id, attribution_kind, COUNT(*) AS count
                    FROM moderation_actions WHERE {' AND '.join(clauses)}
                    GROUP BY credited_moderator_id, attribution_kind""",
                params,
            ).fetchall()

    async def delete_user(self, user_id: int) -> set[int]:
        return await asyncio.to_thread(self._delete_user_sync, user_id)

    def _delete_user_sync(self, user_id: int) -> set[int]:
        with closing(self._connect()) as connection, connection:
            guild_ids = {
                row[0]
                for row in connection.execute(
                    """SELECT DISTINCT guild_id FROM moderation_observations
                       WHERE target_user_id = ? OR executor_user_id = ?
                          OR credited_moderator_hint = ?""",
                    (user_id, user_id, user_id),
                )
            }
            connection.execute(
                """UPDATE moderation_observations SET
                     source_key = CASE
                       WHEN source_kind = 'discord_ban_snapshot' AND target_user_id = ?
                         THEN 'deleted:' || observation_id ELSE source_key END,
                     target_user_id = CASE WHEN target_user_id = ? THEN NULL ELSE target_user_id END,
                     executor_user_id = CASE WHEN executor_user_id = ? THEN NULL ELSE executor_user_id END,
                     credited_moderator_hint = CASE
                       WHEN credited_moderator_hint = ? THEN NULL ELSE credited_moderator_hint END,
                     attribution_hint = CASE
                       WHEN credited_moderator_hint = ? THEN NULL ELSE attribution_hint END,
                     reason = CASE
                       WHEN target_user_id = ? OR executor_user_id = ?
                         OR credited_moderator_hint = ? THEN NULL ELSE reason END
                   WHERE target_user_id = ? OR executor_user_id = ?
                      OR credited_moderator_hint = ?""",
                (
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                    user_id,
                ),
            )
        return guild_ids

    async def delete_guild(self, guild_id: int) -> None:
        await asyncio.to_thread(self._delete_guild_sync, guild_id)

    def _delete_guild_sync(self, guild_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM moderation_actions WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM moderation_observations WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM moderation_sync_state WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM moderation_migration_runs WHERE guild_id = ?", (guild_id,))

    async def sync_state(self, guild_id: int) -> SynchronizationState:
        return await asyncio.to_thread(self._sync_state_sync, guild_id)

    def _sync_state_sync(self, guild_id: int) -> SynchronizationState:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO moderation_sync_state(guild_id) VALUES (?)",
                (guild_id,),
            )
            row = connection.execute(
                "SELECT * FROM moderation_sync_state WHERE guild_id = ?", (guild_id,)
            ).fetchone()
        return SynchronizationState(
            guild_id=guild_id,
            audit_ban_cursor=row["audit_ban_cursor"],
            audit_unban_cursor=row["audit_unban_cursor"],
            red_modlog_cursor=row["red_modlog_cursor"],
            last_sync_at=_datetime(row["last_sync_at"]),
            last_reconciliation_at=_datetime(row["last_reconciliation_at"]),
            historical_gap=bool(row["historical_gap"]),
            migration_state=row["migration_state"],
            projection_checkpoint=row["projection_checkpoint"],
            projection_version=row["projection_version"],
        )
