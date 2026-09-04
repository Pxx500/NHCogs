"""Shared operational destinations and technical error reporting."""

from __future__ import annotations

import asyncio
import logging

import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .command_overview import channel_is_private, send_group_overview
from .operational_errors import OperationalErrorReporter, OperationalFailure

log = logging.getLogger("red.NHCogs.NHMisc")
NHMISC_CONFIG_IDENTIFIER = 8597423150612235807
ERROR_CONFIG_IDENTIFIER = 8597423150612235808


class OperationalSupport(commands.Cog):
    """Own shared log delivery and technical error configuration."""

    def __init__(self, bot):
        self.bot = bot
        # Keep the persisted NHMisc identity even when NHMisc cannot load.
        self.log_config = Config.get_conf(
            None,
            identifier=NHMISC_CONFIG_IDENTIFIER,
            cog_name="NHMisc",
            force_registration=True,
        )
        self.log_config.register_guild(
            voice_log_channel=None,
            alert_channel=None,
            maintenance_channel=None,
            moderation_log_channel=None,
        )
        self.config = Config.get_conf(
            None,
            identifier=ERROR_CONFIG_IDENTIFIER,
            cog_name="OperationalSupport",
            force_registration=True,
        )
        self.config.register_guild(error_channel=None, error_maintainer_id=None)
        self._report_tasks: set[asyncio.Task] = set()
        self.operational_errors = OperationalErrorReporter(
            bot,
            self.config,
            cog_data_path(raw_name="NHMisc") / "operational_errors.sqlite",
            logger=log,
        )

    async def cog_load(self) -> None:
        await self.operational_errors.initialize()

    async def cog_unload(self) -> None:
        if self._report_tasks:
            await asyncio.gather(*tuple(self._report_tasks), return_exceptions=True)

    async def report_global_error(self, *, source: str, action: str, error: BaseException) -> None:
        """Report a suite-wide failure to its configured guild destinations."""
        try:
            configured_guilds = await self.config.all_guilds()
        except Exception:
            log.exception("Could not read technical error destinations")
            return
        for guild_id, settings in configured_guilds.items():
            if settings.get("error_channel") is not None:
                await self.report_operational_error(
                    guild_id=int(guild_id), source=source, action=action, error=error
                )

    def schedule_error(
        self, *, source: str, action: str, error: BaseException, guild_id: int | None = None
    ) -> None:
        task: asyncio.Task
        if guild_id is None:
            task = asyncio.create_task(
                self.report_global_error(source=source, action=action, error=error)
            )
        else:
            task = asyncio.create_task(
                self.report_operational_error(
                    guild_id=guild_id, source=source, action=action, error=error
                )
            )
        self._report_tasks.add(task)
        task.add_done_callback(self._report_tasks.discard)

    async def send_technical_alert(self, guild_id: int, content: str) -> None:
        try:
            await self.operational_errors.send_alert(guild_id, content)
        except Exception:
            log.exception("Could not publish operational alert for guild %s", guild_id)

    async def handle_command_error(self, ctx, error, *, source: str) -> None:
        expected = tuple(
            kind for name in (
                "UserInputError", "UserFeedbackCheckFailure", "CheckFailure",
                "CommandOnCooldown", "DisabledCommand", "MaxConcurrencyReached",
            ) if isinstance((kind := getattr(commands, name, None)), type)
            and kind not in (Exception, BaseException, object)
        )
        original = getattr(error, "original", error)
        if isinstance(error, expected) or isinstance(original, expected):
            await ctx.bot.on_command_error(ctx, original, unhandled_by_cog=True)
            return
        if ctx.guild is not None:
            await self.report_operational_error(
                guild_id=ctx.guild.id, source=source,
                action=getattr(ctx.command, "qualified_name", "unknown command"), error=original,
                channel_id=ctx.channel.id, message_id=ctx.message.id,
            )
        try:
            await ctx.send(
                "Something went wrong while running this command. The error was logged.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Could not send command error feedback")

    async def cog_command_error(self, ctx, error) -> None:
        await self.handle_command_error(ctx, error, source="NHCogs")

    @commands.group(name="nhcogs", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def nhcogs(self, ctx: commands.Context) -> None:
        """Configure shared NHCogs settings."""
        await send_group_overview(ctx, title="NHCogs", include_descendants=False)

    @nhcogs.group(name="errors", invoke_without_command=True)
    async def errors(self, ctx: commands.Context) -> None:
        """Configure technical error reports and the maintainer notification."""
        await send_group_overview(
            ctx, lambda: self._show_error_configuration(ctx), title="Technical errors"
        )

    async def _show_error_configuration(self, ctx: commands.Context, *, field: str | None = None) -> None:
        settings = self.config.guild(ctx.guild)
        embed = discord.Embed(title="Technical error reporting")
        if field in (None, "channel"):
            channel_id = await settings.error_channel()
            channel = ctx.guild.get_channel(channel_id) if channel_id is not None else None
            embed.add_field(
                name="Channel", value=f"#{channel.name}" if channel else "Not configured", inline=False
            )
        if field in (None, "maintainer"):
            maintainer_id = await settings.error_maintainer_id()
            member = ctx.guild.get_member(maintainer_id) if maintainer_id is not None else None
            embed.add_field(
                name="Maintainer", value=member.display_name if member else "Not configured", inline=False
            )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _require_private_configuration(ctx: commands.Context) -> None:
        if not channel_is_private(ctx.guild, ctx.channel):
            raise commands.UserFeedbackCheckFailure(
                "Run this command in a private moderator channel"
            )

    @errors.group(name="channel", invoke_without_command=True)
    async def error_channel(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Show or set the shared private error channel."""
        if channel is None:
            await send_group_overview(
                ctx, lambda: self._show_error_configuration(ctx, field="channel"), title="Error channel"
            )
            return
        self._require_private_configuration(ctx)
        if not channel_is_private(ctx.guild, channel):
            raise commands.UserFeedbackCheckFailure("The error channel must be hidden from @everyone")
        missing = self.missing_log_permissions(ctx.guild, channel, require_attach_files=True)
        if missing is not None:
            raise commands.UserFeedbackCheckFailure(missing)
        await self.config.guild(ctx.guild).error_channel.set(channel.id)
        await ctx.send("Error channel updated.", allowed_mentions=discord.AllowedMentions.none())

    @error_channel.command(name="clear")
    async def error_channel_clear(self, ctx: commands.Context) -> None:
        """Stop sending technical failure alerts to Discord."""
        self._require_private_configuration(ctx)
        await self.config.guild(ctx.guild).error_channel.clear()
        await ctx.send("Error channel cleared.", allowed_mentions=discord.AllowedMentions.none())

    @errors.group(name="maintainer", invoke_without_command=True)
    async def error_maintainer(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show or set the maintainer notified by technical failure alerts."""
        if member is None:
            await send_group_overview(
                ctx, lambda: self._show_error_configuration(ctx, field="maintainer"),
                title="Error maintainer",
            )
            return
        self._require_private_configuration(ctx)
        await self.config.guild(ctx.guild).error_maintainer_id.set(member.id)
        await ctx.send("Error maintainer updated.", allowed_mentions=discord.AllowedMentions.none())

    @error_maintainer.command(name="clear")
    async def error_maintainer_clear(self, ctx: commands.Context) -> None:
        """Stop pinging a maintainer in technical failure alerts."""
        self._require_private_configuration(ctx)
        await self.config.guild(ctx.guild).error_maintainer_id.clear()
        await ctx.send("Error maintainer cleared.", allowed_mentions=discord.AllowedMentions.none())

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        for guild_id, guild_data in (await self.config.all_guilds()).items():
            if guild_data.get("error_maintainer_id") == user_id:
                await self.config.guild_from_id(guild_id).error_maintainer_id.clear()

    async def report_operational_error(
        self,
        *,
        guild_id: int,
        source: str,
        action: str,
        error: BaseException,
        channel_id: int | None = None,
        thread_id: int | None = None,
        message_id: int | None = None,
    ) -> OperationalFailure | None:
        try:
            return await self.operational_errors.report(
                guild_id=guild_id,
                source=source,
                action=action,
                error=error,
                channel_id=channel_id,
                thread_id=thread_id,
                message_id=message_id,
            )
        except Exception:
            log.exception(
                "Failed to persist NH operational error for guild %s",
                guild_id,
            )
            return None

    def get_log_channel(
        self, guild: discord.Guild, channel_id: int | None
    ) -> discord.TextChannel | None:
        if channel_id is None:
            return None

        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def missing_log_permissions(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        require_attach_files: bool = False,
    ) -> str | None:
        me = guild.me
        permissions = channel.permissions_for(me)
        if not permissions.view_channel:
            return f"I need permission to view {channel.mention}."
        if not permissions.send_messages:
            return f"I need permission to send messages in {channel.mention}."
        if require_attach_files and not permissions.attach_files:
            return f"I need permission to attach files in {channel.mention}."
        return None

    async def require_private_log_channel(
        self,
        guild: discord.Guild,
        config_key: str,
        label: str,
    ) -> discord.TextChannel:
        config_value = getattr(self.log_config.guild(guild), config_key)
        return self._require_private_channel(guild, await config_value(), label)

    def _require_private_channel(self, guild, channel_id, label):
        channel = self.get_log_channel(
            guild,
            channel_id,
        )
        if channel is None:
            raise commands.UserFeedbackCheckFailure(
                f"The private {label} channel is not configured"
            )
        if channel.permissions_for(guild.default_role).view_channel:
            raise commands.UserFeedbackCheckFailure(
                f"The {label} channel must be hidden from @everyone"
            )
        if self.missing_log_permissions(guild, channel) is not None:
            raise commands.UserFeedbackCheckFailure(
                f"I cannot send messages in the {label} channel"
            )
        return channel

    async def require_private_error_channel(
        self,
        guild: discord.Guild,
    ) -> discord.TextChannel:
        """Return the configured private operational error channel."""
        return self._require_private_channel(
            guild, await self.config.guild(guild).error_channel(), "operational error"
        )

    async def send_configured_log(
        self,
        guild: discord.Guild,
        config_key: str,
        content: str,
        *,
        ping_user: discord.abc.Snowflake | None = None,
        require_private: bool = False,
        log_failure: bool = True,
    ) -> bool:
        """Send to a configured guild log destination."""
        config_value = getattr(self.log_config.guild(guild), config_key)
        channel = self.get_log_channel(guild, await config_value())
        if channel is None:
            log.warning(
                "Could not send NHMisc log for guild %s because %s is not configured",
                guild.id,
                config_key,
            )
            return False
        if require_private and channel.permissions_for(guild.default_role).view_channel:
            log.warning(
                "Could not send NHMisc log for guild %s because %s is public",
                guild.id,
                config_key,
            )
            return False

        allowed_mentions = (
            discord.AllowedMentions(
                everyone=False,
                users=[ping_user],
                roles=False,
                replied_user=False,
            )
            if ping_user is not None
            else None
        )
        return (
            await self.send_log_message(
                channel,
                content,
                allowed_mentions=allowed_mentions,
                log_failure=log_failure,
            )
            is not None
        )

    async def send_moderation_log(
        self,
        guild: discord.Guild,
        content: str,
        *,
        log_failure: bool = True,
    ) -> bool:
        """Send to the configured moderator action channel without mentions."""
        return await self.send_configured_log(
            guild,
            "moderation_log_channel",
            content,
            require_private=True,
            log_failure=log_failure,
        )

    async def send_log_message(
        self,
        channel: discord.TextChannel,
        content: str,
        *,
        allowed_mentions: discord.AllowedMentions | None = None,
        log_failure: bool = True,
    ) -> discord.Message | None:
        if allowed_mentions is None:
            allowed_mentions = discord.AllowedMentions.none()
        try:
            return await channel.send(content, allowed_mentions=allowed_mentions)
        except discord.HTTPException as error:
            if log_failure:
                log.exception("Failed to send voice log message to channel %s", channel.id)
                await self.report_operational_error(
                    guild_id=channel.guild.id,
                    source="NHMisc",
                    action="send configured log",
                    error=error,
                    channel_id=channel.id,
                )
        return None


async def ensure_operational_support(bot) -> OperationalSupport:
    support = bot.get_cog("OperationalSupport")
    if support is None:
        support = OperationalSupport(bot)
        await bot.add_cog(support)
    return support
