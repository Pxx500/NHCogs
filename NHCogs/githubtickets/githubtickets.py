from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import presentation, settings
from .coordinator import TicketActor, TicketCoordinator
from .dashboard import GitHubTicketsDashboard, send_developer_profile
from .discord_projection import DiscordTicketProjection
from .models import (
    CategoryAlreadyExists,
    CategoryLimitReached,
    InvalidCategoryName,
    PresenceTier,
    Ticket,
    TicketState,
)
from .projection import ProjectionNotFound
from .routing import CandidateFacts
from .scheduler import DeadlineScheduler
from .store import MAX_CATEGORY_NAME_LENGTH, GitHubTicketsStore
from .ticket_views import TicketControls

log = logging.getLogger(__name__)


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
        self._participant_roles: dict[int, frozenset[int]] = {}
        self.projection = DiscordTicketProjection(bot, self._ticket_view)
        self.coordinator = TicketCoordinator(
            self.store,
            self.projection,
            get_settings=self._get_guild_settings,
            get_candidates=self._get_candidates,
            wake_deadlines=self._wake_deadlines,
        )
        self.scheduler = DeadlineScheduler(self.store, self._process_due_deadline)
        self._startup_task: asyncio.Task[None] | None = None
        self._dashboard_command = discord.app_commands.Command(
            name="github-tickets",
            description=presentation.SLASH_DESCRIPTION,
            callback=self._open_dashboard,
        )
        self._developer_profile_command = discord.app_commands.ContextMenu(
            name=presentation.DEVELOPER_PROFILE_COMMAND,
            callback=self._open_developer_profile,
        )
        self._application_commands_registered = False

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        permissions = ctx.channel.permissions_for(ctx.author)
        return bool(permissions.manage_messages)

    async def cog_load(self) -> None:
        await self.store.initialize()
        await self._refresh_participant_roles()
        self._register_application_commands()
        if self._startup_task is None or self._startup_task.done():
            self._startup_task = asyncio.create_task(
                self._restore_runtime(),
                name="github-tickets-startup",
            )
            self._startup_task.add_done_callback(self._observe_startup_task)

    async def cog_unload(self) -> None:
        self._unregister_application_commands()
        startup_task = self._startup_task
        self._startup_task = None
        if startup_task is not None:
            startup_task.cancel()
            try:
                await startup_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await self.scheduler.close()

    async def _restore_runtime(self) -> None:
        await self.bot.wait_until_red_ready()
        await self._refresh_participant_roles()
        for ticket in await self.store.list_projection_cleanup_tickets():
            await self.coordinator.recover_projection_cleanup(ticket.ticket_id)
        for ticket in await self.store.list_active_tickets():
            if ticket.message_id is None:
                continue
            self.bot.add_view(
                self._ticket_view(
                    ticket.ticket_id,
                    ticket.state is TicketState.CLAIMED,
                ),
                message_id=ticket.message_id,
            )
        now = datetime.now(timezone.utc)
        for ticket_id in await self.store.due_ticket_ids(now):
            await self.coordinator.process_due(ticket_id)
        self.scheduler.start()

    @staticmethod
    def _observe_startup_task(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.error(
                "GitHub Tickets startup failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _register_application_commands(self) -> None:
        if self._application_commands_registered:
            return
        self.bot.tree.add_command(self._dashboard_command, override=True)
        self.bot.tree.add_command(self._developer_profile_command, override=True)
        self._application_commands_registered = True

    def _unregister_application_commands(self) -> None:
        if not self._application_commands_registered:
            return
        for command in (self._dashboard_command, self._developer_profile_command):
            command_type = command.type
            existing = self.bot.tree.get_command(command.name, type=command_type)
            if existing is command:
                self.bot.tree.remove_command(command.name, type=command_type)
        self._application_commands_registered = False

    @discord.app_commands.guild_only()
    async def _open_dashboard(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        actor = self._actor_from_interaction(interaction)
        if guild_id is None or not actor.can_participate:
            await interaction.response.send_message(
                presentation.CANNOT_USE_ACTION,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await GitHubTicketsDashboard(
            self.store,
            guild_id=guild_id,
            create_ticket=self.coordinator.create_ticket,
            actor_factory=self._actor_from_interaction,
        ).send(interaction)

    @discord.app_commands.guild_only()
    async def _open_developer_profile(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild_id = self._interaction_guild_id(interaction)
        actor = self._actor_from_interaction(interaction)
        if guild_id is None or not actor.can_participate:
            await interaction.response.send_message(
                presentation.CANNOT_USE_ACTION,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_developer_profile(
            interaction,
            self.store,
            guild_id=guild_id,
            user_id=member.id,
        )

    @staticmethod
    def _interaction_guild_id(interaction: discord.Interaction) -> int | None:
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is not None:
            return int(guild_id)
        guild = getattr(interaction, "guild", None)
        return int(guild.id) if guild is not None else None

    def _actor_from_interaction(self, interaction: discord.Interaction) -> TicketActor:
        guild_id = self._interaction_guild_id(interaction)
        user = interaction.user
        permissions = getattr(user, "guild_permissions", None)
        can_manage_messages = bool(
            permissions is not None and permissions.manage_messages
        )
        participant_role_ids = self._participant_roles.get(guild_id or 0, frozenset())
        member_role_ids = {int(role.id) for role in getattr(user, "roles", ())}
        return TicketActor(
            user_id=int(user.id),
            is_participant=bool(participant_role_ids.intersection(member_role_ids)),
            can_manage_messages=can_manage_messages,
        )

    def _ticket_view(self, ticket_id: int, claimed: bool) -> TicketControls:
        return TicketControls(
            ticket_id,
            claimed=claimed,
            actor_factory=self._actor_from_interaction,
            claim=self.coordinator.claim,
            decline=self.coordinator.decline,
            unassign=self.coordinator.unassign,
            mark_finished=self.coordinator.mark_finished,
        )

    async def _get_guild_settings(self, guild_id: int) -> settings.GuildSettings:
        raw = await self.config.guild_from_id(guild_id).all()
        return settings.GuildSettings.from_mapping(raw)

    async def _refresh_participant_roles(self) -> None:
        guilds = await self.config.all_guilds()
        self._participant_roles = {
            int(guild_id): frozenset(
                settings.GuildSettings.from_mapping(raw).participant_role_ids
            )
            for guild_id, raw in guilds.items()
        }

    def _wake_deadlines(self) -> None:
        self.scheduler.wake()

    async def _process_due_deadline(self, ticket_id: int) -> None:
        await self.coordinator.process_due(ticket_id)

    async def _get_candidates(self, ticket: Ticket) -> tuple[CandidateFacts, ...]:
        guild = self.bot.get_guild(ticket.guild_id)
        if guild is None:
            return ()
        members = tuple(getattr(guild, "members", ()))
        histories = await self.store.candidate_history(
            ticket.ticket_id,
            (int(member.id) for member in members),
        )
        history_by_id = {history.user_id: history for history in histories}
        participant_role_ids = self._participant_roles.get(ticket.guild_id, frozenset())
        candidates: list[CandidateFacts] = []
        for member in members:
            user_id = int(member.id)
            history = history_by_id.get(user_id)
            if history is None:
                continue
            member_role_ids = {int(role.id) for role in getattr(member, "roles", ())}
            permissions = getattr(member, "guild_permissions", None)
            candidates.append(
                CandidateFacts(
                    user_id=user_id,
                    is_cached_member=True,
                    has_participant_role=bool(
                        participant_role_ids.intersection(member_role_ids)
                    ),
                    can_manage_messages=bool(
                        permissions is not None and permissions.manage_messages
                    ),
                    has_profile=history.has_profile,
                    allows_automatic_pings=history.automatic_pings,
                    matching_category_count=history.matching_category_count,
                    was_pinged=history.was_pinged,
                    timed_out=history.timed_out,
                    declined=history.declined,
                    unassigned=history.unassigned,
                    presence_tier=self._presence_tier(member),
                    active_assignment_count=history.active_assignment_count,
                    last_ping_at=history.last_ping_at,
                )
            )
        return tuple(candidates)

    @staticmethod
    def _presence_tier(member: discord.Member) -> PresenceTier:
        status = getattr(member, "status", None)
        value = getattr(status, "value", str(status)).lower()
        if value == "online":
            return PresenceTier.ONLINE
        if value == "idle":
            return PresenceTier.IDLE
        if value in ("dnd", "do_not_disturb"):
            return PresenceTier.DO_NOT_DISTURB
        return PresenceTier.OFFLINE

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload) -> None:
        await self.coordinator.handle_message_deleted(int(payload.message_id))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload) -> None:
        for message_id in payload.message_ids:
            await self.coordinator.handle_message_deleted(int(message_id))

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        await self.coordinator.handle_thread_deleted(int(thread.id))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild_id = int(channel.guild.id)
        await self.store.delete_tickets_for_channel(guild_id, int(channel.id))
        guild_config = self.config.guild_from_id(guild_id)
        configured_channel_id = await guild_config.get_raw(
            "ticket_channel_id",
            default=None,
        )
        if configured_channel_id == channel.id:
            await guild_config.clear_raw("ticket_channel_id")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild_id = int(role.guild.id)
        guild_config = self.config.guild_from_id(guild_id)
        role_ids = list(await guild_config.get_raw("participant_role_ids", default=[]))
        if role.id not in role_ids:
            return
        role_ids.remove(role.id)
        await guild_config.set_raw("participant_role_ids", value=role_ids)
        self._participant_roles[guild_id] = frozenset(role_ids)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        guild_id = int(guild.id)
        await self.store.delete_guild_state(guild_id)
        await self.config.guild_from_id(guild_id).clear()
        self._participant_roles.pop(guild_id, None)

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        del requester
        now = datetime.now(timezone.utc)
        authored_tickets = await self.store.list_authored_tickets(user_id)
        for ticket in authored_tickets:
            cleanup = await self.store.begin_authored_ticket_cleanup(
                ticket.ticket_id,
                author_id=user_id,
                updated_at=now,
            )
            if cleanup is not None:
                await self.coordinator.recover_projection_cleanup(cleanup.ticket_id)

        protection_until_by_guild = {}
        for guild_id in await self.store.user_reference_guild_ids(user_id):
            guild_settings = await self._get_guild_settings(guild_id)
            protection_until_by_guild[guild_id] = now + timedelta(
                seconds=guild_settings.protection_seconds
            )
        affected = await self.store.redact_user(
            user_id,
            protection_until_by_guild=protection_until_by_guild,
            updated_at=now,
        )
        for ticket in affected:
            if ticket.state is TicketState.CLAIMED:
                continue
            try:
                await self.projection.edit_ticket(ticket)
            except ProjectionNotFound:
                if ticket.message_id is not None:
                    await self.coordinator.handle_message_deleted(ticket.message_id)
            except Exception:
                pass
        if any(ticket.next_action_at is not None for ticket in affected):
            self.scheduler.wake()

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
        channel: discord.abc.GuildChannel,
    ) -> None:
        """Set the ticket channel"""
        if not isinstance(channel, discord.TextChannel):
            await ctx.send(presentation.TICKET_CHANNEL_MUST_BE_TEXT)
            return
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
        self._participant_roles[ctx.guild.id] = frozenset(role_ids)
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
        self._participant_roles[ctx.guild.id] = frozenset(role_ids)
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
