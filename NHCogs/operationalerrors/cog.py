from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import sqlite3
import traceback
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .. import command_overview
from ..operational_errors import _log_report_failure

log = logging.getLogger("red.OperationalErrors")

MAX_SUMMARY_LENGTH = 1_000


@dataclass(frozen=True, slots=True)
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


class OperationalErrors(commands.Cog):
    """Persist operational failures and publish private Discord alerts."""

    CONFIG_IDENTIFIER = 208949585754543553992613466368209142183

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=self.CONFIG_IDENTIFIER,
            force_registration=True,
        )
        self.config.register_global(
            error_channel=None,
            error_maintainer_id=None,
        )
        self._database_path = cog_data_path(self) / "operational_errors.sqlite"

    async def cog_load(self) -> None:
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
    def _correlation_fingerprint(
        *, source: str, action: str, correlation_key: str
    ) -> str:
        payload = "\n".join((source, action, "correlation", correlation_key))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OperationalFailure:
        first_seen_at = _datetime(row["first_seen_at"])
        last_seen_at = _datetime(row["last_seen_at"])
        if first_seen_at is None or last_seen_at is None:
            raise RuntimeError("Operational failure timestamps are missing")
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
            raise RuntimeError("Operational failure write could not be read back")
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
        correlation_key: str | None = None,
    ) -> OperationalFailure | None:
        exception_type = type(error).__name__
        summary = (str(error).strip() or exception_type)[:MAX_SUMMARY_LENGTH]
        fingerprint = (
            self._correlation_fingerprint(
                source=source,
                action=action,
                correlation_key=correlation_key,
            )
            if correlation_key is not None
            else self._fingerprint(
                source=source,
                action=action,
                summary=summary,
                exception_type=exception_type,
            )
        )
        now = datetime.now(timezone.utc)
        failure: OperationalFailure | None = None
        try:
            failure = await asyncio.to_thread(
                self._record_sync,
                guild_id=guild_id,
                fingerprint=fingerprint,
                source=source,
                action=action,
                summary=summary,
                exception_type=exception_type,
                occurred_at=now,
                channel_id=channel_id,
                thread_id=thread_id,
                message_id=message_id,
            )
        except BaseException:
            _log_report_failure(
                "Failed to persist operational error for guild %s",
                guild_id,
            )

        alert_failure = failure or OperationalFailure(
            guild_id=guild_id,
            fingerprint=fingerprint,
            source=source,
            action=action,
            summary=summary,
            exception_type=exception_type,
            first_seen_at=now,
            last_seen_at=now,
            occurrences=1,
            recovered_at=None,
            channel_id=channel_id,
            thread_id=thread_id,
            message_id=message_id,
        )
        trace = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        try:
            await self._publish_alert(alert_failure, trace)
        except BaseException:
            _log_report_failure(
                "Failed to publish operational error alert for guild %s",
                guild_id,
            )
        return failure

    async def _publish_alert(
        self,
        failure: OperationalFailure,
        trace: str,
    ) -> None:
        channel_id = await self.config.error_channel()
        maintainer_id = await self.config.error_maintainer_id()
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        if channel is None:
            log.error(
                "Cannot publish operational error because its channel is not configured"
            )
            return
        guild = channel.guild
        if channel.permissions_for(guild.default_role).view_channel:
            log.error(
                "Cannot publish operational error because channel %s is public",
                channel.id,
            )
            return

        maintainer = guild.get_member(maintainer_id) if maintainer_id is not None else None
        mention = maintainer.mention if maintainer is not None else None
        mention_target: Any = maintainer
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

    async def mark_action_recovered(
        self,
        *,
        guild_id: int,
        source: str,
        action: str,
        correlation_key: str | None = None,
    ) -> int:
        try:
            return await asyncio.to_thread(
                self._mark_action_recovered_sync,
                guild_id=guild_id,
                source=source,
                action=action,
                fingerprint=(
                    self._correlation_fingerprint(
                        source=source,
                        action=action,
                        correlation_key=correlation_key,
                    )
                    if correlation_key is not None
                    else None
                ),
                recovered_at=datetime.now(timezone.utc),
            )
        except BaseException:
            _log_report_failure(
                "Failed to mark operational error recovered for guild %s",
                guild_id,
            )
            return 0

    def _mark_action_recovered_sync(
        self,
        *,
        guild_id: int,
        source: str,
        action: str,
        fingerprint: str | None,
        recovered_at: datetime,
    ) -> int:
        with closing(self._connect()) as connection, connection:
            if fingerprint is None:
                cursor = connection.execute(
                    """UPDATE operational_failures SET recovered_at = ?
                       WHERE guild_id = ? AND source = ? AND action = ?
                         AND recovered_at IS NULL""",
                    (_timestamp(recovered_at), guild_id, source, action),
                )
            else:
                cursor = connection.execute(
                    """UPDATE operational_failures SET recovered_at = ?
                       WHERE guild_id = ? AND source = ? AND action = ?
                         AND fingerprint = ? AND recovered_at IS NULL""",
                    (
                        _timestamp(recovered_at),
                        guild_id,
                        source,
                        action,
                        fingerprint,
                    ),
                )
            return cursor.rowcount

    async def active_count(self, guild_id: int) -> int:
        try:
            return await asyncio.to_thread(self._active_count_sync, guild_id)
        except BaseException:
            _log_report_failure(
                "Failed to count operational errors for guild %s",
                guild_id,
            )
            return 0

    def _active_count_sync(self, guild_id: int) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM operational_failures
                   WHERE guild_id = ? AND recovered_at IS NULL""",
                (guild_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    async def _delete_guild(self, guild_id: int) -> None:
        try:
            await asyncio.to_thread(self._delete_guild_sync, guild_id)
        except BaseException:
            _log_report_failure(
                "Failed to delete operational errors for guild %s",
                guild_id,
            )

    def _delete_guild_sync(self, guild_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM operational_failures WHERE guild_id = ?",
                (guild_id,),
            )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self._delete_guild(guild.id)
        channel_id = await self.config.error_channel()
        if channel_id is None or guild.get_channel(channel_id) is None:
            return
        await self.config.error_channel.clear()
        await self.config.error_maintainer_id.clear()

    async def red_delete_data_for_user(
        self,
        *,
        requester: str,
        user_id: int,
    ) -> None:
        del requester
        if await self.config.error_maintainer_id() == user_id:
            await self.config.error_maintainer_id.clear()

    async def _send_group_overview(
        self,
        ctx: commands.Context,
        *,
        include_descendants: bool = True,
    ) -> None:
        await command_overview.send_group_overview(
            ctx,
            lambda: self._send_configuration_overview(ctx),
            include_descendants=include_descendants,
            title="NHCogs" if ctx.command.name == "nhcogs" else "Operational errors",
        )

    async def _send_configuration_overview(self, ctx: commands.Context) -> None:
        channel_id = await self.config.error_channel()
        maintainer_id = await self.config.error_maintainer_id()
        active_failures = await self.active_count(ctx.guild.id)
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        maintainer = (
            ctx.guild.get_member(maintainer_id) if maintainer_id is not None else None
        )
        channel_label = f"#{channel.name}" if channel is not None else "Not configured"
        maintainer_label = (
            f"@{maintainer.display_name}"
            if maintainer is not None
            else "Not configured"
        )
        embed = discord.Embed(title="Operational error configuration")
        embed.add_field(name="Channel", value=channel_label, inline=False)
        embed.add_field(name="Maintainer", value=maintainer_label, inline=False)
        embed.add_field(name="Active failures", value=str(active_failures), inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _require_private_channel(ctx: commands.Context) -> None:
        if not command_overview.channel_is_private(ctx.guild, ctx.channel):
            raise commands.UserFeedbackCheckFailure(
                "Run this command in a channel hidden from @everyone"
            )

    @commands.group(name="nhcogs", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def nhcogs(self, ctx: commands.Context) -> None:
        """Configure process-wide NHCogs services."""
        await self._send_group_overview(ctx, include_descendants=False)

    @nhcogs.group(name="errors", invoke_without_command=True)
    async def nhcogs_errors(self, ctx: commands.Context) -> None:
        """Configure private process-wide operational error reporting."""
        await self._send_group_overview(ctx)

    @nhcogs_errors.group(name="channel", invoke_without_command=True)
    async def nhcogs_errors_channel(self, ctx: commands.Context) -> None:
        """Configure the private operational error channel."""
        await self._send_group_overview(ctx)

    @nhcogs_errors_channel.command(name="set")
    async def nhcogs_errors_channel_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        """Set the private operational error channel."""
        self._require_private_channel(ctx)
        if channel.guild.id != ctx.guild.id:
            raise commands.UserFeedbackCheckFailure(
                "The error channel must belong to this server"
            )
        if channel.permissions_for(ctx.guild.default_role).view_channel:
            raise commands.UserFeedbackCheckFailure(
                "The error channel must be hidden from @everyone"
            )
        bot_permissions = channel.permissions_for(ctx.guild.me)
        if not all(
            getattr(bot_permissions, permission, False)
            for permission in ("view_channel", "send_messages", "attach_files")
        ):
            raise commands.UserFeedbackCheckFailure(
                "I need View Channel, Send Messages, and Attach Files there"
            )
        await self.config.error_channel.set(channel.id)
        await ctx.send(
            f"Operational errors will be sent to #{channel.name}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhcogs_errors_channel.command(name="clear")
    async def nhcogs_errors_channel_clear(self, ctx: commands.Context) -> None:
        """Clear the private operational error channel."""
        self._require_private_channel(ctx)
        await self.config.error_channel.clear()
        await ctx.send(
            "Operational error channel cleared",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhcogs_errors.group(name="maintainer", invoke_without_command=True)
    async def nhcogs_errors_maintainer(self, ctx: commands.Context) -> None:
        """Configure the maintainer pinged by operational alerts."""
        await self._send_group_overview(ctx)

    @nhcogs_errors_maintainer.command(name="set")
    async def nhcogs_errors_maintainer_set(
        self,
        ctx: commands.Context,
        member: discord.Member,
    ) -> None:
        """Set the maintainer pinged by operational alerts."""
        self._require_private_channel(ctx)
        await self.config.error_maintainer_id.set(member.id)
        await ctx.send(
            f"Operational error maintainer set to @{member.display_name}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhcogs_errors_maintainer.command(name="clear")
    async def nhcogs_errors_maintainer_clear(self, ctx: commands.Context) -> None:
        """Clear the maintainer pinged by operational alerts."""
        self._require_private_channel(ctx)
        await self.config.error_maintainer_id.clear()
        await ctx.send(
            "Operational error maintainer cleared",
            allowed_mentions=discord.AllowedMentions.none(),
        )
