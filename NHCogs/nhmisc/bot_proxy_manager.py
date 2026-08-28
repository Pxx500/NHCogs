from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    SessionStatus,
)
from .bot_proxy_store import ActiveSessionRecord, BotProxyStore, CharacterPreset
from .bot_proxy_workflow import (
    BotProxyWorkflowSession,
    WorkflowInputError,
    _get_channel_or_thread,
    _moderator_mention,
)

MESSAGE_LINK_PATTERN = re.compile(
    r"^https://(?:canary\.|ptb\.)?discord\.com/channels/(\d+)/(\d+)/(\d+)$"
)
CHANNEL_MENTION_PATTERN = re.compile(r"^<#(\d+)>$")
LOG_CONTENT_PREVIEW_LENGTH = 300
log = logging.getLogger(__name__)


def _compact_content_preview(content: str) -> str:
    preview = " ".join(content.split())
    if len(preview) <= LOG_CONTENT_PREVIEW_LENGTH:
        return preview
    return f"{preview[: LOG_CONTENT_PREVIEW_LENGTH - 3]}..."

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
        self._state_locks: dict[int, asyncio.Lock] = {}
        self._disabled_guild_ids: set[int] = set()
        self._session_sequences: dict[tuple[int, int], int] = {}
        self._moderation_log = moderation_log
        self._error_reporter = error_reporter
        self._avatar_loader = avatar_loader

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(session.finish(SessionStatus.RELOADED) for session in tuple(self.sessions.values())),
            return_exceptions=True,
        )

    def _state_lock(self, guild_id: int) -> asyncio.Lock:
        return self._state_locks.setdefault(guild_id, asyncio.Lock())

    async def enabled(self, guild: discord.Guild) -> bool:
        async with self._state_lock(guild.id):
            return await self._enabled_locked(guild)

    async def _enabled_locked(self, guild: discord.Guild) -> bool:
        value = bool(await self.config.guild(guild).bot_proxy_enabled())
        if value:
            self._disabled_guild_ids.discard(guild.id)
        else:
            self._disabled_guild_ids.add(guild.id)
        return value

    async def require_enabled(self, guild: discord.Guild) -> None:
        if not await self.enabled(guild):
            raise WorkflowInputError("Bot Proxy is disabled")

    def require_enabled_now(self, guild_id: int) -> None:
        if guild_id in self._disabled_guild_ids:
            raise WorkflowInputError("Bot Proxy is disabled")

    @asynccontextmanager
    async def enabled_operation(
        self,
        guild: discord.Guild,
    ) -> AsyncIterator[None]:
        async with self._state_lock(guild.id):
            self.require_enabled_now(guild.id)
            yield

    async def set_enabled(self, guild: discord.Guild, enabled: bool) -> None:
        async with self._state_lock(guild.id):
            await self.config.guild(guild).bot_proxy_enabled.set(enabled)
            if enabled:
                self._disabled_guild_ids.discard(guild.id)
                return
            self._disabled_guild_ids.add(guild.id)
            sessions = tuple(
                session
                for session in self.sessions.values()
                if session.guild.id == guild.id
            )
        for session in sessions:
            try:
                await session.finish(SessionStatus.DISABLED)
            except Exception as error:  # noqa: BLE001
                await self.report_session_error(
                    session,
                    error,
                    action="close disabled Bot Proxy session",
                )

    async def create_session(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        *,
        destination: ProxyDestination | None = None,
    ) -> BotProxyWorkflowSession:
        async with self._state_lock(guild.id):
            if not await self._enabled_locked(guild):
                raise WorkflowInputError("Bot Proxy is disabled")
            return await self._create_session(
                guild,
                moderator,
                destination=destination,
            )

    async def _create_session(
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
            ("manage_messages", "Manage Messages"),
            ("manage_webhooks", "Manage Webhooks"),
        )
        missing = [label for attr, label in required if not getattr(permissions, attr)]
        if missing:
            raise WorkflowInputError(
                f"Bot is missing {', '.join(missing)} in {channel.mention}"
            )
        return channel

    async def delete_closed_sessions(self, guild: discord.Guild) -> bool:
        guild_config_factory = getattr(self.config, "guild", None)
        if guild_config_factory is None:
            return False
        setting = getattr(
            guild_config_factory(guild),
            "bot_proxy_delete_closed_sessions",
            None,
        )
        if setting is None:
            return False
        value = await setting()
        return bool(value)

    async def resolve_destination(
        self, guild: discord.Guild, value: str
    ) -> ProxyDestination:
        channel_match = CHANNEL_MENTION_PATTERN.fullmatch(value)
        if channel_match is not None:
            channel_id = int(channel_match.group(1))
        elif value.isdigit():
            channel_id = int(value)
        else:
            channel_id = None
        if channel_id is not None:
            channel = _get_channel_or_thread(guild, channel_id)
            if channel is None:
                raise WorkflowInputError("Channel is unavailable")
            forum_or_media = tuple(
                channel_type
                for name in ("ForumChannel", "MediaChannel")
                if (channel_type := getattr(discord, name, None)) is not None
            )
            if forum_or_media and isinstance(channel, forum_or_media):
                raise WorkflowInputError("Choose an existing forum or media post")
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise WorkflowInputError("Choose a text channel or thread")
            return ProxyDestination(guild.id, channel.id)
        message_match = MESSAGE_LINK_PATTERN.fullmatch(value)
        if message_match is None or int(message_match.group(1)) != guild.id:
            raise WorkflowInputError(
                "Send a channel mention, channel ID, or same-server message link"
            )
        channel_id = int(message_match.group(2))
        message_id = int(message_match.group(3))
        channel = _get_channel_or_thread(guild, channel_id)
        if channel is None:
            raise WorkflowInputError("Message channel is unavailable")
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise WorkflowInputError("Choose a message in a text channel or thread")
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
        if session is None:
            return False
        if not await self.enabled(session.guild):
            await session.thread.send(
                "Bot Proxy is disabled",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        return await session.handle_message(message)

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
        identity_suffix = ""
        if identity.kind is IdentityType.CHARACTER:
            identity_suffix = f" as {identity.display_name or 'character'}"
        content_preview = _compact_content_preview(published_draft.content or "")
        notification = (
            f"{moderator.mention} sent Bot Proxy{identity_suffix}: {message.jump_url}"
        )
        if content_preview:
            notification = f"{notification}\n{content_preview}"
        try:
            await self._write_moderation_log(session.guild, notification)
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
                f"{moderator.mention} {action} Bot Proxy character "
                f"{preset.preset_name} as {preset.display_name}",
            )
        except Exception as error:  # noqa: BLE001
            await self._error_reporter(
                guild_id=guild.id,
                source="NHMisc",
                action=f"audit Bot Proxy character {action}",
                error=error,
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
