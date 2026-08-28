from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from typing import Any

MAX_PROXY_CONTENT_LENGTH = 2000
log = logging.getLogger(__name__)


class IdentityType(str, Enum):
    BOT = "bot"
    CHARACTER = "character"


@dataclass(frozen=True, slots=True)
class ProxyDestination:
    guild_id: int
    channel_id: int
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProxyIdentity:
    kind: IdentityType
    display_name: str | None = None
    preset_name: str | None = None
    avatar_bytes: bytes | None = None
    avatar_media_type: str | None = None
    avatar_sha256: str | None = None


@dataclass(slots=True)
class BotProxyDraft:
    destination: ProxyDestination | None = None
    content: str | None = None
    identity: ProxyIdentity = field(
        default_factory=lambda: ProxyIdentity(IdentityType.BOT)
    )

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.destination is None:
            errors.append("Destination is not set")
        if self.content is None or not self.content.strip():
            errors.append("Content is not set")
        elif len(self.content) > MAX_PROXY_CONTENT_LENGTH:
            errors.append(
                f"Content cannot exceed {MAX_PROXY_CONTENT_LENGTH} characters"
            )
        if self.identity.kind is IdentityType.CHARACTER:
            if not self.identity.display_name:
                errors.append("Character display name is not set")
            if self.destination is not None and self.destination.message_id is not None:
                errors.append("Characters cannot reply to an existing message")
        return tuple(errors)


@dataclass(frozen=True, slots=True)
class ActiveSession:
    session_id: str
    guild_id: int
    moderator_id: int
    thread_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionRouteKind(str, Enum):
    CREATE = "create"
    USE = "use"
    CHOOSE = "choose"


class SessionStatus(str, Enum):
    CANCELLED = "Cancelled"
    TIMED_OUT = "Timed out"
    RELOADED = "Reloaded"


@dataclass(frozen=True, slots=True)
class SessionRoute:
    kind: SessionRouteKind
    sessions: tuple[ActiveSession, ...]


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, ActiveSession] = {}

    def add(self, session: ActiveSession) -> None:
        self._sessions[session.session_id] = session

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def sessions_for(self, guild_id: int, moderator_id: int) -> tuple[ActiveSession, ...]:
        return tuple(
            sorted(
                (
                    session
                    for session in self._sessions.values()
                    if session.guild_id == guild_id
                    and session.moderator_id == moderator_id
                ),
                key=lambda session: (session.created_at, session.session_id),
            )
        )

    def route_for_message(self, guild_id: int, moderator_id: int) -> SessionRoute:
        sessions = self.sessions_for(guild_id, moderator_id)
        if not sessions:
            kind = SessionRouteKind.CREATE
        elif len(sessions) == 1:
            kind = SessionRouteKind.USE
        else:
            kind = SessionRouteKind.CHOOSE
        return SessionRoute(kind, sessions)


def _user_only_mentions() -> Any:
    discord = import_module("discord")
    return discord.AllowedMentions(
        everyone=False,
        users=True,
        roles=False,
        replied_user=False,
    )


def _no_mentions() -> Any:
    discord = import_module("discord")
    return discord.AllowedMentions.none()


def _is_discord_not_found(error: Exception) -> bool:
    try:
        discord = import_module("discord")
    except ModuleNotFoundError:
        return False
    return isinstance(error, discord.NotFound)


def _store_sender(identity_type: IdentityType) -> Any:
    store_module = import_module(".bot_proxy_store", package=__package__)
    return store_module.ProxySender(identity_type.value)


