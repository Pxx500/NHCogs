from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from .. import command_overview
from . import presentation, settings
from .coordinator import TicketActor, TicketCoordinator, TicketResult
from .dashboard import (
    GitHubTicketsDashboard,
    send_developer_profile,
    send_new_ticket_modal,
)
from .discord_projection import DiscordTicketProjection
from .github_app import GitHubAppClient
from .models import (
    CategoryAlreadyExists,
    CategoryLimitReached,
    InvalidCategoryName,
    PresenceTier,
    Ticket,
    TicketState,
)
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
        self._github_client: GitHubAppClient | None = None
        self._github_organization: str | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._new_ticket_command = discord.app_commands.Command(
            name=presentation.NEW_TICKET_COMMAND,
            description=presentation.NEW_TICKET_COMMAND_DESCRIPTION,
            callback=self._safe_open_new_ticket,
        )
        self._developer_profile_slash_command = discord.app_commands.Command(
            name=presentation.DEVELOPER_PROFILE_SLASH_COMMAND,
            description=presentation.DEVELOPER_PROFILE_SLASH_DESCRIPTION,
            callback=self._safe_open_profile_dashboard,
        )
        self._developer_profile_context_command = discord.app_commands.ContextMenu(
            name=presentation.DEVELOPER_PROFILE_COMMAND,
            callback=self._safe_open_developer_profile,
        )
        self._application_commands_registered = False
        self._replaced_application_commands: dict[
            tuple[str, discord.AppCommandType],
            (
                discord.app_commands.Command
                | discord.app_commands.ContextMenu
                | discord.app_commands.Group
                | None
            ),
        ] = {}
        self._restored_view_message_ids: set[int] = set()

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
        while True:
            try:
                await self._restore_runtime_once()
                self.scheduler.start()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("GitHub Tickets startup restore iteration failed")
                self.scheduler.start()
                await asyncio.sleep(1)

    async def _restore_runtime_once(self) -> None:
        await self._refresh_participant_roles()
        for ticket in await self.store.list_projection_cleanup_tickets():
            await self.coordinator.recover_projection_cleanup(ticket.ticket_id)
        for ticket in await self.store.list_active_tickets():
            if ticket.message_id is None or ticket.message_id in self._restored_view_message_ids:
                continue
            self.bot.add_view(self._ticket_view(ticket), message_id=ticket.message_id)
            self._restored_view_message_ids.add(ticket.message_id)
        now = datetime.now(timezone.utc)
        for ticket_id in await self.store.due_ticket_ids(now):
            await self.coordinator.process_due(ticket_id)

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
        commands_to_add = self._application_commands()
        previous = {
            (command.name, self._application_command_type(command)): self.bot.tree.get_command(
                command.name,
                type=self._application_command_type(command),
            )
            for command in commands_to_add
        }
        try:
            for command in commands_to_add:
                self.bot.tree.add_command(command, override=True)
        except Exception:
            for command in commands_to_add:
                command_type = self._application_command_type(command)
                existing = self.bot.tree.get_command(command.name, type=command_type)
                if existing is command:
                    self.bot.tree.remove_command(command.name, type=command_type)
            for _key, command in previous.items():
                if command is not None:
                    try:
                        self.bot.tree.add_command(command, override=True)
                    except Exception:
                        log.exception(
                            "GitHub Tickets failed to restore an application command"
                        )
            raise
        self._replaced_application_commands = previous
        self._application_commands_registered = True

    def _unregister_application_commands(self) -> None:
        if not self._application_commands_registered:
            return
        previous = self._replaced_application_commands
        self._replaced_application_commands = {}
        for command in self._application_commands():
            command_type = self._application_command_type(command)
            existing = self.bot.tree.get_command(command.name, type=command_type)
            if existing is command:
                self.bot.tree.remove_command(command.name, type=command_type)
                displaced = previous.get((command.name, command_type))
                if displaced is not None:
                    try:
                        self.bot.tree.add_command(displaced, override=True)
                    except Exception:
                        log.exception(
                            "GitHub Tickets failed to restore a displaced application command"
                        )
        self._application_commands_registered = False

    @staticmethod
    def _application_command_type(command) -> discord.AppCommandType:
        return getattr(command, "type", discord.AppCommandType.chat_input)

    def _application_commands(self):
        return (
            self._new_ticket_command,
            self._developer_profile_slash_command,
            self._developer_profile_context_command,
        )

    @discord.app_commands.guild_only()
    async def _open_new_ticket(self, interaction: discord.Interaction) -> None:
        guild_id = self._interaction_guild_id(interaction)
        if guild_id is None:
            await interaction.response.send_message(
                presentation.CANNOT_USE_ACTION,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await send_new_ticket_modal(
            interaction,
            self.store,
            guild_id=guild_id,
            create_ticket=self.coordinator.create_ticket_for_pull_request,
            fetch_pull_request=(
                self._github_client.get_pull_request
                if self._github_client is not None
                else None
            ),
            expected_organization=self._github_organization,
            actor_factory=self._actor_from_interaction,
            count_automatic_candidates=self._count_automatic_candidates,
        )

    @discord.app_commands.guild_only()
    async def _open_profile_dashboard(
        self,
        interaction: discord.Interaction,
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
        await GitHubTicketsDashboard(
            self.store,
            guild_id=guild_id,
            actor_factory=self._actor_from_interaction,
            member_lookup=lambda user_id: self._cached_member(guild_id, user_id),
            member_actor_factory=lambda member: self._actor_for_member(
                guild_id,
                member,
            ),
        ).send(interaction)

    @discord.app_commands.guild_only()
    async def _safe_open_new_ticket(self, interaction: discord.Interaction) -> None:
        try:
            await self._open_new_ticket(interaction)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("GitHub Tickets new ticket callback failed")
            await self._send_interaction_failure(interaction)

    @discord.app_commands.guild_only()
    async def _safe_open_profile_dashboard(
        self,
        interaction: discord.Interaction,
    ) -> None:
        try:
            await self._open_profile_dashboard(interaction)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("GitHub Tickets developer profile dashboard callback failed")
            await self._send_interaction_failure(interaction)

    @discord.app_commands.guild_only()
    async def _safe_open_developer_profile(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        try:
            await self._open_developer_profile(interaction, member)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("GitHub Tickets profile callback failed")
            await self._send_interaction_failure(interaction)

    @staticmethod
    async def _send_interaction_failure(interaction: discord.Interaction) -> None:
        response = interaction.response
        is_done = getattr(response, "is_done", lambda: False)
        sender = interaction.followup.send if is_done() else response.send_message
        await sender(
            presentation.COULD_NOT_COMPLETE_ACTION,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
        return self._actor_for_member(guild_id or 0, interaction.user)

    def _actor_for_member(
        self,
        guild_id: int,
        member: discord.Member,
    ) -> TicketActor:
        permissions = getattr(member, "guild_permissions", None)
        can_manage_messages = bool(
            permissions is not None and permissions.manage_messages
        )
        participant_role_ids = self._participant_roles.get(guild_id, frozenset())
        member_role_ids = {int(role.id) for role in getattr(member, "roles", ())}
        return TicketActor(
            user_id=int(member.id),
            is_participant=bool(participant_role_ids.intersection(member_role_ids)),
            can_manage_messages=can_manage_messages,
        )

    def _cached_member(self, guild_id: int, user_id: int) -> discord.Member | None:
        guild = self.bot.get_guild(guild_id)
        return guild.get_member(user_id) if guild is not None else None

    def _ticket_view(self, ticket: Ticket) -> TicketControls:
        return TicketControls(
            ticket.ticket_id,
            ticket.public_token,
            claimed=ticket.state is TicketState.CLAIMED,
            actor_factory=self._actor_from_interaction,
            claim=self._claim_ticket,
            decline=self._decline_ticket,
            unassign=self._unassign_ticket,
            mark_finished=self._finish_ticket,
        )

    async def _ticket_action(self, action, public_token: str, actor: TicketActor):
        ticket = await self.store.get_ticket_by_public_token(public_token)
        if ticket is None:
            return TicketResult(False, presentation.TICKET_NOT_ACTIVE)
        return await action(ticket.ticket_id, actor)

    async def _claim_ticket(self, public_token: str, actor: TicketActor):
        return await self._ticket_action(self.coordinator.claim, public_token, actor)

    async def _decline_ticket(self, public_token: str, actor: TicketActor):
        return await self._ticket_action(self.coordinator.decline, public_token, actor)

    async def _unassign_ticket(self, public_token: str, actor: TicketActor):
        return await self._ticket_action(self.coordinator.unassign, public_token, actor)

    async def _finish_ticket(self, public_token: str, actor: TicketActor):
        ticket = await self.store.get_ticket_by_public_token(public_token)
        if ticket is None:
            return TicketResult(False, presentation.TICKET_NOT_ACTIVE)
        result = await self.coordinator.mark_finished(ticket.ticket_id, actor)
        if result.success and result.finished_ticket is not None:
            await self._log_finished_ticket(result.finished_ticket, actor.user_id)
        return result

    async def _log_finished_ticket(self, ticket: Ticket, actor_id: int) -> None:
        try:
            guild_settings = await self._get_guild_settings(ticket.guild_id)
            if guild_settings.log_channel_id is None:
                return
            channel = self.bot.get_channel(guild_settings.log_channel_id)
            if channel is None:
                return
            await channel.send(
                presentation.finished_ticket_log(
                    title=ticket.pr_title,
                    url=ticket.pr_url,
                    actor_id=actor_id,
                    author_id=ticket.author_id,
                    reviewer_id=ticket.assignee_id,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("GitHub Tickets finished ticket log failed")

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

    async def _automatic_candidate_ids(
        self,
        guild_id: int,
        category_ids: tuple[int, ...],
        excluded_user_ids: frozenset[int],
    ) -> frozenset[int]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return frozenset()
        profiles = await self.store.list_matching_profiles(guild_id, category_ids)
        profile_ids = {profile.user_id for profile in profiles}
        return frozenset(
            user_id
            for member in getattr(guild, "members", ())
            if (user_id := int(member.id)) not in excluded_user_ids
            and user_id in profile_ids
            and self._actor_for_member(guild_id, member).can_participate
        )

    async def _count_automatic_candidates(
        self,
        guild_id: int,
        category_ids: tuple[int, ...],
        excluded_user_ids: frozenset[int],
    ) -> int:
        return len(
            await self._automatic_candidate_ids(
                guild_id,
                category_ids,
                excluded_user_ids,
            )
        )

    async def _get_candidates(self, ticket: Ticket) -> tuple[CandidateFacts, ...]:
        guild = self.bot.get_guild(ticket.guild_id)
        if guild is None:
            return ()
        members = tuple(getattr(guild, "members", ()))
        matching_profile_ids = await self._automatic_candidate_ids(
            ticket.guild_id,
            ticket.category_ids,
            frozenset({ticket.author_id}),
        )
        histories = await self.store.candidate_history(
            ticket.ticket_id,
            (
                int(member.id)
                for member in members
                if int(member.id) != ticket.author_id
            ),
        )
        history_by_id = {history.user_id: history for history in histories}
        candidates: list[CandidateFacts] = []
        for member in members:
            user_id = int(member.id)
            if user_id == ticket.author_id:
                continue
            history = history_by_id.get(user_id)
            if history is None:
                continue
            actor = self._actor_for_member(ticket.guild_id, member)
            candidates.append(
                CandidateFacts(
                    user_id=user_id,
                    is_cached_member=True,
                    has_participant_role=actor.is_participant,
                    can_manage_messages=actor.can_manage_messages,
                    matches_profile=user_id in matching_profile_ids,
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
        await self._run_listener(
            lambda: self.coordinator.handle_message_deleted(int(payload.message_id)),
            "raw message deletion",
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload) -> None:
        for message_id in payload.message_ids:
            await self._run_listener(
                lambda message_id=message_id: self.coordinator.handle_message_deleted(
                    int(message_id)
                ),
                "bulk message deletion",
            )

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        await self._run_listener(
            lambda: self.coordinator.handle_thread_deleted(int(thread.id)),
            "thread deletion",
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await self.store.delete_profile(int(member.guild.id), int(member.id))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("GitHub Tickets member profile deletion failed")

    async def _run_listener(self, operation, label: str) -> None:
        for attempt in range(2):
            try:
                await operation()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("GitHub Tickets %s failed", label)
                if attempt == 0:
                    await asyncio.sleep(1)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self._run_listener(
            lambda: self._handle_guild_channel_delete(channel),
            "guild channel deletion",
        )

    async def _handle_guild_channel_delete(self, channel) -> None:
        guild_id = int(channel.guild.id)
        config_error: Exception | None = None
        try:
            guild_config = self.config.guild_from_id(guild_id)
            configured_channel_id = await guild_config.get_raw(
                "ticket_channel_id",
                default=None,
            )
            if configured_channel_id == channel.id:
                await guild_config.clear_raw("ticket_channel_id")
            configured_log_channel_id = await guild_config.get_raw(
                "log_channel_id",
                default=None,
            )
            if configured_log_channel_id == channel.id:
                await guild_config.clear_raw("log_channel_id")
        except Exception as error:
            config_error = error
        try:
            await self.store.delete_tickets_for_channel(guild_id, int(channel.id))
        except Exception as store_error:
            if config_error is not None:
                raise store_error from config_error
            raise
        if config_error is not None:
            raise config_error

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._run_listener(
            lambda: self._handle_guild_role_delete(role),
            "guild role deletion",
        )

    async def _handle_guild_role_delete(self, role) -> None:
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
        await self._run_listener(
            lambda: self._handle_guild_remove(guild),
            "guild removal",
        )

    async def _handle_guild_remove(self, guild) -> None:
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
        affected = await self.coordinator.redact_user(
            user_id,
            updated_at=now,
        )
        for ticket in affected:
            if ticket.state is TicketState.CLAIMED:
                continue
            await self.coordinator.sync_projection(ticket.ticket_id)
        if any(ticket.next_action_at is not None for ticket in affected):
            self.scheduler.wake()

    @commands.group(name="githubtickets", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def githubtickets(self, ctx: commands.Context) -> None:
        """Configure GitHub Tickets"""
        await self._send_group_overview(ctx, include_descendants=False)

    async def _send_group_overview(
        self,
        ctx: commands.Context,
        *,
        include_descendants: bool = True,
    ) -> None:
        titles = {
            "githubtickets": "GitHub Tickets",
            "logchannel": "Log channel",
        }
        await command_overview.send_group_overview(
            ctx,
            lambda: self._send_configuration_overview(ctx),
            include_descendants=include_descendants,
            title=titles.get(ctx.command.name),
        )

    async def _send_configuration_overview(self, ctx: commands.Context) -> None:
        guild_settings = settings.GuildSettings.from_mapping(
            await self.config.guild(ctx.guild).all()
        )
        categories = await self.store.list_categories(ctx.guild.id)
        content = presentation.configuration_overview(
                ticket_channel=(
                    f"<#{guild_settings.ticket_channel_id}>"
                    if guild_settings.ticket_channel_id is not None
                    else None
                ),
                log_channel=(
                    f"<#{guild_settings.log_channel_id}>"
                    if guild_settings.log_channel_id is not None
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
            )
        for chunk in presentation.message_chunks(content):
            await ctx.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @githubtickets.group(name="channel", invoke_without_command=True)
    async def githubtickets_channel(self, ctx: commands.Context) -> None:
        """Configure the ticket channel"""
        await self._send_group_overview(ctx)

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

    @githubtickets.group(name="logchannel", invoke_without_command=True)
    async def githubtickets_logchannel(self, ctx: commands.Context) -> None:
        """Configure the log channel"""
        await self._send_group_overview(ctx)

    @githubtickets_logchannel.command(name="set")
    async def githubtickets_logchannel_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        """Set the log channel"""
        await self.config.guild(ctx.guild).set_raw("log_channel_id", value=channel.id)
        await ctx.send(
            presentation.log_channel_set(channel.mention),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @githubtickets_logchannel.command(name="clear")
    async def githubtickets_logchannel_clear(self, ctx: commands.Context) -> None:
        """Clear the log channel"""
        await self.config.guild(ctx.guild).clear_raw("log_channel_id")
        await ctx.send(presentation.LOG_CHANNEL_CLEARED)

    @githubtickets.group(name="role", invoke_without_command=True)
    async def githubtickets_role(self, ctx: commands.Context) -> None:
        """Configure participant roles"""
        await self._send_group_overview(ctx)

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
        await self._send_group_overview(ctx)

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

    @githubtickets_category.command(name="rename")
    async def githubtickets_category_rename(
        self,
        ctx: commands.Context,
        old_name: str,
        *,
        new_name: str,
    ) -> None:
        """Rename a category"""
        normalized_old_name = old_name.strip().lower()
        normalized_new_name = new_name.strip().lower()
        if not normalized_new_name:
            await ctx.send(presentation.CATEGORY_NAME_EMPTY)
            return
        if len(normalized_new_name) > MAX_CATEGORY_NAME_LENGTH:
            await ctx.send(presentation.CATEGORY_NAME_TOO_LONG)
            return
        try:
            category = await self.store.rename_category(
                ctx.guild.id,
                normalized_old_name,
                normalized_new_name,
            )
        except InvalidCategoryName:
            await ctx.send(presentation.CATEGORY_NAME_EMPTY)
            return
        except CategoryAlreadyExists:
            await ctx.send(presentation.CATEGORY_ALREADY_EXISTS)
            return
        if category is None:
            await ctx.send(presentation.CATEGORY_NOT_FOUND)
            return
        await ctx.send(
            presentation.category_renamed(normalized_old_name, category.name)
        )

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
        await self._send_group_overview(ctx)

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
        await self._send_group_overview(ctx)

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
