from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import discord

from .bot_proxy import (
    ActiveSession,
    BotProxyDraft,
    BotProxyPublisher,
    IdentityType,
    ProxyDestination,
    SessionRegistry,
    SessionRouteKind,
    SessionStatus,
)
from .bot_proxy_store import ActiveSessionRecord, BotProxyStore, CharacterPreset
from .bot_proxy_workflow import (
    BotProxyWorkflowSession,
    SessionPickerView,
    TrackedMessageActionsView,
    WorkflowInputError,
    _get_channel_or_thread,
    _moderator_mention,
)

MESSAGE_LINK_PATTERN = re.compile(
    r"^https://(?:canary\.|ptb\.)?discord\.com/channels/(\d+)/(\d+)/(\d+)$"
)
CHANNEL_MENTION_PATTERN = re.compile(r"^<#(\d+)>$")
log = logging.getLogger(__name__)

@dataclass(slots=True)
class AvatarData:
    data: bytes
    media_type: str
    sha256: str


class BotProxyWorkflowManager:
    def __init__(
        self,
        *,
        config: Any,
        store: BotProxyStore,
        moderation_log: Any,
        error_reporter: Any,
        avatar_loader: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.publisher = BotProxyPublisher(store)
        self.registry = SessionRegistry()
        self.sessions: dict[str, BotProxyWorkflowSession] = {}
        self._route_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._session_sequences: dict[tuple[int, int], int] = {}
        self._moderation_log = moderation_log
        self._error_reporter = error_reporter
        self._avatar_loader = avatar_loader

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(session.finish(SessionStatus.RELOADED) for session in tuple(self.sessions.values())),
            return_exceptions=True,
        )

    async def create_session(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        *,
        destination: ProxyDestination | None = None,
    ) -> BotProxyWorkflowSession:
        workspace = await self.workspace_channel(guild)
        sequence_key = (guild.id, moderator.id)
        session_number = self._session_sequences.get(sequence_key, 0) + 1
        self._session_sequences[sequence_key] = session_number
        launcher = await workspace.send(
            f"{moderator.mention} opened a Bot Proxy session",
            allowed_mentions=_moderator_mention(moderator),
        )
        thread = await launcher.create_thread(
            name=f"bot-proxy-{moderator.display_name}-{session_number}"[:100],
            auto_archive_duration=60,
        )
        placeholder = await thread.send("Opening Bot Proxy session")
        active = ActiveSession(uuid4().hex, guild.id, moderator.id, thread.id)
        session = BotProxyWorkflowSession(
            self,
            active=active,
            guild=guild,
            moderator=moderator,
            launcher=launcher,
            thread=thread,
            dashboard=placeholder,
            draft=BotProxyDraft(destination=destination),
        )
        recorded = False
        try:
            await self.store.record_active_session(
                ActiveSessionRecord(
                    session_id=active.session_id,
                    guild_id=guild.id,
                    moderator_id=moderator.id,
                    launcher_channel_id=workspace.id,
                    launcher_message_id=launcher.id,
                    thread_id=thread.id,
                    dashboard_message_id=placeholder.id,
                )
            )
            recorded = True
            self.registry.add(active)
            self.sessions[active.session_id] = session
            await session.refresh()
        except Exception:
            self.registry.remove(active.session_id)
            self.sessions.pop(active.session_id, None)
            try:
                await thread.edit(archived=True, locked=True)
                if recorded:
                    await self.store.remove_active_session(active.session_id)
            except Exception as cleanup_error:
                await self._error_reporter(
                    guild_id=guild.id,
                    source="NHMisc",
                    action="roll back Bot Proxy session creation",
                    error=cleanup_error,
                    channel_id=workspace.id,
                    thread_id=thread.id,
                    message_id=placeholder.id,
                )
            raise
        return session

    async def workspace_channel(self, guild: discord.Guild) -> discord.TextChannel:
        channel_id = await self.config.guild(guild).bot_proxy_channel()
        if channel_id is None:
            raise WorkflowInputError("Bot Proxy channel is not configured")
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise WorkflowInputError("Configured Bot Proxy channel is unavailable")
        if channel.permissions_for(guild.default_role).view_channel:
            raise WorkflowInputError("Configured Bot Proxy channel is visible to @everyone")
        permissions = channel.permissions_for(guild.me)
        required = (
            ("view_channel", "View Channel"),
            ("send_messages", "Send Messages"),
            ("create_public_threads", "Create Public Threads"),
            ("send_messages_in_threads", "Send Messages in Threads"),
            ("manage_threads", "Manage Threads"),
        )
        missing = [label for attr, label in required if not getattr(permissions, attr)]
        if missing:
            raise WorkflowInputError(
                f"Bot is missing {', '.join(missing)} in {channel.mention}"
            )
        return channel

    async def route_message(
        self,
        interaction: discord.Interaction,
        source: discord.Message,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Bot Proxy is only available in a server",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        tracked = await self.store.get_message(
            interaction.guild.id,
            source.channel.id,
            source.id,
        )
        if tracked is not None and tracked.deleted_at is None:
            await interaction.edit_original_response(
                content="Choose a Bot Proxy action",
                view=TrackedMessageActionsView(
                    self,
                    tracked,
                    interaction.user.id,
                ),
            )
            return
        await self.route_destination_after_defer(
            interaction,
            ProxyDestination(
                interaction.guild.id,
                source.channel.id,
                source.id,
            ),
        )

    async def route_destination_after_defer(
        self,
        interaction: discord.Interaction,
        destination: ProxyDestination,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.edit_original_response(
                content="Bot Proxy is only available in a server",
                view=None,
            )
            return
        key = (interaction.guild.id, interaction.user.id)
        lock = self._route_locks.setdefault(key, asyncio.Lock())
        async with lock:
            route = self.registry.route_for_message(*key)
            if route.kind is SessionRouteKind.CREATE:
                session = await self.create_session(
                    interaction.guild,
                    interaction.user,
                    destination=destination,
                )
                await interaction.edit_original_response(
                    content=f"Open {session.thread.mention}"
                )
                return
            if route.kind is SessionRouteKind.USE:
                session = self.sessions[route.sessions[0].session_id]
                await session.set_destination(destination)
                await interaction.edit_original_response(
                    content=f"Open {session.thread.mention}"
                )
                return
            await interaction.edit_original_response(
                content="Choose an active Bot Proxy session",
                view=SessionPickerView(self, route.sessions, destination),
            )

    async def resolve_destination(
        self, guild: discord.Guild, value: str
    ) -> ProxyDestination:
        channel_match = CHANNEL_MENTION_PATTERN.fullmatch(value)
        if channel_match is not None:
            channel_id = int(channel_match.group(1))
            channel = _get_channel_or_thread(guild, channel_id)
            if channel is None:
                raise WorkflowInputError("Channel is unavailable")
            if isinstance(channel, (discord.ForumChannel, discord.MediaChannel)):
                raise WorkflowInputError("Choose an existing forum or media post")
            return ProxyDestination(guild.id, channel.id)
        message_match = MESSAGE_LINK_PATTERN.fullmatch(value)
        if message_match is None or int(message_match.group(1)) != guild.id:
            raise WorkflowInputError("Send a channel mention or same-server message link")
        channel_id = int(message_match.group(2))
        message_id = int(message_match.group(3))
        channel = _get_channel_or_thread(guild, channel_id)
        if channel is None:
            raise WorkflowInputError("Message channel is unavailable")
        try:
            await channel.fetch_message(message_id)
        except discord.NotFound as error:
            raise WorkflowInputError("Reply target no longer exists") from error
        return ProxyDestination(guild.id, channel_id, message_id)

    async def resolve_publish_channel(
        self,
        guild: discord.Guild,
        destination: ProxyDestination | None,
        moderator: discord.Member,
        *,
        identity_type: IdentityType = IdentityType.BOT,
    ) -> Any:
        if destination is None:
            raise WorkflowInputError("Destination is not set")
        channel = _get_channel_or_thread(guild, destination.channel_id)
        if channel is None:
            raise WorkflowInputError("Destination is unavailable")
        moderator_permissions = channel.permissions_for(moderator)
        if not moderator_permissions.manage_messages:
            raise WorkflowInputError("You need Manage Messages in the destination")
        bot_permissions = channel.permissions_for(guild.me)
        if not bot_permissions.view_channel:
            raise WorkflowInputError("Bot cannot send messages in the destination")
        if isinstance(channel, discord.Thread):
            if not bot_permissions.send_messages_in_threads:
                raise WorkflowInputError("Bot cannot send messages in this thread")
        elif not bot_permissions.send_messages:
            raise WorkflowInputError("Bot cannot send messages in the destination")
        if destination.message_id is not None and not bot_permissions.read_message_history:
            raise WorkflowInputError("Bot needs Read Message History for replies")
        parent_channel = getattr(channel, "parent", None) or channel
        if (
            identity_type is IdentityType.CHARACTER
            and not parent_channel.permissions_for(guild.me).manage_webhooks
        ):
            raise WorkflowInputError("Bot needs Manage Webhooks for character messages")
        return channel

    async def load_avatar_url(self, value: str) -> AvatarData | None:
        if not value:
            return None
        if self._avatar_loader is None:
            raise WorkflowInputError("Avatar loading is unavailable")
        loaded = await self._avatar_loader.from_url(value)
        return AvatarData(
            loaded.avatar_bytes,
            loaded.avatar_media_type,
            loaded.avatar_sha256,
        )

    async def load_avatar_attachment(self, attachment: Any) -> AvatarData:
        if self._avatar_loader is None:
            raise WorkflowInputError("Avatar loading is unavailable")
        loaded = await self._avatar_loader.from_attachment(attachment)
        return AvatarData(
            loaded.avatar_bytes,
            loaded.avatar_media_type,
            loaded.avatar_sha256,
        )

    async def handle_message(self, message: discord.Message) -> bool:
        session = next(
            (
                item
                for item in self.sessions.values()
                if item.thread.id == message.channel.id
            ),
            None,
        )
        return await session.handle_message(message) if session is not None else False

    async def log_publication(
        self,
        session: BotProxyWorkflowSession,
        moderator: discord.Member,
        message: discord.Message,
        *,
        draft: BotProxyDraft | None = None,
    ) -> None:
        published_draft = session.draft if draft is None else draft
        identity = published_draft.identity
        destination = published_draft.destination
        destination_link = message.jump_url
        source_line = ""
        if destination is not None and destination.message_id is not None:
            source_line = (
                "\nReply target: https://discord.com/channels/"
                f"{destination.guild_id}/{destination.channel_id}/"
                f"{destination.message_id}"
            )
        metadata = (
            f"Bot Proxy send by {moderator.mention}\n"
            f"Message: {message.jump_url}\n"
            f"Destination: {destination_link}{source_line}\n"
            f"Sender: {identity.kind.value}\n"
            f"Character preset: {identity.preset_name or 'none'}\n"
            f"Character name: {identity.display_name or 'none'}\n"
            f"Avatar digest: {identity.avatar_sha256 or 'none'}"
        )
        try:
            await self._write_moderation_log(session.guild, metadata)
            await self._write_moderation_log(
                session.guild,
                published_draft.content or "",
            )
        except Exception as error:  # noqa: BLE001
            await self.report_session_error(
                session,
                error,
                action="audit Bot Proxy publication",
            )

    async def log_preset_change(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        action: str,
        preset: CharacterPreset,
    ) -> None:
        try:
            await self._write_moderation_log(
                guild,
                f"Bot Proxy character {action} by {moderator.mention}: "
                f"{preset.preset_name} as {preset.display_name}\n"
                f"Avatar digest: {preset.avatar_sha256 or 'none'}",
            )
        except Exception as error:  # noqa: BLE001
            await self._error_reporter(
                guild_id=guild.id,
                source="NHMisc",
                action=f"audit Bot Proxy character {action}",
                error=error,
            )

    async def log_tracked_change(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        action: str,
        record: Any,
    ) -> None:
        try:
            await self._write_moderation_log(
                guild,
                f"Bot Proxy message {action} by {moderator.mention}: "
                f"https://discord.com/channels/{record.guild_id}/"
                f"{record.channel_id}/{record.message_id}\n"
                f"Sender: {record.sender.value}\n"
                f"Revision: {record.revision}\n"
                f"Character preset: {record.character_preset_name or 'none'}\n"
                f"Avatar digest: {record.avatar_sha256 or 'none'}",
            )
            await self._write_moderation_log(guild, record.content)
        except Exception as error:  # noqa: BLE001
            await self._error_reporter(
                guild_id=guild.id,
                source="NHMisc",
                action=f"audit Bot Proxy message {action}",
                error=error,
                channel_id=record.channel_id,
                message_id=record.message_id,
            )

    async def _write_moderation_log(
        self, guild: discord.Guild, content: str
    ) -> None:
        result = await self._moderation_log(guild, content)
        if result is False:
            raise RuntimeError("Bot Proxy moderation log was not delivered")

    async def report_session_error(
        self,
        session: BotProxyWorkflowSession,
        error: Exception,
        *,
        action: str,
    ) -> None:
        await self._error_reporter(
            guild_id=session.guild.id,
            source="NHMisc",
            action=action,
            error=error,
            channel_id=session.thread.id,
            thread_id=session.thread.id,
            message_id=session.dashboard.id,
        )

    async def handle_tracked_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        action: str,
        record: Any,
    ) -> None:
        if isinstance(error, (WorkflowInputError, ValueError)):
            await self.private_feedback(interaction, str(error))
            return
        await self._error_reporter(
            guild_id=record.guild_id,
            source="NHMisc",
            action=action,
            error=error,
            channel_id=record.channel_id,
            message_id=record.message_id,
        )
        await self.private_feedback(interaction, "Bot Proxy failed")

    async def handle_expected_or_reported_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        action: str,
        session: BotProxyWorkflowSession,
    ) -> None:
        if isinstance(error, (WorkflowInputError, ValueError)):
            await self.private_feedback(interaction, str(error))
            return
        await self._error_reporter(
            guild_id=session.guild.id,
            source="NHMisc",
            action=action,
            error=error,
            channel_id=session.thread.id,
            thread_id=session.thread.id,
            message_id=session.dashboard.id,
        )
        await self.private_feedback(interaction, "Bot Proxy failed")

    async def private_feedback(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=content, view=None)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except discord.HTTPException as error:
            guild = interaction.guild
            if guild is None:
                log.exception("Failed to deliver Bot Proxy private feedback")
                return
            try:
                await self._error_reporter(
                    guild_id=guild.id,
                    source="NHMisc",
                    action="deliver Bot Proxy private feedback",
                    error=error,
                    channel_id=getattr(interaction.channel, "id", None),
                )
            except Exception:
                log.exception("Failed to report undelivered Bot Proxy feedback")