class BotProxyPublisher:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._webhook_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._message_locks: dict[tuple[int, int, int], asyncio.Lock] = {}

    async def publish(
        self,
        *,
        draft: BotProxyDraft,
        moderator_id: int,
        channel: Any,
    ) -> Any:
        errors = draft.validation_errors()
        if errors:
            raise ValueError(errors[0])
        destination = draft.destination
        if destination is None or draft.content is None:
            raise RuntimeError("validated Bot Proxy draft is incomplete")
        if channel.guild.id != destination.guild_id or channel.id != destination.channel_id:
            raise ValueError("Resolved destination does not match the Bot Proxy draft")

        if draft.identity.kind is IdentityType.CHARACTER:
            return await self._publish_character(
                draft=draft,
                moderator_id=moderator_id,
                channel=channel,
            )

        message = await self._publish_bot(draft, channel)
        await self._track_or_compensate(
            message,
            guild_id=destination.guild_id,
            channel_id=destination.channel_id,
            moderator_id=moderator_id,
            sender=_store_sender(draft.identity.kind),
            webhook_id=None,
            content=draft.content,
            reply_message_id=destination.message_id,
            character_preset_name=None,
            character_display_name=None,
            avatar_sha256=None,
        )
        return message

    async def preview(self, *, draft: BotProxyDraft, channel: Any) -> Any:
        """Render an untracked, non-notifying copy of a frozen draft."""
        errors = draft.validation_errors()
        if errors:
            raise ValueError(errors[0])
        if draft.content is None:
            raise RuntimeError("validated Bot Proxy draft has no content")
        if draft.identity.kind is IdentityType.BOT:
            return await channel.send(
                content=draft.content,
                allowed_mentions=_no_mentions(),
            )
        message, _webhook_id = await self._send_character(
            draft=draft,
            channel=channel,
            allowed_mentions=_no_mentions(),
        )
        return message

    async def edit_tracked(
        self,
        *,
        record: Any,
        channel: Any,
        content: str,
        moderator_id: int,
    ) -> Any:
        lock = self._message_lock(record)
        async with lock:
            transition_token = await self._store.claim_message_transition(
                guild_id=record.guild_id,
                channel_id=record.channel_id,
                message_id=record.message_id,
                expected_revision=record.revision,
            )
            try:
                return await self._edit_tracked_locked(
                    record=record,
                    channel=channel,
                    content=content,
                    moderator_id=moderator_id,
                    transition_token=transition_token,
                )
            finally:
                await self._store.release_message_transition(
                    guild_id=record.guild_id,
                    channel_id=record.channel_id,
                    message_id=record.message_id,
                    transition_token=transition_token,
                )

    async def _edit_tracked_locked(
        self,
        *,
        record: Any,
        channel: Any,
        content: str,
        moderator_id: int,
        transition_token: str,
    ) -> Any:
        message = None
        webhook = None
        edit_kwargs = {
            "content": content,
            "allowed_mentions": _user_only_mentions(),
        }
        if record.sender.value == IdentityType.BOT.value:
            message = await channel.fetch_message(record.message_id)
            await message.edit(**edit_kwargs)
        else:
            webhook = await self._tracked_webhook(record, channel)
            parent_channel = getattr(channel, "parent", None) or channel
            if parent_channel is not channel:
                edit_kwargs["thread"] = channel
            await webhook.edit_message(record.message_id, **edit_kwargs)
        try:
            return await self._store.edit_message(
                guild_id=record.guild_id,
                channel_id=record.channel_id,
                message_id=record.message_id,
                expected_revision=record.revision,
                content=content,
                moderator_id=moderator_id,
                transition_token=transition_token,
            )
        except Exception:  # noqa: BLE001
            rollback = {
                "content": record.content,
                "allowed_mentions": _user_only_mentions(),
            }
            try:
                if message is not None:
                    await message.edit(**rollback)
                elif webhook is not None:
                    if getattr(channel, "parent", None) is not None:
                        rollback["thread"] = channel
                    await webhook.edit_message(record.message_id, **rollback)
            except Exception:  # noqa: BLE001
                log.exception(
                    "Failed to roll back untracked Bot Proxy edit %s",
                    record.message_id,
                )
            raise

    async def delete_tracked(
        self,
        *,
        record: Any,
        channel: Any,
        moderator_id: int,
    ) -> Any:
        lock = self._message_lock(record)
        async with lock:
            transition_token = await self._store.claim_message_transition(
                guild_id=record.guild_id,
                channel_id=record.channel_id,
                message_id=record.message_id,
                expected_revision=record.revision,
            )
            try:
                return await self._delete_tracked_locked(
                    record=record,
                    channel=channel,
                    moderator_id=moderator_id,
                    transition_token=transition_token,
                )
            finally:
                await self._store.release_message_transition(
                    guild_id=record.guild_id,
                    channel_id=record.channel_id,
                    message_id=record.message_id,
                    transition_token=transition_token,
                )

    async def _delete_tracked_locked(
        self,
        *,
        record: Any,
        channel: Any,
        moderator_id: int,
        transition_token: str,
    ) -> Any:
        if record.sender.value == IdentityType.BOT.value:
            message = await channel.fetch_message(record.message_id)
            await message.delete()
        else:
            webhook = await self._tracked_webhook(record, channel)
            kwargs = {}
            if getattr(channel, "parent", None) is not None:
                kwargs["thread"] = channel
            await webhook.delete_message(record.message_id, **kwargs)
        return await self._store.mark_message_deleted(
            guild_id=record.guild_id,
            channel_id=record.channel_id,
            message_id=record.message_id,
            expected_revision=record.revision,
            moderator_id=moderator_id,
            transition_token=transition_token,
        )

    def _message_lock(self, record: Any) -> asyncio.Lock:
        key = (record.guild_id, record.channel_id, record.message_id)
        return self._message_locks.setdefault(key, asyncio.Lock())

    @staticmethod
    async def _tracked_webhook(record: Any, channel: Any) -> Any:
        parent_channel = getattr(channel, "parent", None) or channel
        for webhook in await parent_channel.webhooks():
            if webhook.id == record.webhook_id:
                return webhook
        raise ValueError("Bot Proxy webhook is unavailable")

    @staticmethod
    async def _publish_bot(draft: BotProxyDraft, channel: Any) -> Any:
        destination = draft.destination
        if destination is None or draft.content is None:
            raise RuntimeError("validated Bot Proxy draft is incomplete")
        if destination.message_id is None:
            return await channel.send(
                content=draft.content,
                allowed_mentions=_user_only_mentions(),
            )
        source_message = await channel.fetch_message(destination.message_id)
        return await source_message.reply(
            content=draft.content,
            allowed_mentions=_user_only_mentions(),
            mention_author=False,
        )

    async def _publish_character(
        self,
        *,
        draft: BotProxyDraft,
        moderator_id: int,
        channel: Any,
    ) -> Any:
        destination = draft.destination
        identity = draft.identity
        if destination is None or draft.content is None or identity.display_name is None:
            raise RuntimeError("validated Bot Proxy character draft is incomplete")
        message, webhook_id = await self._send_character(
            draft=draft,
            channel=channel,
            allowed_mentions=_user_only_mentions(),
        )
        avatar_sha256 = identity.avatar_sha256
        if avatar_sha256 is None and identity.avatar_bytes is not None:
            avatar_sha256 = hashlib.sha256(identity.avatar_bytes).hexdigest()
        await self._track_or_compensate(
            message,
            guild_id=destination.guild_id,
            channel_id=destination.channel_id,
            moderator_id=moderator_id,
            sender=_store_sender(identity.kind),
            webhook_id=webhook_id,
            content=draft.content,
            reply_message_id=None,
            character_preset_name=identity.preset_name,
            character_display_name=identity.display_name,
            avatar_sha256=avatar_sha256,
        )
        return message

    async def _send_character(
        self,
        *,
        draft: BotProxyDraft,
        channel: Any,
        allowed_mentions: Any,
    ) -> tuple[Any, int]:
        destination = draft.destination
        identity = draft.identity
        if destination is None or draft.content is None or identity.display_name is None:
            raise RuntimeError("validated Bot Proxy character draft is incomplete")
        parent_channel = getattr(channel, "parent", None) or channel
        lock_key = (channel.guild.id, parent_channel.id)
        lock = self._webhook_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            webhook = await self._owned_webhook(channel.guild.id, parent_channel)
            webhook = await webhook.edit(avatar=identity.avatar_bytes)
            send_kwargs = {
                "content": draft.content,
                "username": identity.display_name,
                "allowed_mentions": allowed_mentions,
                "wait": True,
            }
            if parent_channel is not channel:
                send_kwargs["thread"] = channel
            return await webhook.send(**send_kwargs), webhook.id

    async def _owned_webhook(self, guild_id: int, parent_channel: Any) -> Any:
        webhook_id = await self._store.get_webhook_id(guild_id, parent_channel.id)
        if webhook_id is not None:
            for webhook in await parent_channel.webhooks():
                if webhook.id == webhook_id:
                    return webhook
            await self._store.forget_webhook(guild_id, parent_channel.id)

        webhook = await parent_channel.create_webhook(name="Bot Proxy")
        await self._store.remember_webhook(guild_id, parent_channel.id, webhook.id)
        return webhook

    async def _track_or_compensate(self, message: Any, **record: Any) -> None:
        try:
            await self._store.record_message(message_id=message.id, **record)
        except Exception:  # noqa: BLE001
            try:
                await message.delete()
            except Exception:  # noqa: BLE001
                log.exception(
                    "Failed to delete untracked Bot Proxy message %s",
                    message.id,
                )
            raise


