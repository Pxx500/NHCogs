from __future__ import annotations

from datetime import datetime, timezone

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import presentation, settings
from .models import CategoryAlreadyExists, CategoryLimitReached, InvalidCategoryName
from .store import MAX_CATEGORY_NAME_LENGTH, GitHubTicketsStore


class GitHubTickets(commands.Cog):
    """Configure GitHub Tickets"""

    CONFIG_IDENTIFIER = 228724500916148494760637198509440112622

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=self.CONFIG_IDENTIFIER,
            force_registration=True,
        )
        self.config.register_guild(**settings.DEFAULTS)
        self.store = GitHubTicketsStore(cog_data_path(self) / "githubtickets.sqlite")

    async def cog_load(self) -> None:
        await self.store.initialize()

    @commands.group(name="githubtickets", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def githubtickets(self, ctx: commands.Context) -> None:
        """Configure GitHub Tickets"""
        await self._send_configuration_overview(ctx)

    async def _send_configuration_overview(self, ctx: commands.Context) -> None:
        guild_settings = settings.GuildSettings.from_mapping(
            await self.config.guild(ctx.guild).all()
        )
        categories = await self.store.list_categories(ctx.guild.id)
        await ctx.send(
            presentation.configuration_overview(
                ticket_channel=(
                    f"<#{guild_settings.ticket_channel_id}>"
                    if guild_settings.ticket_channel_id is not None
                    else None
                ),
                participant_roles=tuple(
                    f"<@&{role_id}>" for role_id in guild_settings.participant_role_ids
                ),
                categories=tuple(category.name for category in categories),
                max_pings=guild_settings.max_pings,
                protection_seconds=guild_settings.protection_seconds,
                volunteer_seconds=guild_settings.volunteer_seconds,
                online_response_seconds=guild_settings.online_response_seconds,
                idle_response_seconds=guild_settings.idle_response_seconds,
                dnd_response_seconds=guild_settings.dnd_response_seconds,
                offline_response_seconds=guild_settings.offline_response_seconds,
                direct_response_seconds=guild_settings.direct_response_seconds,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @githubtickets.group(name="channel", invoke_without_command=True)
    async def githubtickets_channel(self, ctx: commands.Context) -> None:
        """Configure the ticket channel"""
        await self._send_configuration_overview(ctx)

    @githubtickets_channel.command(name="set")
    async def githubtickets_channel_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        """Set the ticket channel"""
        await self.config.guild(ctx.guild).set_raw("ticket_channel_id", value=channel.id)
        await ctx.send(
            presentation.ticket_channel_set(channel.mention),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @githubtickets_channel.command(name="clear")
    async def githubtickets_channel_clear(self, ctx: commands.Context) -> None:
        """Clear the ticket channel"""
        await self.config.guild(ctx.guild).clear_raw("ticket_channel_id")
        await ctx.send(presentation.TICKET_CHANNEL_CLEARED)

    @githubtickets.group(name="role", invoke_without_command=True)
    async def githubtickets_role(self, ctx: commands.Context) -> None:
        """Configure participant roles"""
        await self._send_configuration_overview(ctx)

    @githubtickets_role.command(name="add")
    async def githubtickets_role_add(
        self,
        ctx: commands.Context,
        role: discord.Role,
    ) -> None:
        """Add a participant role"""
        guild_config = self.config.guild(ctx.guild)
        role_ids = list(await guild_config.get_raw("participant_role_ids", default=[]))
        if role.id in role_ids:
            await ctx.send(presentation.ROLE_ALREADY_CONFIGURED)
            return
        role_ids.append(role.id)
        await guild_config.set_raw("participant_role_ids", value=role_ids)
        await ctx.send(
            presentation.participant_role_added(role.mention),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @githubtickets_role.command(name="remove")
    async def githubtickets_role_remove(
        self,
        ctx: commands.Context,
        role: discord.Role,
    ) -> None:
        """Remove a participant role"""
        guild_config = self.config.guild(ctx.guild)
        role_ids = list(await guild_config.get_raw("participant_role_ids", default=[]))
        if role.id not in role_ids:
            await ctx.send(presentation.ROLE_NOT_CONFIGURED)
            return
        role_ids.remove(role.id)
        await guild_config.set_raw("participant_role_ids", value=role_ids)
        await ctx.send(
            presentation.participant_role_removed(role.mention),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @githubtickets.group(name="category", invoke_without_command=True)
    async def githubtickets_category(self, ctx: commands.Context) -> None:
        """Configure categories"""
        await self._send_configuration_overview(ctx)

    @githubtickets_category.command(name="add")
    async def githubtickets_category_add(
        self,
        ctx: commands.Context,
        *,
        name: str,
    ) -> None:
        """Add a category"""
        normalized = name.strip().lower()
        if not normalized:
            await ctx.send(presentation.CATEGORY_NAME_EMPTY)
            return
        if len(normalized) > MAX_CATEGORY_NAME_LENGTH:
            await ctx.send(presentation.CATEGORY_NAME_TOO_LONG)
            return
        try:
            category = await self.store.add_category(
                ctx.guild.id,
                normalized,
                datetime.now(timezone.utc),
            )
        except InvalidCategoryName:
            await ctx.send(presentation.CATEGORY_NAME_EMPTY)
            return
        except CategoryAlreadyExists:
            await ctx.send(presentation.CATEGORY_ALREADY_EXISTS)
            return
        except CategoryLimitReached:
            await ctx.send(presentation.CATEGORY_LIMIT_REACHED)
            return
        await ctx.send(presentation.category_added(category.name))

    @githubtickets_category.command(name="remove")
    async def githubtickets_category_remove(
        self,
        ctx: commands.Context,
        *,
        name: str,
    ) -> None:
        """Remove a category"""
        normalized = name.strip().lower()
        category = next(
            (
                category
                for category in await self.store.list_categories(ctx.guild.id)
                if category.name == normalized
            ),
            None,
        )
        if category is None:
            await ctx.send(presentation.CATEGORY_NOT_FOUND)
            return
        await self.store.delete_category(category.category_id)
        await ctx.send(presentation.category_removed(category.name))

    @githubtickets.command(name="maxpings")
    async def githubtickets_maxpings(
        self,
        ctx: commands.Context,
        count: int,
    ) -> None:
        """Set the maximum pings per ticket"""
        if count < 0:
            await ctx.send(presentation.MAXIMUM_PINGS_NEGATIVE)
            return
        await self.config.guild(ctx.guild).set_raw("max_pings", value=count)
        await ctx.send(presentation.maximum_pings_set(count))

    @githubtickets.group(name="timing", invoke_without_command=True)
    async def githubtickets_timing(self, ctx: commands.Context) -> None:
        """Configure ticket timing"""
        await self._send_configuration_overview(ctx)

    @githubtickets_timing.command(name="protection")
    async def githubtickets_timing_protection(
        self, ctx: commands.Context, duration: str
    ) -> None:
        """Set the protection period"""
        await self._set_timing(ctx, "protection_seconds", "Protection period", duration)

    @githubtickets_timing.command(name="volunteer")
    async def githubtickets_timing_volunteer(
        self, ctx: commands.Context, duration: str
    ) -> None:
        """Set the initial volunteer window"""
        await self._set_timing(
            ctx,
            "volunteer_seconds",
            "Initial volunteer window",
            duration,
        )

    @githubtickets_timing.command(name="online")
    async def githubtickets_timing_online(
        self, ctx: commands.Context, duration: str
    ) -> None:
        """Set the Online response time"""
        await self._set_timing(ctx, "online_response_seconds", "Online response time", duration)

    @githubtickets_timing.command(name="idle")
    async def githubtickets_timing_idle(
        self, ctx: commands.Context, duration: str
    ) -> None:
        """Set the Idle response time"""
        await self._set_timing(ctx, "idle_response_seconds", "Idle response time", duration)

    @githubtickets_timing.command(name="donotdisturb")
    async def githubtickets_timing_donotdisturb(
        self, ctx: commands.Context, duration: str
    ) -> None:
        """Set the Do Not Disturb response time"""
        await self._set_timing(
            ctx,
            "dnd_response_seconds",
            "Do Not Disturb response time",
            duration,
        )

    @githubtickets_timing.command(name="offline")
    async def githubtickets_timing_offline(
        self, ctx: commands.Context, duration: str
    ) -> None:
        """Set the Offline response time"""
        await self._set_timing(
            ctx,
            "offline_response_seconds",
            "Offline response time",
            duration,
        )

    @githubtickets_timing.command(name="direct")
    async def githubtickets_timing_direct(
        self, ctx: commands.Context, duration: str
    ) -> None:
        """Set the direct response time"""
        await self._set_timing(ctx, "direct_response_seconds", "Direct response time", duration)

    async def _set_timing(
        self,
        ctx: commands.Context,
        key: str,
        label: str,
        duration: str,
    ) -> None:
        try:
            seconds = settings.parse_duration(duration)
        except settings.NegativeDuration:
            await ctx.send(presentation.DURATION_NEGATIVE)
            return
        except settings.InvalidDuration:
            await ctx.send(presentation.INVALID_DURATION)
            return
        await self.config.guild(ctx.guild).set_raw(key, value=seconds)
        await ctx.send(presentation.timing_set(label, seconds))

    @githubtickets.group(name="profile", invoke_without_command=True)
    async def githubtickets_profile(self, ctx: commands.Context) -> None:
        """Manage developer profiles"""
        await self._send_configuration_overview(ctx)

    @githubtickets_profile.command(name="clear")
    async def githubtickets_profile_clear(
        self,
        ctx: commands.Context,
        user_id: str,
    ) -> None:
        """Clear a developer profile"""
        try:
            parsed_user_id = int(user_id)
        except (TypeError, ValueError):
            parsed_user_id = 0
        if parsed_user_id <= 0:
            await ctx.send(presentation.INVALID_USER_ID)
            return
        await self.store.save_profile(
            guild_id=ctx.guild.id,
            user_id=parsed_user_id,
            github_username=None,
            category_ids=(),
            automatic_pings=False,
            updated_at=datetime.now(timezone.utc),
        )
        await ctx.send(presentation.profile_cleared(parsed_user_id))
