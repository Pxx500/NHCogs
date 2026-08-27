from __future__ import annotations

import asyncio
import hashlib
import io
import sqlite3
import traceback
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

MAX_SUMMARY_LENGTH = 1_000


@dataclass(frozen=True)
class OperationalFailure:
    guild_id: int
    fingerprint: str
    source: str
    action: str
    summary: str
    exception_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrences: int
    recovered_at: datetime | None
    channel_id: int | None
    thread_id: int | None
    message_id: int | None


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class OperationalErrorReporter:
    """Persist NH operational failures and publish private Discord alerts."""

    def __init__(self, bot: Any, config: Any, database_path: Path, *, logger: Any):
        self._bot = bot
        self._config = config
        self._database_path = Path(database_path)
        self._logger = logger

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
                """CREATE TABLE IF NOT EXISTS operational_failures (
                    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    source TEXT NOT NULL,
                    action TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    exception_type TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrences INTEGER NOT NULL,
                    recovered_at TEXT,
                    channel_id INTEGER,
                    thread_id INTEGER,
                    message_id INTEGER
                )"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS operational_failures_open
                   ON operational_failures(guild_id, fingerprint)
                   WHERE recovered_at IS NULL"""
            )

    @staticmethod
    def _fingerprint(
        *, source: str, action: str, summary: str, exception_type: str
    ) -> str:
        payload = "\n".join((source, action, exception_type, summary))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OperationalFailure:
        first_seen_at = _datetime(row["first_seen_at"])
        last_seen_at = _datetime(row["last_seen_at"])
        if first_seen_at is None or last_seen_at is None:
            raise RuntimeError("operational failure timestamps are missing")
        return OperationalFailure(
            guild_id=row["guild_id"],
            fingerprint=row["fingerprint"],
            source=row["source"],
            action=row["action"],
            summary=row["summary"],
            exception_type=row["exception_type"],
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            occurrences=row["occurrences"],
            recovered_at=_datetime(row["recovered_at"]),
            channel_id=row["channel_id"],
            thread_id=row["thread_id"],
            message_id=row["message_id"],
        )

    def _record_sync(
        self,
        *,
        guild_id: int,
        fingerprint: str,
        source: str,
        action: str,
        summary: str,
        exception_type: str,
        occurred_at: datetime,
        channel_id: int | None,
        thread_id: int | None,
        message_id: int | None,
    ) -> OperationalFailure:
        timestamp = _timestamp(occurred_at)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT failure_id FROM operational_failures
                   WHERE guild_id = ? AND fingerprint = ? AND recovered_at IS NULL""",
                (guild_id, fingerprint),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """INSERT INTO operational_failures
                       (guild_id, fingerprint, source, action, summary, exception_type,
                        first_seen_at, last_seen_at, occurrences, channel_id,
                        thread_id, message_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (
                        guild_id,
                        fingerprint,
                        source,
                        action,
                        summary,
                        exception_type,
                        timestamp,
                        timestamp,
                        channel_id,
                        thread_id,
                        message_id,
                    ),
                )
                failure_id = cursor.lastrowid
            else:
                failure_id = row["failure_id"]
                connection.execute(
                    """UPDATE operational_failures
                       SET summary = ?, last_seen_at = ?, occurrences = occurrences + 1,
                           channel_id = ?, thread_id = ?, message_id = ?
                       WHERE failure_id = ?""",
                    (
                        summary,
                        timestamp,
                        channel_id,
                        thread_id,
                        message_id,
                        failure_id,
                    ),
                )
            stored = connection.execute(
                "SELECT * FROM operational_failures WHERE failure_id = ?",
                (failure_id,),
            ).fetchone()
        if stored is None:
            raise RuntimeError("operational failure write could not be read back")
        return self._from_row(stored)

    async def report(
        self,
        *,
        guild_id: int,
        source: str,
        action: str,
        error: BaseException,
        channel_id: int | None = None,
        thread_id: int | None = None,
        message_id: int | None = None,
    ) -> OperationalFailure:
        exception_type = type(error).__name__
        summary = (str(error).strip() or exception_type)[:MAX_SUMMARY_LENGTH]
        fingerprint = self._fingerprint(
            source=source,
            action=action,
            summary=summary,
            exception_type=exception_type,
        )
        failure = await asyncio.to_thread(
            self._record_sync,
            guild_id=guild_id,
            fingerprint=fingerprint,
            source=source,
            action=action,
            summary=summary,
            exception_type=exception_type,
            occurred_at=datetime.now(timezone.utc),
            channel_id=channel_id,
            thread_id=thread_id,
            message_id=message_id,
        )
        trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        try:
            await self._publish_alert(failure, trace)
        except Exception:
            self._logger.exception(
                "Failed to publish NH operational error alert for guild %s",
                guild_id,
            )
        return failure

    async def _publish_alert(self, failure: OperationalFailure, trace: str) -> None:
        guild = self._bot.get_guild(failure.guild_id)
        if guild is None:
            self._logger.error(
                "Cannot publish NH operational error because guild %s is unavailable",
                failure.guild_id,
            )
            return
        guild_config = self._config.guild_from_id(failure.guild_id)
        channel_id = await guild_config.error_channel()
        maintainer_id = await guild_config.error_maintainer_id()
        channel = guild.get_channel(channel_id) if channel_id is not None else None
        if channel is None:
            self._logger.error(
                "Cannot publish NH operational error because its channel is not configured"
            )
            return
        if channel.permissions_for(guild.default_role).view_channel:
            self._logger.error(
                "Cannot publish NH operational error because channel %s is public",
                channel.id,
            )
            return

        maintainer = guild.get_member(maintainer_id) if maintainer_id is not None else None
        mention = maintainer.mention if maintainer is not None else None
        mention_target = maintainer
        if maintainer_id is not None and mention_target is None:
            mention = f"<@{maintainer_id}>"
            mention_target = discord.Object(id=maintainer_id)
        lines = []
        if mention is not None:
            lines.append(mention)
        lines.extend(
            (
                f"**{failure.source} operational error**",
                f"Action: {failure.action}",
                f"Error: {failure.exception_type}: {failure.summary}",
                f"Occurrences: {failure.occurrences}",
            )
        )
        context = self._format_context(failure)
        if context is not None:
            lines.append(f"Context: {context}")
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=[mention_target] if mention_target is not None else False,
            roles=False,
            replied_user=False,
        )
        payload = trace or f"{failure.exception_type}: {failure.summary}\n"
        await channel.send(
            "\n".join(lines),
            file=discord.File(
                io.BytesIO(payload.encode("utf-8")),
                filename=f"nh-error-{failure.fingerprint[:12]}.txt",
            ),
            allowed_mentions=allowed_mentions,
        )

    @staticmethod
    def _format_context(failure: OperationalFailure) -> str | None:
        context_channel_id = failure.thread_id or failure.channel_id
        if context_channel_id is None:
            return None
        channel = f"<#{context_channel_id}>"
        if failure.message_id is None:
            return channel
        return (
            f"{channel} "
            f"https://discord.com/channels/{failure.guild_id}/"
            f"{context_channel_id}/{failure.message_id}"
        )

    async def mark_recovered(self, *, guild_id: int, fingerprint: str) -> bool:
        return await asyncio.to_thread(
            self._mark_recovered_sync,
            guild_id=guild_id,
            fingerprint=fingerprint,
            recovered_at=datetime.now(timezone.utc),
        )

    async def active_count(self, guild_id: int) -> int:
        return await asyncio.to_thread(self._active_count_sync, guild_id)

    async def delete_guild(self, guild_id: int) -> None:
        await asyncio.to_thread(self._delete_guild_sync, guild_id)

    def _delete_guild_sync(self, guild_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM operational_failures WHERE guild_id = ?", (guild_id,)
            )

    def _active_count_sync(self, guild_id: int) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM operational_failures
                   WHERE guild_id = ? AND recovered_at IS NULL""",
                (guild_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _mark_recovered_sync(
        self,
        *,
        guild_id: int,
        fingerprint: str,
        recovered_at: datetime,
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE operational_failures SET recovered_at = ?
                   WHERE guild_id = ? AND fingerprint = ? AND recovered_at IS NULL""",
                (_timestamp(recovered_at), guild_id, fingerprint),
            )
            return cursor.rowcount > 0
