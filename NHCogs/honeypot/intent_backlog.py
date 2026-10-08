"""Retain observed message IDs until content access returns."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import discord

from ..gateway_capabilities import available

RETENTION_SECONDS = 14 * 24 * 60 * 60
RETRY_SECONDS = 30
DRAIN_BATCH_SIZE = 10
log = logging.getLogger("red.Honeypot")


@dataclass(frozen=True)
class PendingMessage:
    guild_id: int
    channel_id: int
    message_id: int
    enqueued_at: int
    attempts: int
    retry_at: int


class IntentBacklog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pending_content_messages (
                    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, enqueued_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, channel_id, message_id)
                )"""
            )

    async def enqueue(self, guild_id: int, channel_id: int, message_id: int, *, now=None) -> None:
        timestamp = int(time.time() if now is None else now)
        async with self._lock:
            await asyncio.to_thread(self._enqueue_sync, guild_id, channel_id, message_id, timestamp)

    def _enqueue_sync(self, guild_id: int, channel_id: int, message_id: int, now: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO pending_content_messages "
                "(guild_id, channel_id, message_id, enqueued_at, retry_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, channel_id, message_id, now, now),
            )

    async def due(self, *, now=None, limit=DRAIN_BATCH_SIZE) -> tuple[PendingMessage, ...]:
        timestamp = int(time.time() if now is None else now)
        async with self._lock:
            return await asyncio.to_thread(self._due_sync, timestamp, limit)

    def _due_sync(self, now: int, limit: int) -> tuple[PendingMessage, ...]:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM pending_content_messages WHERE enqueued_at <= ?",
                (now - RETENTION_SECONDS,),
            )
            rows = connection.execute(
                "SELECT * FROM pending_content_messages WHERE retry_at <= ? "
                "ORDER BY enqueued_at, message_id LIMIT ?", (now, limit),
            ).fetchall()
        return tuple(PendingMessage(**dict(row)) for row in rows)

    async def acknowledge(self, message: PendingMessage) -> None:
        async with self._lock:
            await asyncio.to_thread(self._acknowledge_sync, message)

    def _acknowledge_sync(self, message: PendingMessage) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM pending_content_messages WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
                (message.guild_id, message.channel_id, message.message_id),
            )

    async def retry(self, message: PendingMessage, *, now=None) -> None:
        timestamp = int(time.time() if now is None else now)
        async with self._lock:
            await asyncio.to_thread(self._retry_sync, message, timestamp)

    def _retry_sync(self, message: PendingMessage, now: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE pending_content_messages SET attempts = attempts + 1, retry_at = ? "
                "WHERE guild_id = ? AND channel_id = ? AND message_id = ?",
                (now + RETRY_SECONDS, message.guild_id, message.channel_id, message.message_id),
            )


async def queue_if_unavailable(cog, message) -> bool:
    if message.guild is None or message.author.bot or message.webhook_id is not None:
        return False
    if available(cog.bot, "message_content"):
        return False
    if await cog.bot.cog_disabled_in_guild(cog, message.guild):
        return True
    config = await cog.config.guild(message.guild).all()
    if config.get("enabled", False) or config.get("gif_detector_enabled", False):
        await cog._intent_backlog.enqueue(message.guild.id, message.channel.id, message.id)
    # Return even when both detectors are disabled, so redacted content isn't
    # accidentally observed as an empty-message fingerprint.
    return True


async def drain_once(cog) -> int:
    pending_messages = await cog._intent_backlog.due()
    if not available(cog.bot, "message_content"):
        return 0
    is_ready = getattr(cog.bot, "is_ready", None)
    if callable(is_ready) and not is_ready():
        return 0
    processed = 0
    for pending in pending_messages:
        if not available(cog.bot, "message_content"):
            break
        try:
            guild = cog.bot.get_guild(pending.guild_id)
            if guild is None:
                await cog._intent_backlog.retry(pending)
                continue
            channel = await cog._fetch_message_channel(guild, pending.channel_id)
            if channel is None:
                await cog._intent_backlog.retry(pending)
                continue
            message = await channel.fetch_message(pending.message_id)
            if not available(cog.bot, "message_content"):
                await cog._intent_backlog.retry(pending)
                break
            await cog.on_message(message)
            if not available(cog.bot, "message_content"):
                await cog._intent_backlog.retry(pending)
                break
        except discord.NotFound:
            await cog._intent_backlog.acknowledge(pending)
        except Exception:
            await cog._intent_backlog.retry(pending)
            log.warning("Deferred message processing failed for message %s", pending.message_id,
                        exc_info=True)
        else:
            await cog._intent_backlog.acknowledge(pending)
            processed += 1
    return processed


async def worker(cog) -> None:
    wait_until_ready = getattr(cog.bot, "wait_until_red_ready", None)
    if callable(wait_until_ready):
        await wait_until_ready()
    while True:
        try:
            await drain_once(cog)
        except Exception:
            log.warning("Could not drain the deferred message queue", exc_info=True)
        await asyncio.sleep(RETRY_SECONDS)
