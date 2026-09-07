from __future__ import annotations

import logging
from collections.abc import Awaitable

import discord
from redbot.core import commands
from redbot.core.commands import RawUserIdConverter

from NHCogs.command_overview import send_group_overview
from NHCogs.honeypot.cleanup import CleanupResult

from .converters import RawMessageId, parse_raw_message_id

log = logging.getLogger("red.NHCogs.Cleanup")
RESULT_TTL_SECONDS = 10
BOUNDARY_AND_COUNT_ARGUMENTS = 2
BOUNDARY_COUNT_AND_FLAG_ARGUMENTS = 3
TRUE_VALUES = frozenset({"1", "yes", "y", "true", "t", "on", "enable", "enabled"})
FALSE_VALUES = frozenset({"0", "no", "n", "false", "f", "off", "disable", "disabled"})


class Cleanup(commands.Cog):
    """Delete recently observed messages without fetching channel history."""

    def __init__(self, bot, support, honeypot) -> None:
        self.bot = bot
        self.support = support
        self.honeypot = honeypot

    @commands.group(
        name="cleanup",
        invoke_without_command=True,
    )
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup(self, ctx: commands.Context) -> None:
        """Delete recently observed messages without fetching channel history."""
        await send_group_overview(ctx, title="Cleanup")

    @cleanup.command(name="messages", usage="<count>")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup_messages(self, ctx: commands.Context, count: int) -> None:
        """Delete recently observed messages from the current channel."""
        await self._run_cleanup(
            ctx,
            "clean up observed channel messages",
            self.honeypot.cleanup_channel(ctx, count),
        )

    @cleanup.command(name="user")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup_user(
        self,
        ctx: commands.Context,
        target: discord.Member | RawUserIdConverter,
        count: int,
    ) -> None:
        """Delete recently observed messages from a user across this server."""
        user_id = int(getattr(target, "id", target))
        await self._run_cleanup(
            ctx,
            "clean up observed user messages",
            self.honeypot.cleanup_user(ctx, user_id, count),
        )

    @cleanup.command(name="after")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup_after(
        self,
        ctx: commands.Context,
        message_id: RawMessageId | None = None,
        delete_pinned: bool = False,
    ) -> None:
        """Delete retained messages after a retained message in this channel."""
        boundary_id = self._resolve_boundary_id(ctx, message_id)
        await self._run_cleanup(
            ctx,
            "clean up observed messages after a boundary",
            self.honeypot.cleanup_after(
                ctx,
                boundary_id,
                delete_pinned=delete_pinned,
            ),
        )

    @cleanup.command(
        name="before",
        usage="[message_id] <count> [delete_pinned]",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup_before(
        self,
        ctx: commands.Context,
        *,
        arguments: str,
    ) -> None:
        """Delete retained messages before a retained message in this channel."""
        boundary_id, count, delete_pinned = self._parse_before_arguments(ctx, arguments)
        await self._run_cleanup(
            ctx,
            "clean up observed messages before a boundary",
            self.honeypot.cleanup_before(
                ctx,
                boundary_id,
                count,
                delete_pinned=delete_pinned,
            ),
        )

    @classmethod
    def _parse_before_arguments(
        cls,
        ctx: commands.Context,
        arguments: str,
    ) -> tuple[int, int, bool]:
        parts = arguments.split()
        has_reply = getattr(getattr(ctx.message, "reference", None), "message_id", None)
        if has_reply and len(parts) == 1:
            return cls._resolve_boundary_id(ctx, None), cls._parse_count(parts[0]), False
        if (
            has_reply
            and len(parts) == BOUNDARY_AND_COUNT_ARGUMENTS
            and parts[1].casefold() in TRUE_VALUES | FALSE_VALUES
        ):
            return (
                cls._resolve_boundary_id(ctx, None),
                cls._parse_count(parts[0]),
                cls._parse_bool(parts[1]),
            )
        if len(parts) not in {
            BOUNDARY_AND_COUNT_ARGUMENTS,
            BOUNDARY_COUNT_AND_FLAG_ARGUMENTS,
        }:
            raise commands.UserFeedbackCheckFailure(
                "Use: cleanup before [message_id] <count> [delete_pinned]"
            )
        try:
            boundary_id = parse_raw_message_id(parts[0])
        except ValueError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        return (
            boundary_id,
            cls._parse_count(parts[1]),
            cls._parse_bool(parts[2]) if len(parts) == BOUNDARY_COUNT_AND_FLAG_ARGUMENTS else False,
        )

    @staticmethod
    def _parse_count(argument: str) -> int:
        try:
            return int(argument)
        except ValueError as error:
            raise commands.UserFeedbackCheckFailure("count must be a number") from error

    @staticmethod
    def _parse_bool(argument: str) -> bool:
        normalized = argument.casefold()
        if normalized in TRUE_VALUES:
            return True
        if normalized in FALSE_VALUES:
            return False
        raise commands.UserFeedbackCheckFailure("delete_pinned must be true or false")

    @cleanup.command(name="between")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cleanup_between(
        self,
        ctx: commands.Context,
        older_id: RawMessageId,
        newer_id: RawMessageId,
        delete_pinned: bool = False,
    ) -> None:
        """Delete retained messages between two retained messages in this channel."""
        await self._run_cleanup(
            ctx,
            "clean up observed messages between boundaries",
            self.honeypot.cleanup_between(
                ctx,
                older_id,
                newer_id,
                delete_pinned=delete_pinned,
            ),
        )

    @staticmethod
    def _resolve_boundary_id(ctx: commands.Context, supplied: int | None) -> int:
        if supplied is not None:
            return supplied
        reference = getattr(ctx.message, "reference", None)
        message_id = getattr(reference, "message_id", None)
        if message_id is None:
            raise commands.UserFeedbackCheckFailure(
                "Provide a retained message ID or reply to a retained message"
            )
        return int(message_id)

    async def _run_cleanup(
        self,
        ctx: commands.Context,
        action: str,
        operation: Awaitable[CleanupResult],
    ) -> None:
        try:
            result = await operation
        except ValueError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        except Exception as error:
            log.exception("Managed cleanup failed")
            await self.support.report_operational_error(
                guild_id=ctx.guild.id,
                source="Cleanup",
                action=action,
                error=error,
                channel_id=ctx.channel.id,
                message_id=ctx.message.id,
            )
            await ctx.send("Cleanup failed. Check the private error channel and try again")
            return
        await ctx.send(
            result.public_message,
            delete_after=RESULT_TTL_SECONDS,
        )