class BotProxySession:
    def __init__(
        self,
        *,
        active: ActiveSession,
        registry: SessionRegistry,
        store: Any,
        thread: Any,
        dashboard: Any,
    ) -> None:
        self.active = active
        self._registry = registry
        self._store = store
        self._thread = thread
        self._dashboard = dashboard
        self.status: SessionStatus | None = None
        self._cleanup_complete = False

    async def finish(
        self,
        status: SessionStatus,
        *,
        launcher: Any | None = None,
        delete: bool = False,
    ) -> None:
        if self._cleanup_complete:
            return
        if self.status is None:
            self.status = status
        self._registry.remove(self.active.session_id)
        if delete:
            if launcher is None:
                raise RuntimeError("Bot Proxy launcher is required for deletion")
            failure = await self._delete_discord_state(launcher)
        else:
            failure = await self._archive_discord_state(status)
        if failure is not None:
            raise failure
        await self._store.remove_active_session(self.active.session_id)
        self._cleanup_complete = True

    async def _delete_discord_state(self, launcher: Any) -> Exception | None:
        failure: Exception | None = None
        for resource in (self._thread, launcher):
            try:
                await resource.delete()
            except Exception as error:  # noqa: BLE001
                if _is_discord_not_found(error):
                    continue
                if failure is None:
                    failure = error
        return failure

    async def _archive_discord_state(
        self,
        status: SessionStatus,
    ) -> Exception | None:
        failure: Exception | None = None
        try:
            await self._dashboard.edit(
                content=f"Bot Proxy session: {status.value}",
                view=None,
            )
        except Exception as error:  # noqa: BLE001
            failure = error
        try:
            await self._thread.edit(archived=True, locked=True)
        except Exception as error:  # noqa: BLE001
            if failure is None:
                failure = error
        return failure
