from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import discord
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator

from .log_buffer import MAX_DUMP_HOURS, ReadOnlyLogBuffer, build_log_dump

_ = Translator("ConsoleDump", __file__)

CONSOLE_DUMP_USAGE = (
    "Usage: `consoledump <bot|honeypot> <hours 1-24> "
    "[debug|info|warning|error|critical]`\n"
    "Scope: `bot` includes all captured Python logs. `honeypot` includes Honeypot "
    "logs and related tracebacks.\n"
    "Hours: a whole number from 1 to 24.\n"
    "Level (optional): the minimum log level to include. Omit it to include all "
    "levels.\n"
    "Examples: `consoledump bot 2`, `consoledump honeypot 1`, "
    "`consoledump bot 6 error`"
)


class ConsoleDump(commands.Cog):
    """Capture sanitized Python logs for private moderator exports."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._console_log_buffer = ReadOnlyLogBuffer()

    async def cog_load(self) -> None:
        root_logger = logging.getLogger()
        if self._console_log_buffer not in root_logger.handlers:
            root_logger.addHandler(self._console_log_buffer)

    async def cog_unload(self) -> None:
        root_logger = logging.getLogger()
        if self._console_log_buffer in root_logger.handlers:
            root_logger.removeHandler(self._console_log_buffer)

    @commands.command(name="consoledump")
    @commands.guild_only()
    async def console_dump(
        self,
        ctx: commands.Context,
        scope: str | None = None,
        hours: str | None = None,
        level: str | None = None,
    ) -> None:
        """Export recent sanitized Python logs to a private text channel."""
        channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send(_("Console dumps require a private text channel."))
            return
        if not channel.permissions_for(ctx.author).manage_messages:
            await ctx.send(_("You need Manage Messages to use this command."))
            return
        if channel.permissions_for(ctx.guild.default_role).view_channel:
            await ctx.send(_("Console dumps cannot be sent to a channel visible to @everyone."))
            return

        missing_permissions = self._missing_channel_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            await ctx.send(missing_permissions)
            return

        normalized_scope = scope.casefold() if scope is not None else None
        normalized_level = level.casefold() if level is not None else None
        try:
            parsed_hours = int(hours) if hours is not None else None
        except ValueError:
            parsed_hours = None
        levels = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }
        if (
            normalized_scope not in {"bot", "honeypot"}
            or parsed_hours is None
            or not 1 <= parsed_hours <= MAX_DUMP_HOURS
            or (normalized_level is not None and normalized_level not in levels)
        ):
            await ctx.send(CONSOLE_DUMP_USAGE)
            return

        dump = build_log_dump(
            self._console_log_buffer.snapshot(),
            scope=normalized_scope,
            hours=parsed_hours,
            minimum_level=levels.get(normalized_level),
            upload_limit=int(ctx.guild.filesize_limit),
            now=datetime.now(timezone.utc),
        )
        await ctx.send(
            file=discord.File(io.BytesIO(dump.content), filename=dump.filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _missing_channel_permissions(
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> str | None:
        me = guild.me
        if me is None:
            return _("I couldn't find my server member.")
        permissions = channel.permissions_for(me)
        if not permissions.view_channel:
            return _("I need `View Channel` in {channel}.").format(channel=channel.mention)
        if not permissions.send_messages:
            return _("I need `Send Messages` in {channel}.").format(channel=channel.mention)
        if not permissions.attach_files:
            return _("I need `Attach Files` in {channel}.").format(channel=channel.mention)
        return None
