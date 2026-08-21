"""Durable index of Discord messages observed by Honeypot."""

import asyncio
import sqlite3
from collections.abc import Collection
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .storage import Migrations, apply_migrations, connect


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE observed_messages (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            created_at_utc INTEGER NOT NULL,
            pinned INTEGER NOT NULL CHECK (pinned IN (0, 1)),
            author_kind TEXT NOT NULL
                CHECK (author_kind IN ('member', 'bot', 'webhook')),
            fingerprint TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_observed_messages_channel
        ON observed_messages (guild_id, channel_id, message_id DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_observed_messages_author
        ON observed_messages (guild_id, author_id, message_id DESC)
        """
    )


MIGRATIONS: Migrations = (_create_schema,)


@dataclass(frozen=True, slots=True)
class MessageRecord:
    message_id: int
    guild_id: int
    channel_id: int
    author_id: int
    created_at: datetime
    pinned: bool
    author_kind: str
    fingerprint: str | None = None


def _to_timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    return int(value.timestamp() * 1_000_000)


def _record_from_row(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        message_id=row["message_id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        author_id=row["author_id"],
        created_at=datetime.fromtimestamp(
            row["created_at_utc"] / 1_000_000,
            tz=timezone.utc,
        ),
        pinned=bool(row["pinned"]),
        author_kind=row["author_kind"],
        fingerprint=row["fingerprint"],
    )


class MessageRegistry:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.database_path)) as connection:
            apply_migrations(connection, MIGRATIONS, label="message registry")

    async def observe(self, record: MessageRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._observe_sync, record)

    def _observe_sync(self, record: MessageRecord) -> None:
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO observed_messages (
                    message_id,
                    guild_id,
                    channel_id,
                    author_id,
                    created_at_utc,
                    pinned,
                    author_kind,
                    fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.message_id,
                    record.guild_id,
                    record.channel_id,
                    record.author_id,
                    _to_timestamp(record.created_at),
                    int(record.pinned),
                    record.author_kind,
                    record.fingerprint,
                ),
            )
            connection.commit()

    async def recent_by_author(
        self,
        guild_id: int,
        author_id: int,
        *,
        limit: int | None = None,
        since_utc: datetime | None = None,
        exclude_message_id: int | None = None,
        exclude_pinned: bool = True,
    ) -> tuple[MessageRecord, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_by_author_sync,
                guild_id,
                author_id,
                limit,
                since_utc,
                exclude_message_id,
                exclude_pinned,
            )

    def _recent_by_author_sync(  # noqa: PLR0917 - mirrors the public query contract
        self,
        guild_id: int,
        author_id: int,
        limit: int | None,
        since_utc: datetime | None,
        exclude_message_id: int | None,
        exclude_pinned: bool,
    ) -> tuple[MessageRecord, ...]:
        conditions = ["guild_id = ?", "author_id = ?"]
        parameters: list[int] = [guild_id, author_id]
        if since_utc is not None:
            conditions.append("created_at_utc >= ?")
            parameters.append(_to_timestamp(since_utc))
        if exclude_message_id is not None:
            conditions.append("message_id != ?")
            parameters.append(exclude_message_id)
        if exclude_pinned:
            conditions.append("pinned = 0")
        query = f"""
            SELECT *
            FROM observed_messages
            WHERE {' AND '.join(conditions)}
            ORDER BY message_id DESC
        """
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    async def recent_in_channel(
        self,
        guild_id: int,
        channel_id: int,
        *,
        limit: int,
        before_message_id: int,
        exclude_pinned: bool = True,
    ) -> tuple[MessageRecord, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_in_channel_sync,
                guild_id,
                channel_id,
                limit,
                before_message_id,
                exclude_pinned,
            )

    def _recent_in_channel_sync(
        self,
        guild_id: int,
        channel_id: int,
        limit: int,
        before_message_id: int,
        exclude_pinned: bool,
    ) -> tuple[MessageRecord, ...]:
        pinned_filter = " AND pinned = 0" if exclude_pinned else ""
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM observed_messages
                WHERE guild_id = ?
                  AND channel_id = ?
                  AND message_id < ?
                  {pinned_filter}
                ORDER BY message_id DESC
                LIMIT ?
                """,
                (guild_id, channel_id, before_message_id, limit),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    async def matching_channel_count(
        self,
        guild_id: int,
        author_id: int,
        fingerprint: str,
        *,
        since_utc: datetime,
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._matching_channel_count_sync,
                guild_id,
                author_id,
                fingerprint,
                since_utc,
            )

    def _matching_channel_count_sync(
        self,
        guild_id: int,
        author_id: int,
        fingerprint: str,
        since_utc: datetime,
    ) -> int:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT channel_id)
                FROM observed_messages
                WHERE guild_id = ?
                  AND author_id = ?
                  AND fingerprint = ?
                  AND created_at_utc >= ?
                """,
                (guild_id, author_id, fingerprint, _to_timestamp(since_utc)),
            ).fetchone()
        return int(row[0])

    async def set_pinned(self, message_id: int, pinned: bool) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_pinned_sync, message_id, pinned)

    def _set_pinned_sync(self, message_id: int, pinned: bool) -> None:
        with closing(connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE observed_messages SET pinned = ? WHERE message_id = ?",
                (int(pinned), message_id),
            )
            connection.commit()

    async def forget(self, message_id: int) -> None:
        await self._delete_where("message_id = ?", (message_id,))

    async def forget_many(self, message_ids: Collection[int]) -> None:
        ids = tuple(message_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        await self._delete_where(f"message_id IN ({placeholders})", ids)

    async def forget_channel(self, guild_id: int, channel_id: int) -> None:
        await self._delete_where(
            "guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )

    async def forget_user(self, user_id: int) -> None:
        await self._delete_where("author_id = ?", (user_id,))

    async def forget_guild(self, guild_id: int) -> None:
        await self._delete_where("guild_id = ?", (guild_id,))

    async def _delete_where(
        self,
        condition: str,
        parameters: tuple[int, ...],
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_where_sync,
                condition,
                parameters,
            )

    def _delete_where_sync(
        self,
        condition: str,
        parameters: tuple[int, ...],
    ) -> int:
        with closing(connect(self.database_path)) as connection:
            cursor = connection.execute(
                f"DELETE FROM observed_messages WHERE {condition}",
                parameters,
            )
            connection.commit()
            return cursor.rowcount

    async def prune(self, before_utc: datetime) -> int:
        return await self._delete_where(
            "created_at_utc < ?",
            (_to_timestamp(before_utc),),
        )
